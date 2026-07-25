"""
services/lavalink.py – Lavalink node lifecycle management.

Handles:
  - Node pool initialisation at bot startup
  - Node reconnection strategy
  - Helper to retrieve the active player for a guild
"""

from __future__ import annotations

import logging

import discord
import wavelink

from config import cfg

logger = logging.getLogger(__name__)


async def connect_nodes(client: discord.Client) -> None:
    """
    Connect to the Lavalink node(s) defined in config.

    Called once from `Bot.setup_hook`. Wavelink will automatically
    attempt reconnection if the node drops; set `inactive_player_timeout`
    here to let the library fire the inactive-player event after silence.
    """
    node = wavelink.Node(
        uri=cfg.lavalink.uri,
        password=cfg.lavalink.password,
        inactive_player_timeout=cfg.alone_timeout,
    )

    try:
        await wavelink.Pool.connect(nodes=[node], client=client, cache_capacity=200)
        logger.info("Lavalink node pool connected to %s", cfg.lavalink.uri)
    except Exception as exc:
        # A failure here means the Lavalink server is down at startup.
        # The bot will still start; Wavelink retries automatically.
        logger.error(
            "Failed to connect to Lavalink node at startup: %s — "
            "Wavelink will retry in the background.",
            exc,
        )


def get_player(guild: discord.Guild) -> wavelink.Player | None:
    """Return the active Wavelink player for a guild, or None."""
    return guild.voice_client  # type: ignore[return-value]
