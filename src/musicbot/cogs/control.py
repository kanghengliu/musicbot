import logging
import os

import discord
from discord import app_commands
from discord.ext import commands
from discord.utils import escape_markdown

from musicbot import applemusic

log = logging.getLogger(__name__)


def _id_set(name: str) -> set[int]:
    return {int(x) for x in os.environ.get(name, "").split(",") if x.strip()}


# Both empty = anyone who can run slash commands may control playback.
CONTROL_ROLE_IDS = _id_set("CONTROL_ROLE_IDS")
CONTROL_USER_IDS = _id_set("CONTROL_USER_IDS")


def _may_control(interaction: discord.Interaction) -> bool:
    if not CONTROL_ROLE_IDS and not CONTROL_USER_IDS:
        return True
    if interaction.user.id in CONTROL_USER_IDS:
        return True
    roles = getattr(interaction.user, "roles", [])
    return any(role.id in CONTROL_ROLE_IDS for role in roles)


controllers_only = app_commands.check(_may_control)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _now(np: applemusic.NowPlaying) -> str:
    return escape_markdown(np.describe())


class SongSelect(discord.ui.Select):
    def __init__(self, songs: list[applemusic.Song]):
        self.songs = {song.id: song for song in songs}
        options = [
            discord.SelectOption(
                label=_clip(song.title or song.id, 100),
                description=_clip(" · ".join(p for p in (song.artist, song.album) if p), 100) or None,
                value=song.id,
            )
            for song in songs
        ]
        super().__init__(placeholder="Pick a song to play", options=options)

    async def callback(self, interaction: discord.Interaction):
        song = self.songs[self.values[0]]
        await interaction.response.edit_message(content=f"Starting **{escape_markdown(song.title)}**…", view=None)
        try:
            result = await applemusic.play_song(song.id)
        except Exception as exc:
            log.warning("play_song(%s) failed: %r", song.id, exc)
            await interaction.edit_original_response(content=f"Couldn't control Apple Music in WayDroid: `{exc}`")
            return
        if not result.changed:
            await interaction.edit_original_response(
                content=(
                    f"Apple Music didn't switch tracks (still on **{_now(result.after)}**). "
                    f"The song may be unavailable in the {applemusic.STOREFRONT.upper()} store, or already playing."
                )
            )
            return
        await interaction.edit_original_response(content=f"Playing **{_now(result.after)}**.")
        await interaction.followup.send(
            f"🎵 {interaction.user.mention} put on **{_now(result.after)}**",
            allowed_mentions=discord.AllowedMentions.none(),
        )


class SearchView(discord.ui.View):
    def __init__(self, owner_id: int, songs: list[applemusic.Song]):
        super().__init__(timeout=180)
        self.owner_id = owner_id
        self.add_item(SongSelect(songs))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id


class Control(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CheckFailure):
            msg = "You're not allowed to control playback."
        else:
            log.exception("control command failed", exc_info=error)
            msg = f"Something went wrong: `{error}`"
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(name="ecasearch", description="帮eca点歌🎵")
    @app_commands.describe(query="Song title, artist, or both.")
    @controllers_only
    async def ecasearch(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            songs = await applemusic.search(query)
        except Exception as exc:
            log.warning("search(%r) failed: %r", query, exc)
            await interaction.followup.send(f"Search failed: `{exc}`", ephemeral=True)
            return
        if not songs:
            await interaction.followup.send(
                f"No songs found in the {applemusic.STOREFRONT.upper()} store for **{escape_markdown(query)}**.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            f"Results for **{escape_markdown(query)}**:",
            view=SearchView(interaction.user.id, songs),
            ephemeral=True,
        )

    @app_commands.command(name="ecaskip", description="切歌⏭️")
    @controllers_only
    async def ecaskip(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        try:
            result = await applemusic.skip()
        except Exception as exc:
            log.warning("skip failed: %r", exc)
            await interaction.followup.send(f"Couldn't control Apple Music in WayDroid: `{exc}`")
            return
        if result.changed:
            await interaction.followup.send(f"⏭️ Now playing **{_now(result.after)}**")
        else:
            # Songs started via /ecasearch replace the queue with just that song, and
            # repeat-one makes "next" replay the current track. The framework
            # MediaController doesn't expose repeat mode, so we can't tell which.
            await interaction.followup.send(
                f"Skip didn't change the song — still on **{_now(result.after)}**. "
                "Repeat-one may be on in Apple Music, or there's nothing queued after this song."
            )

    @app_commands.command(name="ecapause", description="暂停/继续⏯️")
    @controllers_only
    async def ecapause(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        try:
            result = await applemusic.toggle_pause()
        except Exception as exc:
            log.warning("toggle_pause failed: %r", exc)
            await interaction.followup.send(f"Couldn't control Apple Music in WayDroid: `{exc}`")
            return
        if not result.changed:
            await interaction.followup.send(f"Apple Music didn't respond (state: {result.after.state}).")
        elif result.after.state == "playing":
            await interaction.followup.send(f"▶️ Resumed **{_now(result.after)}**")
        else:
            await interaction.followup.send(f"⏸️ Paused **{_now(result.after)}**")


async def setup(bot: commands.Bot):
    await bot.add_cog(Control(bot))
