import asyncio
import json
import logging
import os
import time
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
from discord.voice_state import ConnectionFlowState, VoiceConnectionState

from musicbot.audio import bitrate_for, make_source

log = logging.getLogger(__name__)

# Delays between rejoin attempts after an unrequested voice drop; the last one
# repeats until we're back in or someone runs /ecaleave.
REJOIN_BACKOFF = (5, 15, 60, 300)

# Voice target per guild, kept across restarts so a reboot isn't a leave.
STATE_FILE = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "musicbot/voice.json"

# How long a clean shutdown waits for the cloud standby to take the channel.
HANDOFF_TIMEOUT = 10
# How long a reconnect waits for the standby to hold the slot.
BOUNCE_TIMEOUT = 3


def _tracked_client(cog: "Voice") -> type[discord.VoiceClient]:
    class GuardedConnectionState(VoiceConnectionState):
        # Set by /ecaleave: the one time a real leave is wanted.
        leaving = False

        async def _voice_disconnect(self) -> None:
            # discord.py sends a gateway leave on every retry and reconnect
            # (then joins again), and when it gives up. That empties the
            # channel for a moment, resetting its timer, or kicks the standby
            # if it holds the channel. Simply skipping it doesn't work either:
            # Discord ignores a join to the channel a session is already in,
            # so the retry never gets a voice server. Instead the standby takes
            # the slot, and our retry's join moves it back with a fresh server.
            if not self.leaving and await cog._hold_slot_for_reconnect(self.voice_client.guild):
                self.state = ConnectionFlowState.disconnected
                self._disconnected.set()
                return
            await super()._voice_disconnect()

    class TrackedVoiceClient(discord.VoiceClient):
        def create_connection_state(self) -> VoiceConnectionState:
            return GuardedConnectionState(self)

        async def on_voice_state_update(self, data) -> None:
            # Runs before discord.py's own handler, so we see its
            # expecting-disconnect flag before it gets reset.
            if cog._on_own_voice_state(self, data):
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
        # someone disconnecting the bot. Persisted, so restarts rejoin.
        self._target: dict[int, int] = self._load_targets()
        self._rejoin_tasks: dict[int, asyncio.Task[None]] = {}
        self._client_cls = _tracked_client(self)
        # Cloud standby lease (None when STANDBY_SSH_HOST isn't set).
        self._link = getattr(bot, "standby", None)
        if self._link is not None:
            self._link.set_targets(self._target)
        self._started = False
        self._handing_off = False
        # gid -> monotonic time we asked the standby to hold the slot for a
        # reconnect; its voice updates are expected until ours come back.
        self._bouncing: dict[int, float] = {}

    def cog_unload(self) -> None:
        for task in self._rejoin_tasks.values():
            task.cancel()

    @staticmethod
    def _load_targets() -> dict[int, int]:
        try:
            raw = json.loads(STATE_FILE.read_text())
            return {int(gid): int(cid) for gid, cid in raw.items()}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, AttributeError) as exc:
            log.warning("ignoring unreadable %s: %r", STATE_FILE, exc)
            return {}

    def _set_target(self, gid: int, channel_id: int | None) -> None:
        if channel_id is None:
            self._target.pop(gid, None)
        else:
            self._target[gid] = channel_id
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps({str(g): c for g, c in self._target.items()}))
            tmp.replace(STATE_FILE)
        except OSError as exc:
            log.warning("couldn't save %s: %r", STATE_FILE, exc)
        if self._link is not None:
            self._link.set_targets(self._target)

    @staticmethod
    async def _drop_client(vc: discord.VoiceClient) -> None:
        # Tear down our end without sending a leave: the gateway voice state is
        # per-bot, so a leave would also kick the standby's session out.
        await vc._connection.soft_disconnect()
        vc.cleanup()

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

    def _on_own_voice_state(self, vc: discord.VoiceClient, data) -> bool:
        """Returns whether discord.py's own handler should see the update."""
        gid = vc.guild.id
        channel_id = data.get("channel_id")
        own = self.bot.ws.session_id if self.bot.ws is not None else None
        if own is not None and data.get("session_id") not in (None, own):
            # The update belongs to another login of this bot — the cloud
            # standby. Keep it away from discord.py, which would adopt the
            # foreign session.
            bouncing = time.monotonic() - self._bouncing.get(gid, -1e9) < 60
            if channel_id is not None and not self._handing_off and not bouncing:
                log.info("standby took the voice channel while we're up — taking it back")
                asyncio.create_task(self._reclaim(vc, gid))
            return False
        if channel_id is not None:
            self._bouncing.pop(gid, None)
        if gid not in self._target:
            return True
        if channel_id is None:
            # A drop discord.py started itself (failed reconnect) is expected;
            # anything else is a mod disconnect or the channel being deleted.
            # Default to "expected" so a discord.py rename can't silently
            # turn off rejoining.
            if not getattr(vc._connection, "_expecting_disconnect", True):
                log.info("disconnected from voice by someone else — not rejoining")
                self._set_target(gid, None)
                self._cancel_rejoin(gid)
        elif int(channel_id) != self._target[gid]:
            log.info("moved to channel %s — rejoins will target it", channel_id)
            self._set_target(gid, int(channel_id))
        return True

    async def _hold_slot_for_reconnect(self, guild: discord.Guild) -> bool:
        """True if discord.py's leave can be skipped: someone else holds the
        channel, or the standby just took it for us. False = really leave."""
        own = self.bot.ws.session_id if self.bot.ws is not None else None
        state = guild.me.voice
        if state is None or state.channel is None:
            return True
        if state.session_id != own:
            return True
        link = self._link
        if link is None or not link.healthy:
            log.info("reconnecting without a standby — leaving first (channel timer may reset)")
            return False
        self._bouncing[guild.id] = time.monotonic()
        if not await link.bounce():
            return False
        deadline = time.monotonic() + BOUNCE_TIMEOUT
        while time.monotonic() < deadline:
            state = guild.me.voice
            if state is not None and state.channel is not None and state.session_id != own:
                log.info("standby holds the slot while we reconnect")
                return True
            await asyncio.sleep(0.05)
        log.warning("standby didn't take the slot within %ds — leaving to reconnect", BOUNCE_TIMEOUT)
        return False

    async def _reclaim(self, vc: discord.VoiceClient, gid: int) -> None:
        if vc.is_playing():
            self._expect_stop[gid] = True
            vc.stop()
        await self._drop_client(vc)
        self._schedule_rejoin(gid)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        # on_ready repeats after full reconnects; startup adoption runs once.
        if self._started:
            return
        self._started = True
        own = self.bot.ws.session_id if self.bot.ws is not None else None
        for guild in self.bot.guilds:
            state = guild.me.voice
            if state is None or not isinstance(state.channel, discord.VoiceChannel):
                continue
            # Fresh process, so any voice state is someone else's: the cloud
            # standby, or a ghost of our previous run. Take over its channel.
            log.info(
                "bot already in %s (session %s, ours %s) — taking it over",
                state.channel.name, state.session_id, own,
            )
            self._set_target(guild.id, state.channel.id)
        for gid in list(self._target):
            self._schedule_rejoin(gid, first_delay=0)

    def _schedule_rejoin(self, gid: int, first_delay: float | None = None) -> None:
        if self._handing_off:
            return
        task = self._rejoin_tasks.get(gid)
        if task is not None and not task.done():
            return
        self._rejoin_tasks[gid] = asyncio.create_task(
            self._rejoin(gid, first_delay), name=f"voice-rejoin-{gid}"
        )

    def _cancel_rejoin(self, gid: int) -> None:
        task = self._rejoin_tasks.pop(gid, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    async def _rejoin(self, gid: int, first_delay: float | None = None) -> None:
        attempt = 0
        while gid in self._target:
            if attempt == 0 and first_delay is not None:
                delay = first_delay
            else:
                delay = REJOIN_BACKOFF[min(attempt, len(REJOIN_BACKOFF) - 1)]
            attempt += 1
            if delay:
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
                self._set_target(gid, None)
                return

            vc: discord.VoiceClient | None = guild.voice_client  # type: ignore[assignment]
            if vc is not None and vc.is_connected():
                return
            if vc is not None:
                # Leftover client from the failed handshake would make
                # connect() raise "Already connected".
                await self._drop_client(vc)

            try:
                vc = await channel.connect(timeout=30.0, cls=self._client_cls)
            except Exception as exc:
                log.warning("rejoin attempt %d failed: %r", attempt, exc)
                stale = guild.voice_client
                if isinstance(stale, discord.VoiceClient):
                    await self._drop_client(stale)
                continue

            self._play(vc, gid)
            log.info("joined %s after %d attempt(s)", channel.name, attempt)
            return

    async def handoff(self) -> None:
        """On shutdown, pass the channel to the cloud standby without leaving.

        Our voice client is torn down quietly (no leave), the standby is told
        to join, and we wait until Discord shows its session in the channel,
        so the channel never empties. Without a standby this is a no-op and
        the normal close leaves voice.
        """
        clients = [vc for vc in self.bot.voice_clients if isinstance(vc, discord.VoiceClient)]
        if self._link is None or not self._target or not clients:
            return
        self._handing_off = True
        for gid in list(self._rejoin_tasks):
            self._cancel_rejoin(gid)
        for vc in clients:
            self._expect_stop[vc.guild.id] = True
            vc.stop()
            await self._drop_client(vc)
        if not await self._link.handoff():
            log.warning("handoff: standby link is down; leaving the voice state for it to take over")
            return

        own = self.bot.ws.session_id if self.bot.ws is not None else None

        def taken(gid: int) -> bool:
            guild = self.bot.get_guild(gid)
            state = guild.me.voice if guild is not None else None
            return state is not None and state.channel is not None and state.session_id != own

        deadline = time.monotonic() + HANDOFF_TIMEOUT
        while time.monotonic() < deadline:
            if all(taken(gid) for gid in self._target):
                log.info("handoff: standby holds the channel")
                return
            await asyncio.sleep(0.2)
        log.warning("handoff: standby didn't take over within %ds", HANDOFF_TIMEOUT)

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
        self._set_target(gid, target.id)

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
        self._set_target(gid, None)
        self._cancel_rejoin(gid)
        vc: discord.VoiceClient | None = interaction.guild.voice_client  # type: ignore[assignment]
        if vc is not None:
            self._expect_stop[gid] = True
            vc._connection.leaving = True  # type: ignore[attr-defined]
            await vc.disconnect(force=False)
        # The standby or a dead session of ours may still hold the channel;
        # an explicit leave clears whichever session it is.
        held = interaction.guild.me.voice is not None and interaction.guild.me.voice.channel is not None
        if held:
            await interaction.guild.change_voice_state(channel=None)
        if vc is None and not held:
            msg = "Stopped trying to rejoin." if rejoining else "Not connected."
            await interaction.response.send_message(msg, ephemeral=True)
            return
        await interaction.response.send_message("Disconnected.")


async def setup(bot: commands.Bot):
    await bot.add_cog(Voice(bot))
