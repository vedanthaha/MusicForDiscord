"""
config.py – Central configuration loaded from environment / .env file.

All other modules import from here; nothing reads os.environ directly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Load .env file that sits next to this module (or in CWD)
_env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=_env_path if _env_path.exists() else None)


def _required(key: str) -> str:
    """Return the env-var value or raise a clear error at startup."""
    value = os.getenv(key, "").strip()
    if not value:
        raise RuntimeError(
            f"Required environment variable '{key}' is not set. "
            "Copy .env.example to .env and fill in the values."
        )
    return value


def _optional(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def _int(key: str, default: int) -> int:
    raw = os.getenv(key, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise RuntimeError(
            f"Environment variable '{key}' must be an integer, got: {raw!r}"
        )


def _float(key: str, default: float) -> float:
    raw = os.getenv(key, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        raise RuntimeError(
            f"Environment variable '{key}' must be a number, got: {raw!r}"
        )


def _bool(key: str, default: bool = False) -> bool:
    raw = os.getenv(key, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


# ─────────────────────────────────────────────────────────────────────────────
# Config dataclasses
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LavalinkConfig:
    host: str = field(default_factory=lambda: _optional("LAVALINK_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _int("LAVALINK_PORT", 2333))
    password: str = field(
        default_factory=lambda: _optional("LAVALINK_PASSWORD", "youshallnotpass")
    )
    secure: bool = field(default_factory=lambda: _bool("LAVALINK_SECURE", False))

    @property
    def uri(self) -> str:
        scheme = "https" if self.secure else "http"
        return f"{scheme}://{self.host}:{self.port}"


@dataclass(frozen=True)
class ArcConfig:
    api_key: str = field(default_factory=lambda: _optional("ARC_API_KEY", ""))
    base_url: str = field(
        default_factory=lambda: _optional("ARC_BASE_URL", "https://api.arcmusic.fun")
    )
    job_timeout: float = field(
        default_factory=lambda: _float("ARC_JOB_TIMEOUT", 60.0)
    )
    max_concurrency: int = field(
        default_factory=lambda: _int("ARC_MAX_CONCURRENCY", 5)
    )
    cache_ttl: int = field(
        default_factory=lambda: _int("ARC_CACHE_TTL", 86400)
    )

    @property
    def enabled(self) -> bool:
        """Arc integration is active only when an API key is provided."""
        return bool(self.api_key)


@dataclass(frozen=True)
class RedisConfig:
    url: str = field(
        default_factory=lambda: _optional("REDIS_URL", "")
    )

    @property
    def enabled(self) -> bool:
        return bool(self.url)


@dataclass(frozen=True)
class BotConfig:
    discord_token: str = field(default_factory=lambda: _required("DISCORD_TOKEN"))
    dev_guild_id: int | None = field(
        default_factory=lambda: (
            int(raw) if (raw := _optional("DEV_GUILD_ID")) else None
        )
    )
    alone_timeout: int = field(default_factory=lambda: _int("ALONE_TIMEOUT", 180))
    default_volume: int = field(default_factory=lambda: _int("DEFAULT_VOLUME", 80))
    log_level: str = field(
        default_factory=lambda: _optional("LOG_LEVEL", "INFO").upper()
    )

    lavalink: LavalinkConfig = field(default_factory=LavalinkConfig)
    arc: ArcConfig = field(default_factory=ArcConfig)
    redis: RedisConfig = field(default_factory=RedisConfig)


# ── Singleton ─────────────────────────────────────────────────────────────────
# Instantiated once; all modules import `cfg` directly.
cfg: BotConfig = BotConfig()
