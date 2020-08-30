import argparse
import asyncio
import logging
import os
import uuid

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
        self.config = cfg
        self.mastodon_clients = {}
        self.tg_mstdn_mappings = {}
        for acc in cfg["mastodon"]["accounts"]:
            mastodon_client = Mastodon(
                client_id=acc["client_id"],
                client_secret=acc["client_secret"],
                access_token=acc["access_token"],
                api_base_url=acc["api_base_url"]
            )
            self.mastodon_clients[acc["name"]] = mastodon_client

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
                    logging.debug("dobbry vechur")
                    logging.info(f"Catched new post from telegram channel {channel.username}")
                    text: str = event.message.text
                    if event.message.forward:
                        logging.debug("The current post is forwarded")
                        text = f"[from {event.message.forward.chat.title} (https://t.me/{event.message.forward.chat.username})]\n\n" + text
                    temp_file_path: str = ""
                    if (event.message.photo or event.message.video or event.message.gif) and not hasattr(event.message.media, "webpage"):
                        logging.debug("Post contains the media, downloading it...")
                        temp_file_name = uuid.uuid4()
                        temp_file_path = f"/tmp/{temp_file_name}{get_extension(event.message.media)}"
                        await self.tg_client.download_media(event.message.media, temp_file_path)
                    for mstdn_acc_name in self.tg_mstdn_mappings[channel.username]:
                        if self.mastodon_clients.get(mstdn_acc_name, None) is None:
                            logging.error(f"{mstdn_acc_name} doesn't exists in mastodon.accounts section of config!")
                            return
                        current_mastodon_client = self.mastodon_clients[mstdn_acc_name]
                        if temp_file_path != "":
                            mstdn_media_meta = current_mastodon_client.media_post(temp_file_path)
                            current_mastodon_client.status_post(text, media_ids=[mstdn_media_meta])
                        else:
                            current_mastodon_client.toot(text)
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
