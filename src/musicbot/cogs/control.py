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
# Songs /ecaqueue lists per section before collapsing the rest into "…and N more".
_PLAYING_NEXT_LIMIT = 20
_CONTINUE_PLAYING_LIMIT = 5
_AUTOPLAY_LIMIT = 3


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


def _song(song: applemusic.Song) -> str:
    return escape_markdown(f"{song.title} — {song.artist}" if song.artist else song.title)


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
        assert isinstance(self.view, OwnerView)
        await interaction.response.edit_message(
            content=f"**{_song(song)}**",
            view=SongActions(self.view.owner_id, song),
        )


class OwnerView(discord.ui.View):
    """Only the user who ran the command may use its components."""

    def __init__(self, owner_id: int):
        super().__init__(timeout=180)
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id


class SearchView(OwnerView):
    def __init__(self, owner_id: int, songs: list[applemusic.Song]):
        super().__init__(owner_id)
        self.add_item(SongSelect(songs))


class SongActions(OwnerView):
    def __init__(self, owner_id: int, song: applemusic.Song):
        super().__init__(owner_id)
        self.song = song

    @discord.ui.button(label="Play now", emoji="▶️", style=discord.ButtonStyle.primary)
    async def play_now(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self._run(interaction, None)

    @discord.ui.button(label="Play next", emoji="⏭️", style=discord.ButtonStyle.secondary)
    async def play_next(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self._run(interaction, True)

    @discord.ui.button(label="Add to queue", emoji="➕", style=discord.ButtonStyle.secondary)
    async def add_to_queue(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self._run(interaction, False)

    async def _run(self, interaction: discord.Interaction, next_up: bool | None):
        """next_up: None = play now (keeps Playing Next), True = front of Playing Next, False = its end."""
        song = self.song
        await interaction.response.edit_message(content=f"Sending **{_song(song)}** to Apple Music…", view=None)
        try:
            if next_up is None:
                result = await applemusic.play_song(song.id)
            else:
                result = await applemusic.enqueue(song.id, next_up=next_up)
        except Exception as exc:
            log.warning("apple music action on %s failed: %r", song.id, exc)
            await interaction.edit_original_response(content=f"Couldn't control Apple Music in WayDroid: `{exc}`")
            return

        store = applemusic.STOREFRONT.upper()
        if next_up is None:
            if not result.changed:
                await interaction.edit_original_response(
                    content=(
                        f"Apple Music didn't switch tracks (still on **{_now(result.after)}**). "
                        f"The song may be unavailable in the {store} store, or already playing."
                    )
                )
                return
            await interaction.edit_original_response(content=f"Playing **{_now(result.after)}**.")
            announcement = f"🎵 {interaction.user.mention} put on **{_now(result.after)}**"
        else:
            where = "to play next" if next_up else "to the queue"
            # "Changed" alone isn't proof: a malformed request can insert the wrong
            # song. Play next must land in the visible window; the end of a long
            # Playing Next section may be past it, so accept any change there.
            if not result.changed or (next_up and not result.queued(song.id)):
                await interaction.edit_original_response(
                    content=f"Apple Music didn't add **{_song(song)}** {where}. It may be unavailable in the {store} store."
                )
                return
            await interaction.edit_original_response(content=f"Queued **{_song(song)}** {where}.")
            announcement = f"➕ {interaction.user.mention} queued **{_song(song)}** {where}"
        await interaction.followup.send(announcement, allowed_mentions=discord.AllowedMentions.none())


class RemoveSelect(discord.ui.Select):
    def __init__(self, entries: list[applemusic.QueueEntry]):
        # Keyed by Apple's queue slot id, not song id: the same song can be queued twice.
        self.entries = {str(entry.queue_id): entry for entry in entries}
        options = [
            discord.SelectOption(
                label=_clip(f"{i}. {entry.title or entry.id}", 100),
                description=_clip(entry.artist, 100) or None,
                value=str(entry.queue_id),
            )
            for i, entry in enumerate(entries, 1)
        ]
        super().__init__(placeholder="Pick a song to remove", options=options)

    async def callback(self, interaction: discord.Interaction):
        entry = self.entries[self.values[0]]
        name = escape_markdown(entry.describe())
        await interaction.response.edit_message(content=f"Removing **{name}**…", view=None)
        try:
            result = await applemusic.remove(entry.queue_id)
        except Exception as exc:
            log.warning("remove(%s) failed: %r", entry.queue_id, exc)
            await interaction.edit_original_response(content=f"Couldn't control Apple Music in WayDroid: `{exc}`")
            return
        if not result.changed or any(e.queue_id == entry.queue_id for e in result.queue):
            await interaction.edit_original_response(
                content=f"Apple Music didn't remove **{name}**. It may have already played or been removed."
            )
            return
        await interaction.edit_original_response(content=f"Removed **{name}**.")
        await interaction.followup.send(
            f"➖ {interaction.user.mention} removed **{name}** from the queue",
            allowed_mentions=discord.AllowedMentions.none(),
        )


class RemoveView(OwnerView):
    def __init__(self, owner_id: int, entries: list[applemusic.QueueEntry]):
        super().__init__(owner_id)
        self.add_item(RemoveSelect(entries))


async def _search(interaction: discord.Interaction, query: str) -> None:
    """Reply with a song picker. The interaction must already be deferred ephemerally."""
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


async def _remove_prompt(interaction: discord.Interaction) -> None:
    """Reply with a Playing Next picker. The interaction must already be deferred ephemerally."""
    try:
        result = await applemusic.upcoming()
    except Exception as exc:
        log.warning("upcoming failed: %r", exc)
        await interaction.followup.send(f"Couldn't read Apple Music's queue: `{exc}`", ephemeral=True)
        return
    # Only songs people added; the album/playlist and Autoplay stay untouched.
    entries = [e for e in result.upcoming() if e.in_queue_section and e.queue_id != -1][:25]
    if not entries:
        await interaction.followup.send("Nothing in **Playing Next** to remove.", ephemeral=True)
        return
    await interaction.followup.send(
        "Which song should come out of **Playing Next**?",
        view=RemoveView(interaction.user.id, entries),
        ephemeral=True,
    )


async def _skip(interaction: discord.Interaction) -> None:
    """Skip and announce it publicly. The interaction must already be deferred."""
    try:
        result = await applemusic.skip()
    except Exception as exc:
        log.warning("skip failed: %r", exc)
        await interaction.followup.send(f"Couldn't control Apple Music in WayDroid: `{exc}`")
        return
    if result.changed:
        await interaction.followup.send(
            f"⏭️ {interaction.user.mention} skipped to **{_now(result.after)}**",
            allowed_mentions=discord.AllowedMentions.none(),
        )
    else:
        # Songs started via /ecasearch replace the queue with just that song, and
        # repeat-one makes "next" replay the current track. The framework
        # MediaController doesn't expose repeat mode, so we can't tell which.
        await interaction.followup.send(
            f"Skip didn't change the song — still on **{_now(result.after)}**. "
            "Repeat-one may be on in Apple Music, or there's nothing queued after this song."
        )


class SearchModal(discord.ui.Modal, title="Search Apple Music"):
    query = discord.ui.TextInput(label="Song title, artist, or both", max_length=100)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await _search(interaction, self.query.value)


class QueueActions(discord.ui.View):
    """Buttons under /ecaqueue. Anyone allowed to control playback may use them."""

    def __init__(self):
        super().__init__(timeout=900)
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if _may_control(interaction):
            return True
        await interaction.response.send_message("You're not allowed to control playback.", ephemeral=True)
        return False

    async def on_timeout(self):
        # Discord keeps showing the buttons after the view stops listening; clicks would just fail.
        if self.message is not None:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item):
        log.exception("queue button failed", exc_info=error)
        msg = f"Something went wrong: `{error}`"
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)

    @discord.ui.button(label="Queue", emoji="🎵", style=discord.ButtonStyle.primary)
    async def search(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await interaction.response.send_modal(SearchModal())

    @discord.ui.button(label="Remove", emoji="🗑️", style=discord.ButtonStyle.secondary)
    async def remove(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await _remove_prompt(interaction)

    @discord.ui.button(label="Skip", emoji="⏭️", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await interaction.response.defer(thinking=True)
        await _skip(interaction)


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
        await _search(interaction, query)

    @app_commands.command(name="ecaqueue", description="接下来放什么📜")
    async def ecaqueue(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        try:
            result = await applemusic.upcoming()
        except Exception as exc:
            log.warning("upcoming failed: %r", exc)
            await interaction.followup.send(f"Couldn't read Apple Music's queue: `{exc}`")
            return
        lines = [f"**Now:** {_now(result.before)}"]
        upcoming = result.upcoming()
        # Same grouping and names as Apple Music's own queue screen, in play order.
        # Each with how many songs to list: people's own picks matter most.
        groups = [
            ("Playing Next", _PLAYING_NEXT_LIMIT, [e for e in upcoming if e.in_queue_section]),
            (
                "Continue Playing",
                _CONTINUE_PLAYING_LIMIT,
                [e for e in upcoming if not e.in_queue_section and not e.from_autoplay],
            ),
            ("Autoplay", _AUTOPLAY_LIMIT, [e for e in upcoming if e.from_autoplay and not e.in_queue_section]),
        ]
        footer = "-# Apple Music only exposes the next few songs for autoplay."
        # A long playlist's Continue Playing section alone can blow past Discord's
        # 2000-character message limit, so cap each section and the message as a whole.
        budget = 2000 - len(footer) - 1
        n = 0
        for i, (title, limit, entries) in enumerate(groups):
            if not entries:
                continue
            lines.append(f"**{title}:**")
            # Room for this section's "…and N more" line plus a header and one for each later section.
            reserve = 30 + 60 * sum(1 for _, _, later in groups[i + 1 :] if later)
            shown = 0
            for entry in entries[:limit]:
                line = f"`{n + shown + 1:>2}.` {escape_markdown(entry.describe())}"
                if len("\n".join(lines)) + len(line) + 1 + reserve > budget:
                    break
                lines.append(line)
                shown += 1
            if shown < len(entries):
                lines.append(f"-# …and {len(entries) - shown} more")
            n += len(entries)
        if n:
            lines.append(footer)
        else:
            lines.append("Nothing queued after this song.")
        view = QueueActions()
        view.message = await interaction.followup.send("\n".join(lines)[:2000], view=view, wait=True)

    @app_commands.command(name="ecaremove", description="不想听这首🗑️")
    @controllers_only
    async def ecaremove(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await _remove_prompt(interaction)

    @app_commands.command(name="ecaskip", description="切歌⏭️")
    @controllers_only
    async def ecaskip(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        await _skip(interaction)

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
