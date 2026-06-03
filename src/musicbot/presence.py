import asyncio
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass

import discord

log = logging.getLogger(__name__)

# Empty = pick whichever player is currently Playing. Set to a substring that
# matches either the MPRIS bus-name (e.g. "kdeconnect") OR the MPRIS Identity
# property (e.g. "WayDroid", "Apple Music"). Identity is set by the source app
# itself and survives KDE Connect hash rolls, so it's the more stable filter.
PLAYER_FILTER = os.environ.get("MPRIS_PLAYER", "").strip()
POLL_INTERVAL = float(os.environ.get("PRESENCE_POLL_SECONDS", "3"))
_STATUS_RANK = {"playing": 0, "paused": 1, "stopped": 2}


@dataclass(frozen=True)
class Track:
    status: str
    title: str
    artist: str

    def activity_name(self) -> str:
        name = self.title
        if self.artist:
            name = f"{self.title} — {self.artist}"
        return name[:128]


def _list_players() -> list[str]:
    res = subprocess.run(["playerctl", "--list-all"], capture_output=True, text=True)
    if res.returncode != 0:
        return []
    return [p for p in res.stdout.splitlines() if p.strip()]


def _read_identity(player: str) -> str:
    """MPRIS root Identity property (e.g. 'Apple Music - WayDroid'). '' on failure."""
    res = subprocess.run(
        [
            "busctl", "--user", "--no-pager", "get-property",
            f"org.mpris.MediaPlayer2.{player}",
            "/org/mpris/MediaPlayer2",
            "org.mpris.MediaPlayer2", "Identity",
        ],
        capture_output=True, text=True,
    )
    if res.returncode != 0:
        return ""
    # busctl format for a string: 's "the value"'
    line = res.stdout.strip()
    if line.startswith("s "):
        return line[2:].strip().strip('"')
    return ""


def _player_metadata(player: str) -> tuple[str, str, str] | None:
    """Return (status, title, artist) for a player, or None if no metadata."""
    res = subprocess.run(
        [
            "playerctl", f"--player={player}", "metadata",
            "--format", "{{status}}|{{title}}|{{artist}}",
        ],
        capture_output=True, text=True,
    )
    if res.returncode != 0 or not res.stdout.strip():
        return None
    parts = res.stdout.strip().split("|", 2)
    while len(parts) < 3:
        parts.append("")
    status, title, artist = (p.strip() for p in parts)
    if not title:
        return None
    return status, title, artist


def _read_metadata() -> Track | None:
    if not shutil.which("playerctl"):
        return None

    candidates = _list_players()
    if PLAYER_FILTER:
        # Match against bus-name or Identity — Identity is more stable.
        candidates = [p for p in candidates if PLAYER_FILTER in p or PLAYER_FILTER in _read_identity(p)]
    if not candidates:
        return None

    tracks: list[Track] = []
    for player in candidates:
        meta = _player_metadata(player)
        if meta is None:
            continue
        status, title, artist = meta
        tracks.append(Track(status=status, title=title, artist=artist))

    if not tracks:
        return None
    # Prefer Playing > Paused > Stopped; within a rank, take the first.
    tracks.sort(key=lambda t: _STATUS_RANK.get(t.status.lower(), 99))
    return tracks[0]


def _to_activity(track: Track | None) -> discord.Activity | None:
    if track is None or track.status.lower() != "playing":
        return None
    return discord.Activity(type=discord.ActivityType.listening, name=track.activity_name())


async def updater(bot: discord.Client) -> None:
    await bot.wait_until_ready()
    last_name: str | None = None
    while not bot.is_closed():
        try:
            track = await asyncio.to_thread(_read_metadata)
            activity = _to_activity(track)
            new_name = activity.name if activity else None
            if new_name != last_name:
                await bot.change_presence(activity=activity)
                log.info("presence → %s", new_name or "<cleared>")
                last_name = new_name
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("presence iteration failed: %r", exc)
        await asyncio.sleep(POLL_INTERVAL)
