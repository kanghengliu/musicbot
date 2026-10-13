"""Cloud standby for musicbot: keeps the bot sitting in its voice channel while
the local bot is down, and steps aside the moment the local bot comes back.

Two entry points:

  standby.py relay   Run over SSH by the local bot (src/musicbot/standby_link.py).
                     Copies each heartbeat line from stdin into the lease file.
  standby.py run     The standby itself (systemd service). Logs in with the same
                     bot token, watches the lease file and the bot's voice state.

The standby opens a real (silent, self-muted) audio connection when it takes
the slot, so Discord shows it as connected rather than with a connection-trouble
warning. Its voice client never sends a gateway leave and never reconnects on
its own, and drops its audio side quietly as soon as another session (the local
bot) takes the slot. If the audio handshake fails, it still holds the slot via
its gateway voice state, as it did before it had audio.

Rules, per guild with a target channel in the lease:
  - Lease fresh (local alive):   stay out. If we hold the channel, wait for the
                                 local bot to take it; leave only if the lease
                                 drops the target (/ecaleave, kicked).
  - Lease says "handoff":        join now; the local bot is shutting down cleanly
                                 and waits for us before it exits.
  - Lease stale (> STALE_AFTER): join. Local died without warning; Discord may
                                 still show its dead session in the channel, and
                                 our join takes the slot over before it expires.
  - Bot vanishes from voice under another session while the lease still has a
    target: join after ORPHAN_GRACE, unless the lease drops the target first
    (that's how a kick or /ecaleave looks from here).
  - Someone kicks us: stay out until the local bot is back.
  - Another session takes the channel from us: that's the local bot; stand down.
"""

import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import discord
from discord.voice_state import ConnectionFlowState, VoiceConnectionState

log = logging.getLogger("standby")

STATE_DIR = Path(os.environ.get("STANDBY_STATE_DIR") or Path.home() / ".local/state/musicbot-standby")
LEASE_FILE = STATE_DIR / "lease.json"

STALE_AFTER = float(os.environ.get("STANDBY_STALE_SECONDS", "30"))
ORPHAN_GRACE = 2.0
# A reconnect request older than this is stale (e.g. it sat in a dead SSH
# connection during an outage); the local bot re-sends it every few seconds
# while it waits.
BOUNCE_MAX_AGE = 5.0
# A join Discord hasn't echoed back by now went into a dead gateway socket;
# reconnect rather than wait out discord.py's ~60s heartbeat timeout.
JOIN_ACK_TIMEOUT = 3.0
# Minimum gap between such forced reconnects, in case Discord is ignoring the
# join for some other reason.
FORCED_RECONNECT_GAP = 20.0
# Local's own Discord connection, as its lease reports it: take over when it
# has been down this long, or silent this long while open (Discord acks a
# heartbeat every ~41s, so a live one never gets there). Both leave margin
# before Discord drops a dead session's voice state (~80s).
GATEWAY_DOWN_AFTER = 15.0
GATEWAY_SILENT_AFTER = 55.0
# Custom status shown while the local bot is down (status goes idle too).
SLEEP_STATUS = os.environ.get("STANDBY_STATUS", "💤 Sleeping")
TICK = 0.25
# Observe-only: log voice/timer events and decisions, never join or leave.
OBSERVE = os.environ.get("STANDBY_OBSERVE", "").strip() not in ("", "0")


def relay() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = LEASE_FILE.with_suffix(".tmp")
    while True:
        line = sys.stdin.readline()
        if not line:
            return
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        data["received_at"] = time.time()
        tmp.write_text(json.dumps(data))
        os.replace(tmp, LEASE_FILE)


class Lease:
    def __init__(self, data: dict):
        self.state: str = data.get("state", "alive")
        self.targets: dict[int, int] = {int(g): int(c) for g, c in (data.get("targets") or {}).items()}
        self.received_at: float = float(data.get("received_at", 0))
        self.bounce: str | None = data.get("bounce")
        # Guilds local explicitly let go of (/ecaleave, kicked, channel
        # deleted). A target merely missing never means leave.
        self.released: set[int] = {int(g) for g in data.get("released") or ()}
        self.gateway_open: bool = bool(data.get("gateway_open", True))
        self.gateway_silent: float = float(data.get("gateway_silent", 0.0))

    @property
    def fresh(self) -> bool:
        return time.time() - self.received_at < STALE_AFTER

    @property
    def gateway_dead(self) -> bool:
        # The silence kept growing since local reported it.
        silent = self.gateway_silent + max(0.0, time.time() - self.received_at)
        return silent >= (GATEWAY_SILENT_AFTER if self.gateway_open else GATEWAY_DOWN_AFTER)

    @classmethod
    def load(cls) -> "Lease":
        try:
            return cls(json.loads(LEASE_FILE.read_text()))
        except FileNotFoundError:
            return cls({})
        except (OSError, ValueError, TypeError) as exc:
            log.warning("unreadable lease: %r", exc)
            return cls({})


