import asyncio
import logging
import os
import signal
from datetime import datetime

import discord
from discord.ext import commands

from musicbot import presence, routing
from musicbot.standby_link import StandbyLink

log = logging.getLogger("musicbot")


def log_voice_timer(data) -> None:
    # discord.py has no parser for this event; it's how Discord publishes the
    # voice channel's "active for N minutes" clock. null = the clock reset.
    start = data.get("voice_start_time")
    started = datetime.fromtimestamp(start).astimezone().isoformat(timespec="seconds") if start else None
    log.info("voice timer: channel %s start_time=%s", data.get("id"), started or "RESET")


def track_voice_sessions(client: discord.Client) -> None:
    """Keep the bot's cached VoiceState.session_id current.

    discord.py only reads session_id when a voice state is first cached, so
    when another login of this bot takes the channel over, guild.me.voice
    keeps the old session. Patch it from the raw event; listeners run after
    the parser, so their `after` sees the fixed value too.
    """
    parsers = client._connection.parsers
    original = parsers["VOICE_STATE_UPDATE"]

    def parse(data) -> None:
        original(data)
        if client.user is None or int(data["user_id"]) != client.user.id or not data.get("channel_id"):
            return
        guild = client.get_guild(int(data["guild_id"])) if data.get("guild_id") else None
        state = guild.me.voice if guild is not None else None
        if state is not None:
            state.session_id = data.get("session_id")

    parsers["VOICE_STATE_UPDATE"] = parse


class MusicBot(commands.Bot):
    def __init__(self, guild_ids: list[int]):
        intents = discord.Intents.default()
        intents.voice_states = True
        super().__init__(command_prefix="!", intents=intents)
        self.guild_ids = guild_ids
        self._routing_task: asyncio.Task[None] | None = None
        self._presence_task: asyncio.Task[None] | None = None
        self.standby = StandbyLink.from_env()

    async def setup_hook(self) -> None:
        self._connection.parsers["VOICE_CHANNEL_START_TIME_UPDATE"] = log_voice_timer
        track_voice_sessions(self)
        if self.standby is not None:
            self.standby.start()
        await self.load_extension("musicbot.cogs.voice")
        await self.load_extension("musicbot.cogs.control")
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
        voice = self.get_cog("Voice")
        if voice is not None:
            try:
                await voice.handoff()
            except Exception:
                log.exception("standby handoff failed")
        if self.standby is not None:
            await self.standby.stop()
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
        stop_task = asyncio.create_task(stop.wait())
        # Watch both: if the bot dies on its own (e.g. login fails on DNS at boot),
        # exit non-zero so systemd's Restart=on-failure kicks in instead of idling.
        await asyncio.wait({bot_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        if not stop_task.done():
            stop_task.cancel()
            exc = bot_task.exception()
            if exc is not None:
                raise exc
            raise SystemExit("bot stopped unexpectedly")
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
