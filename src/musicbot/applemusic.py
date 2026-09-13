"""Search the Apple Music catalog and drive the Apple Music app inside WayDroid.

Playback goes over ADB: a tiny dex (contrib/mediactl) runs as the shell uid,
which holds MEDIA_CONTENT_CONTROL, and calls the app's MediaSession transport
controls directly — no UI taps.
"""
import asyncio
import hashlib
import json
import logging
import os
import re
import shlex
import threading
from dataclasses import dataclass
from pathlib import Path

import aiohttp
from adb_shell.adb_device import AdbDeviceTcp
from adb_shell.auth.keygen import keygen
from adb_shell.auth.sign_pythonrsa import PythonRSASigner

log = logging.getLogger(__name__)

# The storefront of the account signed into the app. Songs outside it won't
# play, so search results are always drawn from (or filtered to) this store.
STOREFRONT = os.environ.get("APPLE_MUSIC_STOREFRONT", "cn").strip().lower()
# The iTunes Search API returns nothing for some stores (cn among them). If the
# storefront's web search can't be parsed, search these instead and keep only
# the songs a lookup against STOREFRONT still returns.
FALLBACK_STOREFRONTS = [
    s.strip().lower() for s in os.environ.get("ITUNES_SEARCH_STOREFRONTS", "us,hk,tw,jp").split(",") if s.strip()
]
ADB_ADDRESS = os.environ.get("WAYDROID_ADB", "192.168.240.112:5555")
ADB_KEY = Path(os.environ.get("ADB_KEY", "~/.config/musicbot/adbkey")).expanduser()

_DEX_LOCAL = Path(__file__).with_name("mediactl.dex")
_DEX_REMOTE = "/data/local/tmp/musicbot-mediactl.dex"
_HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"}
_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=15)
_SERVER_DATA = re.compile(r'<script[^>]*id="serialized-server-data"[^>]*>(.*?)</script>', re.S)
# adb-shell devices aren't thread-safe, and concurrent transport commands would
# race each other's before/after checks anyway.
_adb_lock = threading.Lock()


@dataclass(frozen=True)
class Song:
    id: str
    title: str
    artist: str
    album: str = ""


@dataclass(frozen=True)
class NowPlaying:
    state: str
    artist: str
    title: str

    def describe(self) -> str:
        if not self.title:
            return "nothing"
        return f"{self.title} — {self.artist}" if self.artist else self.title


@dataclass(frozen=True)
class ControlResult:
    before: NowPlaying
    after: NowPlaying
    changed: bool


class MediaCtlError(RuntimeError):
    pass


def _parse_web_search(page: str) -> list[Song]:
    m = _SERVER_DATA.search(page)
    if m is None:
        raise ValueError("search page has no serialized-server-data")
    sections = json.loads(m.group(1))["data"][0]["data"]["sections"]
    songs = []
    for section in sections:
        if section.get("itemKind") != "trackLockup":
            continue
        for item in section.get("items") or []:
            descriptor = item.get("contentDescriptor") or {}
            song_id = (descriptor.get("identifiers") or {}).get("storeAdamID")
            if descriptor.get("kind") != "song" or not song_id:
                continue
            songs.append(
                Song(
                    id=str(song_id),
                    title=item.get("title") or "",
                    # tertiaryLinks isn't the album — it's a matched-lyrics snippet with <mark> tags.
                    artist=", ".join(link.get("title", "") for link in item.get("subtitleLinks") or []),
                )
            )
    return songs


async def _get_json(session: aiohttp.ClientSession, url: str, params: dict[str, str]) -> dict:
    async with session.get(url, params=params) as resp:
        resp.raise_for_status()
        # Served as text/javascript, so resp.json() would refuse it.
        return json.loads(await resp.text())


