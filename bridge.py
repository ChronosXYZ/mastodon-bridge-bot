import argparse
import asyncio
import logging
import os
import uuid
import re

import toml
from mastodon import Mastodon
from telethon import TelegramClient, events
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.contacts import ResolveUsernameRequest
from telethon.tl.types import InputChannel
from telethon.utils import get_extension

logging.basicConfig(level=logging.INFO)

class BridgeBot:

    def __init__(self, cfg: dict):
        # RegExps
        self.re_md_links = re.compile(r'\[(.*?)\]\((https?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\(\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+)\)')
        # Config init
        self.config = cfg
        self.mastodon_clients = {}
        self.mastodon_clients_visibility = {}
        self.tg_mstdn_mappings = {}
        for acc in cfg["mastodon"]["accounts"]:
            mastodon_client = Mastodon(
                client_id=acc["client_id"],
                client_secret=acc["client_secret"],
                access_token=acc["access_token"],
                api_base_url=acc["api_base_url"]
            )
            self.mastodon_clients[acc["name"]] = mastodon_client
            try: 
              self.mastodon_clients_visibility[acc["name"]] = acc["visibility"]
            except KeyError:
              self.mastodon_clients_visibility[acc["name"]] = None
        for m in cfg["mastodon"]["mappings"]:
            if self.tg_mstdn_mappings.get("tg_channel_handle", None) is None:
                self.tg_mstdn_mappings[m["tg_channel_handle"]] = []
            self.tg_mstdn_mappings[m["tg_channel_handle"]].append(m["account_name"])

        self.tg_client = TelegramClient(cfg["telegram"]["session_file"], cfg["telegram"]["api_id"],
                                        cfg["telegram"]["api_hash"])

    async def run(self):
        await self.tg_client.connect()
        await self.tg_client.start()
        for ch_id in self.config["telegram"]["channels"]:
            result = await self.tg_client(ResolveUsernameRequest(ch_id))
            channel = InputChannel(result.peer.channel_id, result.chats[0].access_hash)
            await self.tg_client(JoinChannelRequest(channel))
        self.tg_client.add_event_handler(self._tg_event_handler)
        logging.info("Bot has been started")
        await self.tg_client.run_until_disconnected()

    @events.register(events.NewMessage())
    async def _tg_event_handler(self, event: events.NewMessage.Event):
        if event.message.post:
            channel = await event.get_chat()
            if channel.broadcast:
                if channel.username in self.tg_mstdn_mappings.keys():
                    if event.message.grouped_id is not None:
                        logging.warning("Albums isn't supported yet")
                        return
                    logging.info(f"Catched new post from telegram channel {channel.username}")
                    # Common Mastodon message limit size. Change if you increased this limit.
                    mstdn_post_limit = 500
                    full_text = event.message.text
                    # Uncomment for debug
                    #print(full_text)
                    full_text = full_text.replace('**', '')
                    full_text = full_text.replace('__', '')
                    full_text = full_text.replace('~~', '')
                    full_text = full_text.replace('`', '')
                    full_text = re.sub(self.re_md_links, r'\g<1> \g<2> ', full_text)
                    # URL of Telegram message
                    tg_message_url = f"[https://t.me/{channel.username}/" + str(event.message.id) + "]\n\n"
                    if event.message.file and not (event.message.photo or event.message.video or event.message.gif):
                      full_text = full_text + "\n\n[К оригинальному посту приложен файл " + event.message.file.name + "]"
                    # Size of full text
                    full_text_size = len(full_text)
                    # Mastodon max post size with TG message URL
                    mstdn_post_size = mstdn_post_limit - len(tg_message_url)
                    # Initial vars
                    long_post_tail = ''
                    reply_start = 0
                    reply_end = 0
                    # Post text if tg message lenght is lt mstdn post limit
                    post_text: str =  tg_message_url + full_text[reply_start:mstdn_post_size]
                    # Set reply_start to non zero for future chunking and make mstdn post with continuaniton note
                    logging.debug("full_text_size: " + str(full_text_size))
                    if full_text_size > mstdn_post_size:
                        long_post_tail = "\n\n[Откройте пост по ссылке или прочитайте продолжение в обсуждении]"
                        mstdn_post_size = mstdn_post_size - len(long_post_tail)
                        reply_start = full_text.rfind(' ', reply_start, mstdn_post_size)
                        post_text: str =  tg_message_url + full_text[0:reply_start] + long_post_tail
                    logging.debug("start reply_start: " + str(reply_start))
                    temp_file_path: str = ""
                    # Downloading media if tg post contains it
                    if (event.message.photo or event.message.video or event.message.gif) and not hasattr(event.message.media, "webpage"):
                        logging.info("Post contains the media, downloading it...")
                        temp_file_name = uuid.uuid4()
                        temp_file_path = f"/tmp/{temp_file_name}{get_extension(event.message.media)}"
                        await self.tg_client.download_media(event.message.media, temp_file_path)
                    # Starting to post messages
                    for mstdn_acc_name in self.tg_mstdn_mappings[channel.username]:
                        if self.mastodon_clients.get(mstdn_acc_name, None) is None:
                            logging.error(f"{mstdn_acc_name} doesn't exists in mastodon.accounts section of config!")
                            return
                        # Make current client with config
                        current_mastodon_client = self.mastodon_clients[mstdn_acc_name]
                        current_mstdn_acc_visibility = self.mastodon_clients_visibility[mstdn_acc_name]
                        # Attach media if tg post contains it
                        if temp_file_path != "":
                          mstdn_media_meta = current_mastodon_client.media_post(temp_file_path)
                        else:
                          mstdn_media_meta = None
                        # First root mstdn post
                        reply_to = current_mastodon_client.status_post(post_text, media_ids=[mstdn_media_meta], visibility=current_mstdn_acc_visibility)
                        tg_message_url = f"[Продолжение https://t.me/{channel.username}/" + str(event.message.id) + "]\n\n"
                        # Chunking post into mstdn limit chunks and reply to root post
                        emergency_break = 0
                        emergency_break_limit = 14
                        while reply_start + mstdn_post_limit < full_text_size:
                            logging.debug("while reply_start:" + str(reply_start)) 
                            reply_end = full_text.rfind(' ', reply_start, reply_start + mstdn_post_limit - len(tg_message_url)*2)
                            if reply_end == reply_start:
                              reply_end = reply_start + mstdn_post_limit - len(tg_message_url)*2
                            logging.debug("while reply_end:" + str(reply_end))
                            post_text: str =  tg_message_url + full_text[reply_start+1:reply_end]
                            reply_to = current_mastodon_client.status_post(post_text, in_reply_to_id=reply_to, visibility=current_mstdn_acc_visibility)
                            reply_start = reply_end
                            # Emergency break for long or endlessly looped posts
                            emergency_break = emergency_break + 1
                            if emergency_break >= emergency_break_limit:
                              logging.debug("Breaking very long reply thread") 
                              break
                        # Final chunk to reply to root post
                        if reply_start > 0:
                            logging.debug("final reply_start: " + str(reply_start))
                            post_text: str =  tg_message_url + full_text[reply_start+1:full_text_size]
                            reply_to = current_mastodon_client.status_post(post_text, in_reply_to_id=reply_to, visibility=current_mstdn_acc_visibility)
                    # Delete media attach
                    if temp_file_path != "":
                        os.remove(temp_file_path)

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=str,
                        help='A path to bot configuration')
    args = parser.parse_args()

    config: dict = toml.loads(open(args.config, "r").read())
    bot = BridgeBot(config)
    asyncio.get_event_loop().run_until_complete(bot.run())
