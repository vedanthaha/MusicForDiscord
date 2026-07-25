"""
bot.py – Discord bot entry point.

Responsibilities:
  - Configure structured logging (coloured console + optional file handler)
  - Build the Bot subclass with correct intents and setup_hook
  - Connect the Lavalink node pool inside setup_hook
  - Load all cogs
  - Run the bot
"""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os
import sys
from pathlib import Path

import discord
from discord.ext import commands

import colorlog

from config import cfg

# ── Logging setup ─────────────────────────────────────────────────────────────


def _configure_logging() -> None:
    """Set up a coloured console handler plus an optional rotating file handler."""
    root = logging.getLogger()
    root.setLevel(cfg.log_level)

    # Console – coloured
    console = colorlog.StreamHandler(stream=sys.stdout)
    console.setFormatter(
        colorlog.ColoredFormatter(
            "%(log_color)s%(asctime)s %(levelname)-8s%(reset)s "
            "%(cyan)s%(name)s%(reset)s – %(message)s",
            datefmt="%H:%M:%S",
            log_colors={
                "DEBUG": "white",
                "INFO": "green",
                "WARNING": "yellow",
                "ERROR": "red",
                "CRITICAL": "bold_red",
            },
        )
    )
    root.addHandler(console)

    # File – rotating, 5 MB × 3 backups
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "bot.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-8s %(name)s – %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root.addHandler(file_handler)

    # Silence noisy third-party loggers
    for noisy in ("discord.gateway", "discord.http"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


logger = logging.getLogger(__name__)

# ── Bot class ─────────────────────────────────────────────────────────────────

COGS: list[str] = [
    "cogs.music",
]


class MusicBot(commands.Bot):
    """Production Discord music bot."""

    def __init__(self) -> None:
        intents = discord.Intents.default()
        # voice_states required so discord.py can track who's in channels
        intents.voice_states = True
        # We do NOT need message_content for a slash-command-only bot
        intents.message_content = False

        super().__init__(
            command_prefix=commands.when_mentioned,  # prefix-based cmds disabled
            intents=intents,
            help_command=None,
        )

    async def setup_hook(self) -> None:
        """Called by discord.py before the bot connects to the gateway."""
        # Load cogs
        for cog in COGS:
            try:
                await self.load_extension(cog)
                logger.info("Loaded cog: %s", cog)
            except Exception as exc:
                logger.error("Failed to load cog %s: %s", cog, exc, exc_info=True)

        # Sync application commands
        if cfg.dev_guild_id:
            guild_obj = discord.Object(id=cfg.dev_guild_id)
            self.tree.copy_global_to(guild=guild_obj)
            synced = await self.tree.sync(guild=guild_obj)
            logger.info(
                "Synced %d command(s) to dev guild %d",
                len(synced),
                cfg.dev_guild_id,
            )
        else:
            synced = await self.tree.sync()
            logger.info("Synced %d command(s) globally", len(synced))

    async def on_ready(self) -> None:
        assert self.user is not None
        logger.info("Logged in as %s (ID: %d)", self.user, self.user.id)
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.listening,
                name="/play",
            )
        )

    async def on_command_error(
        self,
        ctx: commands.Context,  # type: ignore[override]
        error: commands.CommandError,
    ) -> None:
        """Swallow or log prefix-command errors (bot is slash-only)."""
        if isinstance(error, commands.CommandNotFound):
            return
        logger.error("Unexpected command error: %s", error, exc_info=True)

    async def on_app_command_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
    ) -> None:
        """
        Global error handler for slash commands.

        Individual commands defer and handle their own errors; this catches
        anything that slips through.
        """
        logger.error(
            "Unhandled app command error in %r: %s",
            interaction.command.name if interaction.command else "unknown",
            error,
            exc_info=True,
        )

        msg = "An unexpected error occurred. Please try again later."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except Exception:
            pass  # interaction already expired


# ── Keep-alive Health Check Server (for Render + UptimeRobot) ───────────────


async def _start_health_server() -> Any:
    port_str = os.getenv("PORT", "8080").strip()
    try:
        from aiohttp import web
        port = int(port_str)
        app = web.Application()

        async def health_check(request: web.Request) -> web.Response:
            return web.Response(text="Bot is online and healthy!", status=200)

        app.router.add_get("/", health_check)
        app.router.add_get("/health", health_check)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
        logger.info("Keep-alive HTTP health server running on port %d", port)
        return runner
    except Exception as exc:
        logger.warning("Could not start keep-alive health server: %s", exc)
        return None


# ── Entry point ───────────────────────────────────────────────────────────────


async def main() -> None:
    _configure_logging()
    health_runner = await _start_health_server()
    bot = MusicBot()

    try:
        async with bot:
            await bot.start(cfg.discord_token)
    finally:
        if health_runner:
            await health_runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot shut down by user.")