def _fmt_ts(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds") if ts else "RESET"


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


# How long an audio handshake may take after our join.
VOICE_CONNECT_TIMEOUT = 20.0


class HoldingConnectionState(VoiceConnectionState):
    # Set only when local explicitly released the channel.
    leaving = False
    _joined = False

    async def _voice_connect(self, *, self_deaf: bool = False, self_mute: bool = False) -> None:
        # Only the join we asked for. discord.py also re-sends the gateway join
        # on its own (resume after a 4015 close, a move mid-handshake) even
        # with reconnect=False; that would take the slot back from the local
        # bot. Failing as a timeout lands in its no-reconnect teardown.
        if self._joined:
            raise asyncio.TimeoutError("standby never re-joins on its own")
        self._joined = True
        await super()._voice_connect(self_deaf=self_deaf, self_mute=self_mute)

    async def _voice_disconnect(self) -> None:
        # discord.py sends a gateway leave when it tears down or gives up on a
        # voice connection. The voice state is per bot, so that would also
        # drop the local bot, or empty the channel if we're the holder.
        if self.leaving:
            await super()._voice_disconnect()
            return
        self.state = ConnectionFlowState.disconnected
        self._disconnected.set()


class HoldingVoiceClient(discord.VoiceClient):
    def create_connection_state(self) -> VoiceConnectionState:
        return HoldingConnectionState(self)

    async def on_voice_state_update(self, data) -> None:
        ws = self.client.ws
        own = ws.session_id if ws is not None else None
        if own is not None and data.get("session_id") not in (None, own):
            # Another session (the local bot) took the slot. Drop our audio
            # side without a leave, and keep discord.py from adopting the
            # foreign session.
            asyncio.create_task(drop_voice_client(self))
            return
        await super().on_voice_state_update(data)


async def drop_voice_client(vc: discord.VoiceClient) -> None:
    """Tear down our audio side without sending a gateway leave."""
    conn = vc._connection
    connector = conn._connector
    if connector is not None and not connector.done() and connector is not asyncio.current_task():
        connector.cancel()
        await asyncio.wait([connector])
    await conn.soft_disconnect(with_state=ConnectionFlowState.disconnected)
    conn._socket_reader.stop()
    if vc.guild.voice_client is vc:
        vc.cleanup()


class Standby(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        super().__init__(intents=intents)
        # gid -> lease.received_at when we were kicked; cleared once the local
        # bot is heard from again.
        self._kicked: dict[int, float] = {}
        # gid -> monotonic time the local bot took the channel from us. Proof
        # it's alive even if its lease is stale (e.g. SSH path broken), but
        # only for STALE_AFTER: it says nothing about whether local died since.
        self._displaced: dict[int, float] = {}
        # gid -> monotonic time the bot left voice under the local's session.
        self._orphaned: dict[int, float] = {}
        self._leaving: set[int] = set()
        self._next_attempt: dict[int, float] = {}
        self._failures: dict[int, int] = {}
        self._last_reason: dict[int, str] = {}
        # Last bounce request handled; seeded from the lease at startup so an
        # old request isn't replayed.
        self._bounce_seen: str | None = Lease.load().bounce
        self._bounce_pending: set[int] = set()
        # gid -> monotonic time of a join Discord hasn't echoed back yet.
        self._join_sent: dict[int, float] = {}
        # gid -> task running channel.connect() for our audio side.
        self._voice_tasks: dict[int, asyncio.Task[None]] = {}
        self._forced_reconnect = -FORCED_RECONNECT_GAP
        self._sleeping = False

    async def setup_hook(self) -> None:
        self._connection.parsers["VOICE_CHANNEL_START_TIME_UPDATE"] = self._on_voice_timer
        track_voice_sessions(self)
        self.loop.create_task(self._tick_loop())

    def _on_voice_timer(self, data) -> None:
        log.info("voice timer: channel %s start_time=%s", data.get("id"), _fmt_ts(data.get("voice_start_time")))

    @property
    def _session(self) -> str | None:
        return self.ws.session_id if self.ws is not None else None

    def _holding(self, guild: discord.Guild) -> discord.VoiceChannel | None:
        state = guild.me.voice
        if state is None or state.channel is None or state.session_id != self._session:
            return None
        return state.channel  # type: ignore[return-value]

    async def on_ready(self) -> None:
        log.info("logged in as %s, session %s%s", self.user, self._session, " (observe-only)" if OBSERVE else "")
        for guild in self.guilds:
            state = guild.me.voice
            if state is not None and state.channel is not None:
                log.info("bot is in %s/%s under session %s", guild.name, state.channel.name, state.session_id)
        self._retry_joins()

    async def on_resumed(self) -> None:
        self._retry_joins()

    def _retry_joins(self) -> None:
        # Joins sent before a reconnect may have been lost; send them again
        # on the next tick rather than after their backoff.
        self._join_sent.clear()
        self._next_attempt.clear()

    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if self.user is None or member.id != self.user.id:
            return
        gid = member.guild.id
        mine = self._session
        lease = Lease.load()
        log.info(
            "bot voice: %s -> %s (session %s%s)",
            getattr(before.channel, "name", None), getattr(after.channel, "name", None),
            after.session_id, ", ours" if after.session_id == mine else "",
        )
        if after.channel is None:
            if after.session_id == mine:
                if gid in self._leaving:
                    self._leaving.discard(gid)
                else:
                    log.info("we were disconnected by someone — staying out until local is back")
                    self._kicked[gid] = lease.received_at
            elif gid in lease.targets:
                self._orphaned[gid] = time.monotonic()
            return
        self._orphaned.pop(gid, None)
        if after.session_id == mine:
            self._join_sent.pop(gid, None)
            # Our join landed; a reconnect hold is served even if the local
            # bot takes the slot back before the next tick sees us holding it.
            self._bounce_pending.discard(gid)
        if after.session_id != mine and before.session_id == mine and before.channel is not None:
            log.info("local bot took the channel — standing down")
            self._displaced[gid] = time.monotonic()
            self._bounce_pending.discard(gid)

    async def _tick_loop(self) -> None:
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                await self._tick()
            except Exception:
                log.exception("tick failed")
            await asyncio.sleep(TICK)

    def _decide(self, gid: int, lease: Lease) -> tuple[bool, str]:
        target = lease.targets.get(gid)
        if target is None:
            return False, "no target"
        if gid in self._kicked:
            return False, "kicked"
        if lease.state == "handoff":
            return True, "local handed off"
        if gid in self._bounce_pending:
            return True, "local is reconnecting"
        if lease.fresh and lease.gateway_dead:
            return True, "local's Discord connection is down"
        if not lease.fresh and time.monotonic() - self._displaced.get(gid, -1e9) >= STALE_AFTER:
            return True, f"lease stale (>{STALE_AFTER:.0f}s)"
        orphaned = self._orphaned.get(gid)
        if orphaned is not None and time.monotonic() - orphaned >= ORPHAN_GRACE:
            return True, "bot left voice under local's session"
        return False, "local alive"

    async def _tick(self) -> None:
        lease = Lease.load()
        for gid, at in list(self._kicked.items()):
            # Only a genuinely healthy local clears it; a fresh lease alone
            # doesn't, or the gateway-dead rule would rejoin after a kick.
            if lease.fresh and lease.state == "alive" and not lease.gateway_dead and lease.received_at > at:
                del self._kicked[gid]
        for gid, at in list(self._displaced.items()):
            if time.monotonic() - at >= STALE_AFTER:
                del self._displaced[gid]
        await self._update_presence(lease)
        if self.is_closed():
            return
        self._check_join_acks()
        if lease.bounce != self._bounce_seen:
            self._bounce_seen = lease.bounce
            try:
                age = time.time() - float(lease.bounce or 0)
            except ValueError:
                age = float("inf")
            if age > BOUNCE_MAX_AGE:
                if lease.bounce is not None:
                    log.info("ignoring a reconnect request from %.0fs ago", age)
            elif lease.fresh:
                # Local is re-handshaking voice: hold the slot so Discord hands
                # it a fresh voice server when it joins back.
                for gid in lease.targets:
                    self._bounce_pending.add(gid)
                    self._next_attempt.pop(gid, None)

        for guild in self.guilds:
            gid = guild.id
            holding = self._holding(guild)
            if gid not in lease.targets and holding is None:
                self._orphaned.pop(gid, None)
                continue
            want, reason = self._decide(gid, lease)
            if self._last_reason.get(gid) != reason:
                log.info("%s: %s → %s", guild.name, reason, "hold channel" if want else "stay out")
                self._last_reason[gid] = reason
            if OBSERVE:
                continue

            target = lease.targets.get(gid)
            if holding is not None and holding.id == target:
                self._failures.pop(gid, None)
                self._bounce_pending.discard(gid)
            if want and target is not None and (holding is None or holding.id != target):
                await self._join(guild, target)
            elif not want and holding is not None and lease.fresh and target is None and gid in lease.released:
                log.info("local dropped the target — leaving %s", holding.name)
                self._leaving.add(gid)
                await self._drop_voice(guild)
                await guild.change_voice_state(channel=None)

    def _check_join_acks(self) -> None:
        now = time.monotonic()
        late = [gid for gid, at in self._join_sent.items() if now - at > JOIN_ACK_TIMEOUT]
        if not late or self.ws is None:
            return
        self._join_sent.clear()
        if now - self._forced_reconnect < FORCED_RECONNECT_GAP:
            return
        self._forced_reconnect = now
        log.warning("join not acknowledged within %.0fs — gateway looks dead, reconnecting", JOIN_ACK_TIMEOUT)
        # Same as discord.py's heartbeat timeout: close with 4000 and resume.
        # In a task, since closing a dead socket can take a while.
        asyncio.create_task(self.ws.close(4000))

    async def _update_presence(self, lease: Lease) -> None:
        # Only while local is down. Discord keeps showing a status this
        # session set even after the local bot sets its own, and changing it
        # back doesn't hand the display over either; a fresh login with no
        # status does (that's how the standby ran before it had one). So once
        # local has the channel back, restart (systemd Restart=always).
        local_down = lease.state == "handoff" or not lease.fresh
        if local_down:
            if not self._sleeping:
                log.info("local is down — status: %s", SLEEP_STATUS)
                if not OBSERVE:
                    await self.change_presence(status=discord.Status.idle, activity=discord.CustomActivity(name=SLEEP_STATUS))
                self._sleeping = True
        elif self._sleeping and self._local_holds_all(lease):
            log.info("local is back and holds the channel — restarting to drop our status")
            await self.close()

    def _local_holds_all(self, lease: Lease) -> bool:
        # Positively: another session in each target channel, and no join of
        # ours in flight that could still land and leave us holding it.
        if self._join_sent or self._bounce_pending:
            return False
        for gid, channel_id in lease.targets.items():
            guild = self.get_guild(gid)
            state = guild.me.voice if guild is not None else None
            if state is None or state.channel is None or state.channel.id != channel_id or state.session_id == self._session:
                return False
        return True

    async def close(self) -> None:
        # discord.py closes with 1000, which ends the session and drops its
        # voice state on the spot. Our cache can't prove we don't hold the
        # slot (a join may have landed unechoed), so always close resumably:
        # Discord keeps any voice state like after a crash until local takes
        # it or our next login does, and an idle session just expires.
        ws = self.ws
        if ws is not None:
            close = ws.close

            async def close_resumable(code: int = 4000) -> None:
                await close(4000)

            ws.close = close_resumable  # type: ignore[method-assign]
        await super().close()

    async def _join(self, guild: discord.Guild, channel_id: int) -> None:
        gid = guild.id
        now = time.monotonic()
        if now < self._next_attempt.get(gid, 0):
            return
        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.VoiceChannel):
            log.warning("target channel %s not found", channel_id)
            self._next_attempt[gid] = now + 60
            return
        failures = self._failures.get(gid, 0)
        # Capped well under the ~80s a dead holder lingers before Discord
        # drops it.
        self._next_attempt[gid] = now + min(10, 5 * 2**failures)
        self._failures[gid] = failures + 1
        log.info("joining %s", channel.name)
        await self._drop_voice(guild)
        self._join_sent.setdefault(gid, now)
        # The join itself goes out as part of the connect; run it aside so a
        # slow handshake doesn't hold up the tick loop.
        self._voice_tasks[gid] = asyncio.create_task(self._connect_voice(channel), name=f"standby-voice-{gid}")

    async def _connect_voice(self, channel: discord.VoiceChannel) -> None:
        try:
            await channel.connect(
                cls=HoldingVoiceClient, reconnect=False, timeout=VOICE_CONNECT_TIMEOUT,
                self_mute=True, self_deaf=True,
            )
            log.info("audio connected in %s", channel.name)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # The join may still have landed: we hold the slot, just without
            # audio (Discord shows the warning icon). Retrying would need a
            # leave, since Discord ignores a join to the channel we're in.
            log.warning("audio connect in %s failed: %r — holding without audio", channel.name, exc)
        finally:
            vc = channel.guild.voice_client
            if isinstance(vc, discord.VoiceClient) and not vc.is_connected():
                await drop_voice_client(vc)

    async def _drop_voice(self, guild: discord.Guild) -> None:
        task = self._voice_tasks.pop(guild.id, None)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.wait([task])
        vc = guild.voice_client
        if isinstance(vc, discord.VoiceClient):
            await drop_voice_client(vc)


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "relay":
        relay()
        return
    if cmd != "run":
        raise SystemExit(f"usage: {sys.argv[0]} [run|relay]")
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise SystemExit("DISCORD_TOKEN missing")
    Standby().run(token, log_handler=None)


if __name__ == "__main__":
    main()
