"""Cloud standby for musicbot: keeps the bot sitting in its voice channel while
the local bot is down, and steps aside the moment the local bot comes back.

Two entry points:

  standby.py relay   Run over SSH by the local bot (src/musicbot/standby_link.py).
                     Copies each heartbeat line from stdin into the lease file.
  standby.py run     The standby itself (systemd service). Logs in with the same
                     bot token, watches the lease file and the bot's voice state.

The standby only sends gateway voice-state updates (join/leave); it never opens
an audio connection, so it can't fight the local bot's audio connection.

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

log = logging.getLogger("standby")

STATE_DIR = Path(os.environ.get("STANDBY_STATE_DIR") or Path.home() / ".local/state/musicbot-standby")
LEASE_FILE = STATE_DIR / "lease.json"

STALE_AFTER = float(os.environ.get("STANDBY_STALE_SECONDS", "30"))
ORPHAN_GRACE = 2.0
TICK = 1.0
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

    @property
    def fresh(self) -> bool:
        return time.time() - self.received_at < STALE_AFTER

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


class Standby(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        super().__init__(intents=intents)
        # gid -> lease.received_at when we were kicked; cleared once the local
        # bot is heard from again.
        self._kicked: dict[int, float] = {}
        # gid -> lease.received_at when the local bot took the channel from us.
        # Proof it's alive even if its lease is stale (e.g. SSH path broken).
        self._displaced: dict[int, float] = {}
        # gid -> monotonic time the bot left voice under the local's session.
        self._orphaned: dict[int, float] = {}
        self._leaving: set[int] = set()
        self._next_attempt: dict[int, float] = {}
        self._failures: dict[int, int] = {}
        self._last_reason: dict[int, str] = {}

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
        if after.session_id != mine and before.session_id == mine and before.channel is not None:
            log.info("local bot took the channel — standing down")
            self._displaced[gid] = lease.received_at

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
        if not lease.fresh and gid not in self._displaced:
            return True, f"lease stale (>{STALE_AFTER:.0f}s)"
        orphaned = self._orphaned.get(gid)
        if orphaned is not None and time.monotonic() - orphaned >= ORPHAN_GRACE:
            return True, "bot left voice under local's session"
        return False, "local alive"

    async def _tick(self) -> None:
        lease = Lease.load()
        for gid, at in list(self._kicked.items()):
            if lease.fresh and lease.state == "alive" and lease.received_at > at:
                del self._kicked[gid]
        for gid, at in list(self._displaced.items()):
            if lease.received_at > at:
                del self._displaced[gid]

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
            if want and target is not None and (holding is None or holding.id != target):
                await self._join(guild, target)
            elif not want and holding is not None and lease.fresh and target is None:
                log.info("local dropped the target — leaving %s", holding.name)
                self._leaving.add(gid)
                await guild.change_voice_state(channel=None)

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
        self._next_attempt[gid] = now + min(60, 5 * 2**failures)
        self._failures[gid] = failures + 1
        log.info("joining %s", channel.name)
        await guild.change_voice_state(channel=channel, self_mute=True, self_deaf=True)


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
