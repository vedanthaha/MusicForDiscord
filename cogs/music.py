"""
cogs/music.py – Music commands using discord.py native voice + Arc API + FFmpeg.

No Lavalink or Java required.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass
from typing import cast, Any

import discord
from discord import app_commands
from discord.ext import commands

import utils.embeds as em
from config import cfg
from services.arc import ArcClient, ArcError

logger = logging.getLogger(__name__)

# ── Track ─────────────────────────────────────────────────────────────────────

@dataclass
class Track:
    title: str
    stream_url: str                   # Arc CDN MP3 URL
    thumbnail: str | None = None
    duration: int | None = None       # seconds
    requester: discord.Member | None = None
    video_id: str | None = None

    @property
    def duration_str(self) -> str:
        if self.duration is None:
            return "?:??"
        m, s = divmod(self.duration, 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    @property
    def youtube_url(self) -> str | None:
        if self.video_id:
            return f"https://www.youtube.com/watch?v={self.video_id}"
        return None

# ── Position Tracking Source ──────────────────────────────────────────────────

class PositionAudioSource(discord.AudioSource):
    """Wraps an AudioSource to track playback position in seconds."""

    def __init__(self, source: discord.AudioSource) -> None:
        self.source = source
        self.read_count = 0

    def read(self) -> bytes:
        data = self.source.read()
        if data:
            self.read_count += 1
        return data

    @property
    def position(self) -> float:
        # Each read returns 20ms of audio frame (0.02s)
        return self.read_count * 0.02

    def is_opus(self) -> bool:
        return self.source.is_opus()

    def cleanup(self) -> None:
        self.source.cleanup()

# ── UI View Controls ──────────────────────────────────────────────────────────

class MusicControlsView(discord.ui.View):
    """Interactive control buttons under the Now Playing embed."""

    def __init__(self, player: GuildPlayer, cog: Music, timeout: float = 600) -> None:
        super().__init__(timeout=timeout)
        self.player = player
        self.cog = cog
        self.message: discord.Message | None = None
        self._update_buttons()

    def _update_buttons(self) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.Button) and child.custom_id == "play_pause":
                if self.player.is_paused:
                    child.label = "Resume"
                    child.style = discord.ButtonStyle.green
                    child.emoji = "▶"
                else:
                    child.label = "Pause"
                    child.style = discord.ButtonStyle.grey
                    child.emoji = "⏸"

    async def update_message(self) -> None:
        if not self.message or not self.player.current:
            return
        self._update_buttons()
        embed = em.now_playing_track(
            self.player.current,
            position=self.player.position,
            is_paused=self.player.is_paused,
            volume=self.player.volume,
            queue_len=len(self.player.queue),
        )
        try:
            await self.message.edit(embed=embed, view=self)
        except Exception:
            pass

    async def on_timeout(self) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        try:
            if self.message:
                await self.message.edit(view=self)
        except Exception:
            pass

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        member = cast(discord.Member, interaction.user)
        if not member.voice or not member.voice.channel or member.voice.channel != self.player.vc.channel:
            await interaction.response.send_message(
                embed=em.error("You must be in the same voice channel to use control buttons."),
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Pause", style=discord.ButtonStyle.grey, emoji="⏸", custom_id="play_pause")
    async def play_pause_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.player.is_paused:
            self.player.resume()
        else:
            self.player.pause()
        await interaction.response.defer()
        await self.update_message()

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.grey, emoji="⏭", custom_id="skip")
    async def skip_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.player.skip()
        await interaction.response.defer()

    @discord.ui.button(label="Vol -", style=discord.ButtonStyle.grey, emoji="🔉", custom_id="vol_down")
    async def vol_down_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        new_vol = max(10, self.player.volume - 10)
        self.player.set_volume(new_vol)
        await interaction.response.defer()
        await self.update_message()

    @discord.ui.button(label="Vol +", style=discord.ButtonStyle.grey, emoji="🔊", custom_id="vol_up")
    async def vol_up_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        new_vol = min(250, self.player.volume + 10)
        self.player.set_volume(new_vol)
        await interaction.response.defer()
        await self.update_message()

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.red, emoji="⏹", custom_id="stop")
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.cog._players.pop(self.player.guild.id, None)
        await self.player.disconnect()
        await interaction.response.send_message(
            embed=em.success("Stopped and disconnected."), ephemeral=True
        )
        self.stop()

# ── GuildPlayer ───────────────────────────────────────────────────────────────

_FFMPEG_BEFORE_OPTS = (
    "-reconnect 1 "
    "-reconnect_streamed 1 "
    "-reconnect_delay_max 5 "
    "-probesize 32k "
    "-analyzeduration 0 "
    "-loglevel warning"
)
_FFMPEG_OPTS = '-vn -ar 48000 -ac 2 -filter:a "volume=1.3"'
_BITRATE_BY_TIER: dict[int, int] = {0: 96_000, 1: 128_000, 2: 256_000, 3: 384_000}

class GuildPlayer:
    """Manages voice playback for a single guild."""

    def __init__(
        self,
        vc: discord.VoiceClient,
        loop: asyncio.AbstractEventLoop,
        cog: Music,
        text_channel: discord.TextChannel | discord.Thread,
        volume: float = 0.8,
    ) -> None:
        self.vc = vc
        self._loop = loop
        self.cog = cog
        self.text_channel = text_channel
        self._volume = max(0.0, min(2.5, volume))
        self.queue: deque[Track] = deque()
        self.current: Track | None = None
        self._idle_task: asyncio.Task | None = None
        self._updater_task: asyncio.Task | None = None
        self.current_view: MusicControlsView | None = None
        self._is_switching: bool = False

    @property
    def is_playing(self) -> bool:
        return self.vc.is_playing()

    @property
    def is_paused(self) -> bool:
        return self.vc.is_paused()

    @property
    def volume(self) -> int:
        return round(self._volume * 100)

    @property
    def guild(self) -> discord.Guild:
        return self.vc.guild

    @property
    def position(self) -> float:
        if self.vc.source and isinstance(self.vc.source, PositionAudioSource):
            return self.vc.source.position
        return 0.0

    def _make_source(self, url: str) -> discord.AudioSource:
        raw = discord.FFmpegPCMAudio(url, before_options=_FFMPEG_BEFORE_OPTS, options=_FFMPEG_OPTS)
        vol_wrapped = discord.PCMVolumeTransformer(raw, volume=self._volume)
        return PositionAudioSource(vol_wrapped)

    async def start(self, track: Track) -> None:
        self._cancel_idle()
        self._cancel_updater()
        if self.vc.is_playing() or self.vc.is_paused():
            self._is_switching = True
            self.vc.stop()
            await asyncio.sleep(0.05)
            self._is_switching = False

        self.current = track
        source = self._make_source(track.stream_url)
        self.vc.play(source, after=self._after_play)
        logger.info("Playing in guild %s: %r", self.guild.id, track.title)
        self._updater_task = self._loop.create_task(self._update_loop())

    def _after_play(self, error: Exception | None) -> None:
        if error:
            logger.error("FFmpeg playback error in guild %s: %s", self.guild.id, error)
        if getattr(self, "_is_switching", False):
            return
        asyncio.run_coroutine_threadsafe(self._advance(), self._loop)

    async def _advance(self) -> None:
        self._cancel_updater()
        if self.current_view:
            self.current_view.stop()
            self.current_view = None

        self.current = None
        if self.queue:
            next_track = self.queue.popleft()
            await self.start(next_track)
            try:
                view = MusicControlsView(self, self.cog)
                embed = em.now_playing_track(
                    next_track,
                    position=0.0,
                    is_paused=False,
                    volume=self.volume,
                    queue_len=len(self.queue),
                )
                msg = await self.text_channel.send(embed=embed, view=view)
                view.message = msg
                self.current_view = view
            except Exception as exc:
                logger.warning("Could not send advance track embed: %s", exc)
        else:
            self._start_idle_timer()

    def pause(self) -> bool:
        if self.vc.is_playing():
            self.vc.pause()
            return True
        return False

    def resume(self) -> bool:
        if self.vc.is_paused():
            self.vc.resume()
            return True
        return False

    def skip(self) -> bool:
        if self.vc.is_playing() or self.vc.is_paused():
            self.vc.stop()
            return True
        return False

    def set_volume(self, percent: int) -> None:
        self._volume = percent / 100.0
        if self.vc.source and isinstance(self.vc.source, PositionAudioSource):
            volume_transformer = self.vc.source.source
            if isinstance(volume_transformer, discord.PCMVolumeTransformer):
                volume_transformer.volume = self._volume

    async def disconnect(self) -> None:
        self._is_switching = True
        self._cancel_idle()
        self._cancel_updater()
        if self.current_view:
            self.current_view.stop()
            self.current_view = None
        self.queue.clear()
        self.current = None
        if self.vc.is_playing() or self.vc.is_paused():
            self.vc.stop()
        if self.vc.is_connected():
            await self.vc.disconnect(force=False)
        logger.info("Disconnected from guild %s", self.guild.id)

    def _start_idle_timer(self) -> None:
        self._cancel_idle()
        self._idle_task = self._loop.create_task(self._idle_disconnect())

    def _cancel_idle(self) -> None:
        if self._idle_task and not self._idle_task.done():
            self._idle_task.cancel()
            self._idle_task = None

    async def _idle_disconnect(self) -> None:
        await asyncio.sleep(cfg.alone_timeout)
        if not self.is_playing and not self.is_paused:
            logger.info("Auto-disconnecting idle player in guild %s", self.guild.id)
            self.cog._players.pop(self.guild.id, None)
            await self.disconnect()

    async def _update_loop(self) -> None:
        while self.is_playing or self.is_paused:
            await asyncio.sleep(5)
            if self.current_view and not self.is_paused:
                await self.current_view.update_message()

    def _cancel_updater(self) -> None:
        if self._updater_task and not self._updater_task.done():
            self._updater_task.cancel()
            self._updater_task = None

# ── Music Cog ─────────────────────────────────────────────────────────────────

class Music(commands.Cog):
    """Music playback powered by Arc API + FFmpeg (no Lavalink required)."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.arc = ArcClient()
        self._players: dict[int, GuildPlayer] = {}

    async def cog_load(self) -> None:
        await self.arc.start()
        logger.info("Music cog loaded; Arc client started.")

    async def cog_unload(self) -> None:
        await self.arc.close()
        for player in list(self._players.values()):
            try:
                await player.disconnect()
            except Exception:
                pass
        self._players.clear()
        logger.info("Music cog unloaded; all players disconnected.")

    def _get_player(self, guild: discord.Guild) -> GuildPlayer | None:
        player = self._players.get(guild.id)
        if player and not player.vc.is_connected():
            del self._players[guild.id]
            return None
        return player

    async def _ensure_player(self, interaction: discord.Interaction) -> GuildPlayer | None:
        guild = cast(discord.Guild, interaction.guild)
        member = cast(discord.Member, interaction.user)

        if not member.voice or not member.voice.channel:
            await interaction.followup.send(
                embed=em.error("You must be in a voice channel first."), ephemeral=True
            )
            return None

        target = member.voice.channel
        existing = self._get_player(guild)

        if existing:
            if existing.vc.channel != target:
                await interaction.followup.send(
                    embed=em.error(f"I'm already in **{existing.vc.channel.mention}**."),
                    ephemeral=True,
                )
                return None
            existing.text_channel = interaction.channel
            return existing

        try:
            if guild.voice_client and guild.voice_client.is_connected():
                vc = cast(discord.VoiceClient, guild.voice_client)
                if vc.channel != target:
                    await vc.move_to(target)
            else:
                try:
                    vc = await target.connect(self_deaf=True, timeout=15.0, reconnect=True)
                except Exception as first_err:
                    logger.warning("Initial voice connect failed (%s). Forcing voice state reset and retrying...", first_err)
                    if guild.voice_client:
                        try:
                            await guild.voice_client.disconnect(force=True)
                        except Exception:
                            pass
                        await asyncio.sleep(1.0)
                    vc = await target.connect(self_deaf=True, timeout=20.0, reconnect=True)
        except Exception as exc:
            logger.warning("Voice connect failed: %s", exc)
            await interaction.followup.send(
                embed=em.error("Couldn't join your channel — check my permissions or try again."),
                ephemeral=True,
            )
            return None

        loop = asyncio.get_event_loop()
        player = GuildPlayer(vc, loop, self, interaction.channel, volume=cfg.default_volume / 100)
        self._players[guild.id] = player

        if isinstance(target, discord.VoiceChannel):
            await self._try_boost_bitrate(target, guild)

        logger.info("Player created for guild %s in %s", guild.id, target.name)
        return player

    @staticmethod
    async def _try_boost_bitrate(channel: discord.VoiceChannel, guild: discord.Guild) -> None:
        if not channel.permissions_for(guild.me).manage_channels:
            return
        max_br = _BITRATE_BY_TIER.get(guild.premium_tier, 96_000)
        if channel.bitrate >= max_br:
            return
        try:
            await channel.edit(bitrate=max_br, reason="Music bot quality boost")
        except discord.HTTPException as exc:
            logger.warning("Bitrate boost failed: %s", exc)

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        original = getattr(error, "original", error)
        logger.error("Command error: %s", original, exc_info=original)
        msg = f"An error occurred: `{original}`"
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=em.error(msg), ephemeral=True)
            else:
                await interaction.response.send_message(embed=em.error(msg), ephemeral=True)
        except Exception:
            pass

    # ── Slash commands ────────────────────────────────────────────────────────

    @app_commands.command(name="play", description="Play a song or add it to the queue.")
    @app_commands.describe(query="Song name, YouTube URL, or Spotify URL")
    @app_commands.guild_only()
    async def play(self, interaction: discord.Interaction, query: str) -> None:
        await interaction.response.defer(ephemeral=False)
        player = await self._ensure_player(interaction)
        if player is None:
            return

        resolved = await self.arc.resolve(query)
        if not resolved.via_arc:
            await interaction.followup.send(
                embed=em.error("Only YouTube and Spotify links / song names are supported."),
                ephemeral=True,
            )
            return

        track = Track(
            title=resolved.title or query,
            stream_url=resolved.lavalink_query,
            thumbnail=resolved.thumbnail,
            duration=resolved.duration,
            requester=cast(discord.Member, interaction.user),
            video_id=resolved.video_id,
        )

        if player.is_playing or player.is_paused:
            player.queue.append(track)
            pos = len(player.queue)
            await interaction.followup.send(embed=em.track_queued(track, pos))
        else:
            player.text_channel = interaction.channel
            await player.start(track)
            
            view = MusicControlsView(player, self)
            msg = await interaction.followup.send(
                embed=em.now_playing_track(
                    track, position=0.0, is_paused=False, volume=player.volume, queue_len=0
                ),
                view=view
            )
            view.message = msg
            player.current_view = view

    @app_commands.command(name="skip", description="Skip the current track.")
    @app_commands.guild_only()
    async def skip(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        guild = cast(discord.Guild, interaction.guild)
        player = self._get_player(guild)

        if not player or not (player.is_playing or player.is_paused):
            await interaction.followup.send(embed=em.error("Nothing is playing."), ephemeral=True)
            return

        title = player.current.title if player.current else "the current track"
        player.skip()
        await interaction.followup.send(embed=em.success(f"Skipped **{title}**."), ephemeral=True)

    @app_commands.command(name="pause", description="Pause playback.")
    @app_commands.guild_only()
    async def pause(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        guild = cast(discord.Guild, interaction.guild)
        player = self._get_player(guild)

        if not player or not player.is_playing:
            await interaction.followup.send(embed=em.error("Nothing is playing."), ephemeral=True)
            return

        if player.pause():
            if player.current_view:
                await player.current_view.update_message()
            await interaction.followup.send(embed=em.success("Paused ⏸"), ephemeral=True)
        else:
            await interaction.followup.send(embed=em.warning("Already paused."), ephemeral=True)

    @app_commands.command(name="resume", description="Resume paused playback.")
    @app_commands.guild_only()
    async def resume(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        guild = cast(discord.Guild, interaction.guild)
        player = self._get_player(guild)

        if not player or not player.is_paused:
            await interaction.followup.send(embed=em.error("Not paused."), ephemeral=True)
            return

        if player.resume():
            if player.current_view:
                await player.current_view.update_message()
            await interaction.followup.send(embed=em.success("Resumed ▶"), ephemeral=True)

    @app_commands.command(name="stop", description="Stop playback and disconnect.")
    @app_commands.guild_only()
    async def stop(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        guild = cast(discord.Guild, interaction.guild)
        player = self._players.pop(guild.id, None)

        if not player:
            await interaction.followup.send(embed=em.error("I'm not connected."), ephemeral=True)
            return

        await player.disconnect()
        await interaction.followup.send(embed=em.success("Stopped and disconnected."), ephemeral=True)

    @app_commands.command(name="queue", description="Show the current queue.")
    @app_commands.describe(page="Page number (default 1)")
    @app_commands.guild_only()
    async def queue(self, interaction: discord.Interaction, page: int = 1) -> None:
        await interaction.response.defer(ephemeral=False)
        guild = cast(discord.Guild, interaction.guild)
        player = self._get_player(guild)

        if not player:
            await interaction.followup.send(embed=em.info("No player active."), ephemeral=True)
            return

        await interaction.followup.send(embed=em.queue_embed_native(player, page))

    @app_commands.command(name="nowplaying", description="Show what's playing.")
    @app_commands.guild_only()
    async def nowplaying(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=False)
        guild = cast(discord.Guild, interaction.guild)
        player = self._get_player(guild)

        if not player or not player.current:
            await interaction.followup.send(embed=em.info("Nothing is playing."), ephemeral=True)
            return

        # Deactivate old view
        if player.current_view:
            player.current_view.stop()

        view = MusicControlsView(player, self)
        embed = em.now_playing_track(
            player.current,
            position=player.position,
            is_paused=player.is_paused,
            volume=player.volume,
            queue_len=len(player.queue),
        )
        msg = await interaction.followup.send(embed=embed, view=view)
        view.message = msg
        player.current_view = view

    @app_commands.command(name="volume", description="Set volume (1–250).")
    @app_commands.describe(level="Volume from 1 to 250")
    @app_commands.guild_only()
    async def volume(self, interaction: discord.Interaction, level: int) -> None:
        await interaction.response.defer(ephemeral=True)
        guild = cast(discord.Guild, interaction.guild)
        player = self._get_player(guild)

        if not player:
            await interaction.followup.send(embed=em.error("I'm not connected."), ephemeral=True)
            return

        if not 1 <= level <= 250:
            await interaction.followup.send(
                embed=em.error("Volume must be between **1** and **250**."), ephemeral=True
            )
            return

        player.set_volume(level)
        if player.current_view:
            await player.current_view.update_message()
        icon = "🔇" if level <= 5 else ("🔉" if level < 60 else "🔊")
        await interaction.followup.send(
            embed=em.success(f"{icon} Volume set to **{level}%**."), ephemeral=True
        )

    @app_commands.command(name="clear", description="Clear the queue.")
    @app_commands.guild_only()
    async def clear(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        guild = cast(discord.Guild, interaction.guild)
        player = self._get_player(guild)

        if not player:
            await interaction.followup.send(embed=em.error("I'm not connected."), ephemeral=True)
            return

        count = len(player.queue)
        player.queue.clear()
        if player.current_view:
            await player.current_view.update_message()
        await interaction.followup.send(
            embed=em.success(f"Cleared **{count}** track(s) from the queue."), ephemeral=True
        )

    @commands.Cog.listener()
    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        if member != self.bot.user:
            return
        if before.channel and not after.channel:
            guild = member.guild
            player = self._players.pop(guild.id, None)
            if player:
                await player.disconnect()

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Music(bot))
