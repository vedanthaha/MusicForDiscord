"""
utils/embeds.py – Reusable Discord embed builders for the music bot.

All embeds share a consistent colour palette and footer so the bot
feels cohesive across all commands.
"""

from __future__ import annotations

import discord
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cogs.music import Track, GuildPlayer

# ── Palette ───────────────────────────────────────────────────────────────────
COLOUR_OK = discord.Colour.from_str("#5865F2")      # Discord Blurple
COLOUR_ERR = discord.Colour.from_str("#ED4245")     # Discord Red
COLOUR_WARN = discord.Colour.from_str("#FEE75C")    # Discord Yellow
COLOUR_INFO = discord.Colour.from_str("#57F287")    # Discord Green

FOOTER_TEXT = "Music Bot • Powered by Arc Music API"


def _base(title: str, colour: discord.Colour, description: str = "") -> discord.Embed:
    embed = discord.Embed(title=title, description=description, colour=colour)
    embed.set_footer(text=FOOTER_TEXT)
    return embed


def ms_to_str(milliseconds: int) -> str:
    """Convert milliseconds into a human-readable duration string."""
    if milliseconds <= 0:
        return "0:00"
    total_seconds = milliseconds // 1000
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def progress_bar(position_ms: int, length_ms: int, width: int = 20) -> str:
    """Return a Unicode block progress bar."""
    if length_ms <= 0:
        ratio = 0.0
    else:
        ratio = max(0.0, min(1.0, position_ms / length_ms))
    filled = round(ratio * width)
    bar = "█" * filled + "░" * (width - filled)
    return f"[{bar}]"


# ── Native Track / Playback embeds (No Wavelink dependency) ───────────────────

def now_playing_track(
    track: Track,
    position: float = 0.0,
    is_paused: bool = False,
    volume: int = 80,
    queue_len: int = 0,
) -> discord.Embed:
    """Rich 'Now Playing' embed for native Track object with progress bar."""
    embed = discord.Embed(colour=COLOUR_OK)
    embed.set_author(name="▶  Now Playing")
    embed.title = track.title
    if track.youtube_url:
        embed.url = track.youtube_url

    if track.thumbnail:
        embed.set_thumbnail(url=track.thumbnail)

    embed.add_field(name="Requested By", value=track.requester.mention if track.requester else "Unknown", inline=True)
    vol_icon = "🔇" if volume == 0 else ("🔉" if volume < 60 else "🔊")
    status = "⏸ Paused" if is_paused else "▶ Playing"
    embed.add_field(name="Volume", value=f"{vol_icon} {volume}%", inline=True)
    embed.add_field(name="Status", value=status, inline=True)

    pos_ms = int(position * 1000)
    dur_ms = int((track.duration or 0) * 1000)
    bar = progress_bar(pos_ms, dur_ms, width=22)
    time_str = f"`{ms_to_str(pos_ms)} {bar} {ms_to_str(dur_ms)}`"
    embed.add_field(name="Progress", value=time_str, inline=False)
    
    if queue_len > 0:
        embed.add_field(name="Queue", value=f"{queue_len} track(s) next", inline=True)

    embed.set_footer(text=FOOTER_TEXT)
    return embed


def track_queued(track: Track, position: int) -> discord.Embed:
    embed = _base("Added to Queue", COLOUR_OK)
    description = f"**{track.title}**"
    if track.youtube_url:
        description = f"**[{track.title}]({track.youtube_url})**"
    embed.description = description
    embed.add_field(name="Duration", value=track.duration_str, inline=True)
    embed.add_field(name="Position", value=f"#{position}", inline=True)
    if track.thumbnail:
        embed.set_thumbnail(url=track.thumbnail)
    return embed


def playlist_queued(title: str, track_count: int) -> discord.Embed:
    embed = _base("Playlist Added to Queue", COLOUR_OK)
    embed.description = f"Queued **{track_count}** tracks from **{title}**"
    return embed



def queue_embed_native(player: GuildPlayer, page: int = 1, page_size: int = 10) -> discord.Embed:
    """Paginated queue embed for native GuildPlayer."""
    q = list(player.queue)
    total = len(q)

    if total == 0:
        embed = _base("Queue", COLOUR_INFO, "The queue is empty.")
        if player.current:
            embed.set_footer(text=f"{FOOTER_TEXT} | Currently playing: {player.current.title}")
        return embed

    total_pages = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))
    start = (page - 1) * page_size
    chunk = q[start : start + page_size]

    lines: list[str] = []
    for i, track in enumerate(chunk, start=start + 1):
        lines.append(f"`{i:>2}.` **{track.title}** `[{track.duration_str}]`")

    # Sum duration of queue
    total_sec = sum(t.duration for t in q if t.duration is not None)
    m, s = divmod(total_sec, 60)
    h, m = divmod(m, 60)
    total_dur = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    embed = _base(f"Queue — Page {page}/{total_pages}", COLOUR_INFO, "\n".join(lines))
    embed.set_footer(text=f"{FOOTER_TEXT} | {total} tracks · {total_dur} total")
    return embed


# ── Status / control embeds ───────────────────────────────────────────────────

def success(message: str) -> discord.Embed:
    return _base("✅  Success", COLOUR_OK, message)


def error(message: str) -> discord.Embed:
    return _base("❌  Error", COLOUR_ERR, message)


def warning(message: str) -> discord.Embed:
    return _base("⚠️  Warning", COLOUR_WARN, message)


def info(message: str) -> discord.Embed:
    return _base("ℹ️  Info", COLOUR_INFO, message)


def lyrics_embed(title: str, artist: str, lyrics_content: str, is_synced: bool = True) -> discord.Embed:
    tag = "⚡ Live Auto-Synced Lyrics" if is_synced else "📜 Lyrics"
    embed = _base(f"🎤 {title}", COLOUR_OK)
    if artist:
        embed.set_author(name=f"{artist} • {tag}")
    else:
        embed.set_author(name=tag)
    embed.description = lyrics_content
    return embed

