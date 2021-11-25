import argparse
import asyncio
import logging
import os
import uuid
import re

import toml
# from mastodon import Mastodon
import atoot
from telethon import TelegramClient, events
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.contacts import ResolveUsernameRequest
from telethon.tl.types import InputChannel
from telethon.utils import get_extension

logging.basicConfig(level=logging.INFO)


class BridgeBot:
    @classmethod
    async def create(cls, cfg: dict):
        self = BridgeBot()
        # RegExps
        self.re_md_links = re.compile(
            r'\[(.*?)\]\((https?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\(\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+)\)')
        # Config init
        self.config = cfg
        self.mastodon_clients = {}
        self.tg_mstdn_mappings = {}
        for acc in cfg["mastodon"]["accounts"]:
            mastodon_client = await atoot.MastodonAPI.create(
                client_id=acc["client_id"],
                client_secret=acc["client_secret"],
                access_token=acc["access_token"],
                instance=acc["api_base_url"]
            )
            self.mastodon_clients[acc["name"]] = {
                "client": mastodon_client,
                "visibility": acc.get("visibility", None),
                "post_size_limit": acc.get("post_size_limit", 500)
            }
        for m in cfg["mastodon"]["mappings"]:
            if self.tg_mstdn_mappings.get("tg_channel_handle", None) is None:
                self.tg_mstdn_mappings[m["tg_channel_handle"]] = []
            self.tg_mstdn_mappings[m["tg_channel_handle"]].append(m["account_name"])

        self.tg_client = TelegramClient(cfg["telegram"]["session_file"], cfg["telegram"]["api_id"],
                                        cfg["telegram"]["api_hash"])
        return self

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
                    full_text = event.message.text
                    full_text = full_text.replace('**', '')
                    full_text = full_text.replace('__', '')
                    full_text = full_text.replace('~~', '')
                    full_text = full_text.replace('`', '')
                    full_text = re.sub(self.re_md_links, r'\g<1> \g<2> ', full_text)
                    # URL of Telegram message
                    full_text = f"[https://t.me/{channel.username}/" + str(event.message.id) + "]\n\n" + full_text
                    if event.message.file and not (event.message.photo or event.message.video or event.message.gif):
                        full_text = full_text + "\n\n[К оригинальному посту приложен файл " + event.message.file.name + "]"
                    reply_start = 0
                    logging.debug("start reply_start: " + str(reply_start))
                    temp_file_path: str = ""
                    # Downloading media if tg post contains it
                    if (event.message.photo or event.message.video or event.message.gif) and not hasattr(
                            event.message.media, "webpage"):
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
                        # Attach media if tg post contains it
                        if temp_file_path != "":
                            mstdn_media_meta = await current_mastodon_client["client"].upload_attachment(
                                open(temp_file_path, 'rb'))
                            media_ids = [mstdn_media_meta["id"]]
                        else:
                            media_ids = None

                        chunks = split_text_into_chunks(full_text, current_mastodon_client["post_size_limit"])
                        reply_to = await current_mastodon_client["client"].create_status(status=chunks[0],
                                                                                         media_ids=media_ids,
                                                                                         visibility=
                                                                                         current_mastodon_client[
                                                                                             "visibility"])
                        for i in range(len(chunks)):
                            if i == 0:
                                continue
                            reply_to = await current_mastodon_client["client"].create_status(status=chunks[i],
                                                                                             in_reply_to_id=reply_to[
                                                                                                 'id'],
                                                                                             visibility=
                                                                                             current_mastodon_client[
                                                                                                 "visibility"])

                    # Delete media attach
                    if temp_file_path != "":
                        os.remove(temp_file_path)


def split_text_into_chunks(text: str, max_chars: int):
    words = text.split(" ")
    chunks = list()
    latest_chunk = 0
    missing_word = ""
    for v in words:
        if len(chunks) < latest_chunk + 1:
            if missing_word != "":
                chunks.append("…" + missing_word + " ")
            else:
                chunks.append("")
        if len(chunks[latest_chunk]) + len(v + " ") <= max_chars:
            chunks[latest_chunk] += v + " "
        else:
            chunks[latest_chunk] = chunks[latest_chunk].strip()
            if len(chunks[latest_chunk]) + len("…") <= max_chars:
                chunks[latest_chunk] += "…"
            latest_chunk += 1
            missing_word = v
    return chunks


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=str,
                        help='A path to bot configuration')
    args = parser.parse_args()

    config: dict = toml.loads(open(args.config, "r").read())
    bot = asyncio.get_event_loop().run_until_complete(BridgeBot.create(config))
    asyncio.get_event_loop().run_until_complete(bot.run())