async def _itunes_search(session: aiohttp.ClientSession, query: str, limit: int) -> list[Song]:
    pages = await asyncio.gather(
        *(
            _get_json(
                session,
                "https://itunes.apple.com/search",
                {"term": query, "entity": "song", "limit": str(limit), "country": store},
            )
            for store in FALLBACK_STOREFRONTS
        )
    )
    ids = list(dict.fromkeys(str(r["trackId"]) for page in pages for r in page.get("results", [])))
    if not ids:
        return []
    found = await _get_json(session, "https://itunes.apple.com/lookup", {"id": ",".join(ids), "country": STOREFRONT})
    by_id = {str(r["trackId"]): r for r in found.get("results", []) if r.get("wrapperType") == "track"}
    return [
        Song(id=i, title=by_id[i]["trackName"], artist=by_id[i]["artistName"], album=by_id[i].get("collectionName", ""))
        for i in ids
        if i in by_id
    ]


async def search(query: str, limit: int = 10) -> list[Song]:
    async with aiohttp.ClientSession(headers=_HTTP_HEADERS, timeout=_HTTP_TIMEOUT) as session:
        try:
            async with session.get(f"https://music.apple.com/{STOREFRONT}/search", params={"term": query}) as resp:
                resp.raise_for_status()
                songs = _parse_web_search(await resp.text())
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError, IndexError, TypeError) as exc:
            log.warning("web search failed (%r); falling back to the iTunes Search API", exc)
            songs = await _itunes_search(session, query, limit)
    unique = {song.id: song for song in reversed(songs)}
    return list(reversed(unique.values()))[:limit]


def _signer() -> PythonRSASigner:
    if not ADB_KEY.exists():
        ADB_KEY.parent.mkdir(parents=True, exist_ok=True)
        keygen(str(ADB_KEY))
        log.warning("generated ADB key %s — approve the USB debugging prompt in the WayDroid window", ADB_KEY)
    return PythonRSASigner(Path(f"{ADB_KEY}.pub").read_text(), ADB_KEY.read_text())


def _parse_row(line: str) -> NowPlaying:
    state, artist, title = (line.split("\t") + ["", "", ""])[:3]
    return NowPlaying(state=state, artist=artist, title=title)


def _mediactl(*args: str) -> ControlResult:
    host, _, port = ADB_ADDRESS.rpartition(":")
    local_md5 = hashlib.md5(_DEX_LOCAL.read_bytes()).hexdigest()
    with _adb_lock:
        dev = AdbDeviceTcp(host, int(port), default_transport_timeout_s=10)
        try:
            dev.connect(rsa_keys=[_signer()], auth_timeout_s=30)
            remote_md5 = dev.shell(f"md5sum {_DEX_REMOTE} 2>/dev/null").split(" ")[0].strip()
            if remote_md5 != local_md5:
                log.info("pushing mediactl.dex to WayDroid")
                dev.push(str(_DEX_LOCAL), _DEX_REMOTE)
            # MediaCtl stays silent for up to 10s while it waits for the change,
            # so the per-read transport timeout must outlast that.
            out = dev.shell(
                f"CLASSPATH={_DEX_REMOTE} app_process / MediaCtl {shlex.join(args)} 2>&1",
                transport_timeout_s=30,
                timeout_s=30,
                read_timeout_s=30,
            )
        finally:
            dev.close()

    rows: dict[str, NowPlaying] = {}
    for line in out.splitlines():
        tag, _, rest = line.partition("\t")
        if tag in ("BEFORE", "AFTER", "TIMEOUT"):
            rows[tag] = _parse_row(rest)
    if "BEFORE" not in rows:
        raise MediaCtlError(out.strip()[-500:] or "mediactl produced no output")
    after = rows.get("AFTER") or rows.get("TIMEOUT") or rows["BEFORE"]
    return ControlResult(before=rows["BEFORE"], after=after, changed="AFTER" in rows)


async def _control(*args: str) -> ControlResult:
    return await asyncio.to_thread(_mediactl, *args)


async def play_song(song_id: str) -> ControlResult:
    return await _control("mediaid", song_id)


async def skip() -> ControlResult:
    return await _control("next")


async def toggle_pause() -> ControlResult:
    return await _control("playpause")
