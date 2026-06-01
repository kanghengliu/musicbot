import asyncio
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass

import discord

log = logging.getLogger(__name__)

# Empty = pick the first available MPRIS player. Set to a specific player name
# (substring match) to pin to one — useful when multiple players are active.
PLAYER_FILTER = os.environ.get("MPRIS_PLAYER", "").strip()
POLL_INTERVAL = float(os.environ.get("PRESENCE_POLL_SECONDS", "3"))


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


def _read_metadata() -> Track | None:
    if not shutil.which("playerctl"):
        return None

    players = _list_players()
    if PLAYER_FILTER:
        players = [p for p in players if PLAYER_FILTER in p]
    if not players:
        return None

    for player in players:
        res = subprocess.run(
            [
                "playerctl",
                f"--player={player}",
                "metadata",
                "--format",
                "{{status}}|{{title}}|{{artist}}",
            ],
            capture_output=True,
            text=True,
        )
        if res.returncode != 0:
            continue
        line = res.stdout.strip()
        if not line:
            continue
        parts = line.split("|", 2)
        while len(parts) < 3:
            parts.append("")
        status, title, artist = parts
        if not title.strip():
            continue
        return Track(status=status.strip(), title=title.strip(), artist=artist.strip())
    return None


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
