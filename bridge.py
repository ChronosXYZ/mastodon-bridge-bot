import asyncio
import toml
import argparse
from mastodon import Mastodon
from telethon import TelegramClient, events
from telethon.events.common import EventBuilder
from telethon.tl.functions.channels import JoinChannelRequest, GetMessagesRequest
from telethon.tl.functions.contacts import ResolveUsernameRequest
from telethon.tl.types import InputChannel


class BridgeBot:
    def __init__(self, cfg: dict):
        self.config = cfg
        self.mastodon_client = Mastodon(
            client_id=cfg["mastodon"]["client_id"],
            client_secret=cfg["mastodon"]["client_secret"],
            access_token=cfg["mastodon"]["access_token"],
            api_base_url=cfg["mastodon"]["api_base_url"]
        )
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
        await self.tg_client.run_until_disconnected()

    @events.register(events.NewMessage())
    async def _tg_event_handler(self, event: events.NewMessage.Event):
        if event.message.post:
            channel = await event.get_chat()
            if channel.broadcast:
                if channel.username in self.config["telegram"]["channels"]:
                    print("dobbry vechur")
                    self.mastodon_client.toot(event.message.text)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=str,
                        help='A path to bot configuration')
    args = parser.parse_args()

    config: dict = toml.loads(open(args.config, "r").read())
    print(config)
    bot = BridgeBot(config)
    asyncio.get_event_loop().run_until_complete(bot.run())
