import os

import discord

DEFAULT_SOURCE = os.environ.get("AUDIO_SOURCE", "BotSink.monitor")
# Optional opus bitrate cap in kbps. By default each guild gets its boost-tier
# maximum: 96 (no boost) / 128 (T1) / 256 (T2) / 384 (T3 or VIP).
_BITRATE_CAP_KBPS = int(os.environ["OPUS_BITRATE"]) if os.environ.get("OPUS_BITRATE") else None


def bitrate_for(guild: discord.Guild) -> int:
    kbps = int(guild.bitrate_limit) // 1000
    if _BITRATE_CAP_KBPS is not None:
        kbps = min(kbps, _BITRATE_CAP_KBPS)
    # libopus accepts [16, 512].
    return max(16, min(kbps, 512))


def make_source(source: str | None = None) -> discord.FFmpegPCMAudio:
    src = source or DEFAULT_SOURCE
    return discord.FFmpegPCMAudio(
        src,
        before_options="-f pulse",
        options="-vn -loglevel warning",
    )
