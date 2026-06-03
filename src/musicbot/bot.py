import asyncio
import logging
import os
import signal

import discord
from discord.ext import commands

from musicbot import presence, routing

log = logging.getLogger("musicbot")


class MusicBot(commands.Bot):
    def __init__(self, guild_ids: list[int]):
        intents = discord.Intents.default()
        intents.voice_states = True
        super().__init__(command_prefix="!", intents=intents)
        self.guild_ids = guild_ids
        self._routing_task: asyncio.Task[None] | None = None
        self._presence_task: asyncio.Task[None] | None = None

    async def setup_hook(self) -> None:
        await self.load_extension("musicbot.cogs.voice")
        if self.guild_ids:
            for gid in self.guild_ids:
                guild = discord.Object(id=gid)
                self.tree.copy_global_to(guild=guild)
                try:
                    synced = await self.tree.sync(guild=guild)
                except discord.Forbidden:
                    log.warning("guild %s: missing access (bot not invited?), skipping", gid)
                    continue
                log.info("synced %d commands to guild %s", len(synced), gid)
        else:
            synced = await self.tree.sync()
            log.info("synced %d global commands (may take up to 1h to appear)", len(synced))

        self._routing_task = asyncio.create_task(routing.reconciler(), name="pw-route-reconciler")
        self._presence_task = asyncio.create_task(presence.updater(self), name="presence-updater")

    async def close(self) -> None:
        for task in (self._routing_task, self._presence_task):
            if task is not None:
                task.cancel()
        await super().close()

    async def on_ready(self):
        log.info("logged in as %s (id=%s)", self.user, self.user.id if self.user else "?")


async def _amain() -> None:
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise SystemExit("DISCORD_TOKEN missing — copy .env.example to .env and fill it in.")
    guild_ids_raw = os.environ.get("GUILD_IDS", "").strip()
    guild_ids = [int(x.strip()) for x in guild_ids_raw.split(",") if x.strip()]

    bot = MusicBot(guild_ids=guild_ids)

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    async with bot:
        bot_task = asyncio.create_task(bot.start(token))
        await stop.wait()
        log.info("shutdown signal received")
        await bot.close()
        await bot_task


def run() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        pass
