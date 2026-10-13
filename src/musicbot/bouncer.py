"""A throwaway second login of the bot that holds its voice slot while the main
session re-handshakes voice, for when the cloud standby doesn't take it.

Same trick as the standby: another session of the bot joining moves its voice
state over, so the channel never empties, and the main session's join moves it
back with a fresh voice server.
"""

import asyncio
import logging
from collections.abc import Callable

import discord

from musicbot.bot import keep_session_on_close, track_voice_sessions

log = logging.getLogger(__name__)

LOGIN_TIMEOUT = 60
JOIN_TIMEOUT = 10


class LocalBouncer:
    def __init__(self, token: str, guild_id: int, channel_id: int, needed: Callable[[], bool]):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        self.client = discord.Client(intents=intents)
        track_voice_sessions(self.client)
        self.token = token
        self.guild_id = guild_id
        self.channel_id = channel_id
        # Checked before joining: the reconnect may have finished while we
        # logged in, and joining then would pull the slot from it.
        self.needed = needed
        self._task: asyncio.Task[None] | None = None

    @property
    def holding(self) -> bool:
        ws = self.client.ws
        guild = self.client.get_guild(self.guild_id)
        state = guild.me.voice if guild is not None else None
        return (
            ws is not None and state is not None and state.channel is not None
            and state.session_id == ws.session_id
        )

    async def join(self) -> bool:
        """Log in and take the slot; True once Discord shows us holding it."""
        await self.client.login(self.token)
        self._task = asyncio.create_task(self.client.connect(), name="voice-bouncer")
        await asyncio.wait_for(self.client.wait_until_ready(), LOGIN_TIMEOUT)
        guild = self.client.get_guild(self.guild_id)
        channel = guild.get_channel(self.channel_id) if guild is not None else None
        if guild is None or not isinstance(channel, discord.VoiceChannel) or not self.needed():
            return False
        await guild.change_voice_state(channel=channel, self_mute=True, self_deaf=True)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + JOIN_TIMEOUT
        while not self.holding and loop.time() < deadline:
            await asyncio.sleep(0.05)
        return self.holding

    async def close(self) -> None:
        if self.holding:
            # Still the only one in the slot (e.g. we're shutting down): don't
            # let our close empty the channel.
            keep_session_on_close(self.client)
        await self.client.close()
        if self._task is not None:
            try:
                await self._task
            except Exception:
                pass
