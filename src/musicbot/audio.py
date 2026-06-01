import os

import discord

DEFAULT_SOURCE = os.environ.get("AUDIO_SOURCE", "BotSink.monitor")
# Opus bitrate in kbps. Bounded [16, 512] by libopus; Discord caps per-channel
# at 96 (no boost) / 128 (T1) / 256 (T2) / 384 (T3) — set the channel bitrate
# to match in the channel's Edit Channel → Audio Bitrate menu.
DEFAULT_BITRATE_KBPS = int(os.environ.get("OPUS_BITRATE", "256"))


def make_source(source: str | None = None) -> discord.FFmpegPCMAudio:
    src = source or DEFAULT_SOURCE
    return discord.FFmpegPCMAudio(
        src,
        before_options="-f pulse",
        options="-vn -loglevel warning",
    )
