import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

from musicbot.audio import bitrate_for, make_source

log = logging.getLogger(__name__)

# Delays between rejoin attempts after an unrequested voice drop; the last one
# repeats until we're back in or someone runs /ecaleave.
REJOIN_BACKOFF = (5, 15, 60, 300)


def _tracked_client(cog: "Voice") -> type[discord.VoiceClient]:
    class TrackedVoiceClient(discord.VoiceClient):
        async def on_voice_state_update(self, data) -> None:
            # Runs before discord.py's own handler, so we see its
            # expecting-disconnect flag before it gets reset.
            cog._on_own_voice_state(self, data)
            await super().on_voice_state_update(data)

    return TrackedVoiceClient


class Voice(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Per-guild flag: set before an intentional vc.stop()/disconnect so the
        # after-callback knows not to auto-restart.
        self._expect_stop: dict[int, bool] = {}
        # Per-guild voice channel we're supposed to be in, and the rejoin loop
        # running for it, if any. Set by /ecajoin; cleared by /ecaleave or by
        # someone disconnecting the bot. In-memory only, so a restart also
        # counts as leaving.
        self._target: dict[int, int] = {}
        self._rejoin_tasks: dict[int, asyncio.Task[None]] = {}
        self._client_cls = _tracked_client(self)

    def cog_unload(self) -> None:
        for task in self._rejoin_tasks.values():
            task.cancel()

    def _play(self, vc: discord.VoiceClient, gid: int) -> int:
        # play() builds a fresh encoder each call, so bitrate/FEC must be passed
        # here rather than set afterwards — otherwise restarts revert to 128/FEC.
        kbps = bitrate_for(vc.guild)
        vc.play(make_source(), after=self._make_after(vc, gid), bitrate=kbps, fec=False)
        return kbps

    def _make_after(self, vc: discord.VoiceClient, gid: int):
        def _after(err: Exception | None):
            if self._expect_stop.pop(gid, False):
                return
            if err:
                log.warning("playback ended with error: %r — auto-restarting", err)
            else:
                log.info("playback ended unexpectedly (clean EOF) — auto-restarting")
            if not vc.is_connected():
                # discord.py gives up after one failed voice handshake, so the
                # drop is ours to recover from. _after runs on the player
                # thread; hand off to the event loop.
                if gid in self._target:
                    self.bot.loop.call_soon_threadsafe(self._schedule_rejoin, gid)
                return
            try:
                self._play(vc, gid)
            except Exception as exc:
                log.warning("auto-restart failed: %r", exc)

        return _after

    def _on_own_voice_state(self, vc: discord.VoiceClient, data) -> None:
        gid = vc.guild.id
        if gid not in self._target:
            return
        channel_id = data.get("channel_id")
        if channel_id is None:
            # A drop discord.py started itself (failed reconnect) is expected;
            # anything else is a mod disconnect or the channel being deleted.
            # Default to "expected" so a discord.py rename can't silently
            # turn off rejoining.
            if not getattr(vc._connection, "_expecting_disconnect", True):
                log.info("disconnected from voice by someone else — not rejoining")
                self._target.pop(gid, None)
                self._cancel_rejoin(gid)
        elif int(channel_id) != self._target[gid]:
            log.info("moved to channel %s — rejoins will target it", channel_id)
            self._target[gid] = int(channel_id)

    def _schedule_rejoin(self, gid: int) -> None:
        task = self._rejoin_tasks.get(gid)
        if task is not None and not task.done():
            return
        self._rejoin_tasks[gid] = asyncio.create_task(self._rejoin(gid), name=f"voice-rejoin-{gid}")

    def _cancel_rejoin(self, gid: int) -> None:
        task = self._rejoin_tasks.pop(gid, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    async def _rejoin(self, gid: int) -> None:
        attempt = 0
        while gid in self._target:
            delay = REJOIN_BACKOFF[min(attempt, len(REJOIN_BACKOFF) - 1)]
            attempt += 1
            log.info("voice dropped — rejoin attempt %d in %ds", attempt, delay)
            await asyncio.sleep(delay)
            await self.bot.wait_until_ready()

            guild = self.bot.get_guild(gid)
            channel_id = self._target.get(gid)
            if guild is None or channel_id is None:
                return
            channel = guild.get_channel(channel_id)
            if not isinstance(channel, discord.VoiceChannel):
                log.warning("rejoin: channel %s is gone, giving up", channel_id)
                self._target.pop(gid, None)
                return

            vc: discord.VoiceClient | None = guild.voice_client  # type: ignore[assignment]
            if vc is not None and vc.is_connected():
                return
            if vc is not None:
                # Leftover client from the failed handshake would make
                # connect() raise "Already connected".
                await vc.disconnect(force=True)

            try:
                vc = await channel.connect(timeout=30.0, cls=self._client_cls)
            except Exception as exc:
                log.warning("rejoin attempt %d failed: %r", attempt, exc)
                stale = guild.voice_client
                if stale is not None:
                    await stale.disconnect(force=True)
                continue

            self._play(vc, gid)
            log.info("rejoined %s after %d attempt(s)", channel.name, attempt)
            return

    async def _resolve_channel(
        self,
        interaction: discord.Interaction,
        channel: discord.VoiceChannel | None,
    ) -> discord.VoiceChannel | None:
        if channel is None:
            member = interaction.user
            if not isinstance(member, discord.Member) or member.voice is None or member.voice.channel is None:
                await interaction.response.send_message(
                    "You're not in a voice channel — join one or pass `channel:`.",
                    ephemeral=True,
                )
                return None
            voice_channel = member.voice.channel
            if not isinstance(voice_channel, discord.VoiceChannel):
                await interaction.response.send_message(
                    "Your current voice channel isn't a regular voice channel.",
                    ephemeral=True,
                )
                return None
            channel = voice_channel

        assert interaction.guild is not None
        me = interaction.guild.me
        perms = channel.permissions_for(me)
        if not (perms.connect and perms.speak):
            await interaction.response.send_message(
                f"I lack Connect/Speak in {channel.mention}.",
                ephemeral=True,
            )
            return None
        return channel

    @app_commands.command(name="ecajoin", description="eca在听什么👀")
    @app_commands.describe(channel="Voice channel to join (defaults to yours).")
    async def ecajoin(
        self,
        interaction: discord.Interaction,
        channel: discord.VoiceChannel | None = None,
    ):
        if interaction.guild is None:
            await interaction.response.send_message("Guild-only command.", ephemeral=True)
            return

        target = await self._resolve_channel(interaction, channel)
        if target is None:
            return

        await interaction.response.defer(thinking=True)

        gid = interaction.guild.id
        self._cancel_rejoin(gid)
        self._target[gid] = target.id

        vc: discord.VoiceClient | None = interaction.guild.voice_client  # type: ignore[assignment]
        if vc is None:
            vc = await target.connect(cls=self._client_cls)
        elif vc.channel != target:
            await vc.move_to(target)
        if vc.is_playing():
            self._expect_stop[gid] = True
            vc.stop()

        kbps = self._play(vc, gid)

        await interaction.followup.send(
            f"Streaming → {target.mention} @ {kbps} kbps.",
        )

    @app_commands.command(name="ecaleave", description="不好听走了🚶")
    async def ecaleave(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("Guild-only command.", ephemeral=True)
            return
        gid = interaction.guild.id
        task = self._rejoin_tasks.get(gid)
        rejoining = task is not None and not task.done()
        self._target.pop(gid, None)
        self._cancel_rejoin(gid)
        vc: discord.VoiceClient | None = interaction.guild.voice_client  # type: ignore[assignment]
        if vc is None:
            msg = "Stopped trying to rejoin." if rejoining else "Not connected."
            await interaction.response.send_message(msg, ephemeral=True)
            return
        self._expect_stop[interaction.guild.id] = True
        await vc.disconnect(force=False)
        await interaction.response.send_message("Disconnected.")


async def setup(bot: commands.Bot):
    await bot.add_cog(Voice(bot))
