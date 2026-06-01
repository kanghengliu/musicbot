import logging

import discord
from discord import app_commands
from discord.ext import commands

from musicbot.audio import DEFAULT_BITRATE_KBPS, DEFAULT_SOURCE, make_source

log = logging.getLogger(__name__)


class Voice(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

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

    @app_commands.command(name="ecaplay", description="Stream the audio bridge into a voice channel.")
    @app_commands.describe(
        channel="Voice channel to join (defaults to yours).",
        source="PipeWire/Pulse source name (defaults to AUDIO_SOURCE env).",
    )
    async def ecaplay(
        self,
        interaction: discord.Interaction,
        channel: discord.VoiceChannel | None = None,
        source: str | None = None,
    ):
        if interaction.guild is None:
            await interaction.response.send_message("Guild-only command.", ephemeral=True)
            return

        target = await self._resolve_channel(interaction, channel)
        if target is None:
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        vc: discord.VoiceClient | None = interaction.guild.voice_client  # type: ignore[assignment]
        if vc is None:
            vc = await target.connect()
        elif vc.channel != target:
            await vc.move_to(target)
        if vc.is_playing():
            vc.stop()

        audio = make_source(source)

        def _after(err: Exception | None):
            if err:
                log.warning("playback ended with error: %r", err)

        vc.play(audio, after=_after)
        try:
            vc.encoder.set_bitrate(DEFAULT_BITRATE_KBPS)
            vc.encoder.set_fec(False)
        except Exception as exc:
            log.warning("failed to configure opus encoder: %r", exc)

        await interaction.followup.send(
            f"Streaming `{source or DEFAULT_SOURCE}` → {target.mention} @ {DEFAULT_BITRATE_KBPS} kbps.",
            ephemeral=True,
        )

    @app_commands.command(name="ecaleave", description="Stop streaming and disconnect.")
    async def ecaleave(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("Guild-only command.", ephemeral=True)
            return
        vc: discord.VoiceClient | None = interaction.guild.voice_client  # type: ignore[assignment]
        if vc is None:
            await interaction.response.send_message("Not connected.", ephemeral=True)
            return
        await vc.disconnect(force=False)
        await interaction.response.send_message("Disconnected.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Voice(bot))
