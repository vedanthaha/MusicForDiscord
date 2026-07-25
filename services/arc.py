"""
services/arc.py – Arc Music API client.

Resolves any user query (YouTube URL, Spotify URL, plain-text search, …)
into a publicly-streamable MP3 URL by:

  1. Detecting the source provider from the query string.
  2. Extracting (or searching for) the YouTube Video ID.
  3. Checking Redis (or in-process LRU) for a previously cached URL.
  4. If not cached: submitting a download job to the Arc API, polling until
     complete, caching the result, and returning the public URL.
  5. Falling back to native Lavalink search strings when Arc is disabled or
     the source is not YouTube.

## Independence guarantee
This module has zero imports from discord.py or the bot framework.  It can be
imported and used from any Python async context.

## Thread safety
The aiohttp session and the asyncio Semaphore are coroutine-safe. The
in-process LRU cache uses a plain dict protected by asyncio.Lock; it is not
safe to share across processes (use Redis for that).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any

import aiohttp

from config import cfg

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Structured exceptions
# ─────────────────────────────────────────────────────────────────────────────


class ArcError(Exception):
    """Base class for all Arc API errors."""


class ArcTimeoutError(ArcError):
    """A download job did not complete within the configured timeout."""

    def __init__(self, job_id: str, timeout: float) -> None:
        super().__init__(
            f"Arc job {job_id!r} timed out after {timeout:.0f}s"
        )
        self.job_id = job_id
        self.timeout = timeout


class ArcJobFailedError(ArcError):
    """The Arc API reported a non-success job status."""

    def __init__(self, job_id: str, status: str) -> None:
        super().__init__(
            f"Arc job {job_id!r} ended with status {status!r}"
        )
        self.job_id = job_id
        self.status = status


class ArcRateLimitError(ArcError):
    """HTTP 429 received from the Arc API."""

    def __init__(self, retry_after: float | None = None) -> None:
        msg = "Arc API rate limit exceeded"
        if retry_after is not None:
            msg += f" – retry after {retry_after:.1f}s"
        super().__init__(msg)
        self.retry_after = retry_after


class ArcHTTPError(ArcError):
    """Unexpected non-2xx response from the Arc API."""

    def __init__(self, status: int, url: str) -> None:
        super().__init__(f"Arc API returned HTTP {status} for {url}")
        self.status = status
        self.url = url


class ArcResolveError(ArcError):
    """Could not resolve the query to a YouTube Video ID."""


# ─────────────────────────────────────────────────────────────────────────────
# Provider detection
# ─────────────────────────────────────────────────────────────────────────────


class Provider(Enum):
    YOUTUBE = auto()
    YOUTUBE_MUSIC = auto()
    SPOTIFY = auto()
    SOUNDCLOUD = auto()
    BANDCAMP = auto()
    TWITCH = auto()
    HTTP = auto()       # generic direct-stream URL
    PLAIN = auto()      # plain-text search query


_YT_URL_RE = re.compile(
    r"^(https?://)?(www\.)?(youtube\.com/(watch|playlist|shorts)|youtu\.be/)",
    re.IGNORECASE,
)
_YT_MUSIC_RE = re.compile(r"^(https?://)?music\.youtube\.com", re.IGNORECASE)
_SPOTIFY_RE = re.compile(r"^(https?://)?open\.spotify\.com", re.IGNORECASE)
_SOUNDCLOUD_RE = re.compile(r"^(https?://)?(www\.)?soundcloud\.com", re.IGNORECASE)
_BANDCAMP_RE = re.compile(r"^(https?://)?[a-zA-Z0-9-]+\.bandcamp\.com", re.IGNORECASE)
_TWITCH_RE = re.compile(r"^(https?://)?(www\.)?twitch\.tv", re.IGNORECASE)
_HTTP_RE = re.compile(r"^https?://", re.IGNORECASE)

# Extracts the 11-character video ID from any YouTube URL variant
_YT_ID_RE = re.compile(
    r"(?:v=|youtu\.be/|embed/|shorts/|/v/|/e/)([A-Za-z0-9_-]{11})"
)


def detect_provider(query: str) -> Provider:
    """Classify a raw user query into its most likely source provider."""
    q = query.strip()
    if _YT_MUSIC_RE.match(q):
        return Provider.YOUTUBE_MUSIC
    if _YT_URL_RE.match(q):
        return Provider.YOUTUBE
    if _SPOTIFY_RE.match(q):
        return Provider.SPOTIFY
    if _SOUNDCLOUD_RE.match(q):
        return Provider.SOUNDCLOUD
    if _BANDCAMP_RE.match(q):
        return Provider.BANDCAMP
    if _TWITCH_RE.match(q):
        return Provider.TWITCH
    if _HTTP_RE.match(q):
        return Provider.HTTP
    return Provider.PLAIN


def extract_youtube_id(url: str) -> str | None:
    """Return the 11-character YouTube video ID from a URL, or None."""
    m = _YT_ID_RE.search(url)
    return m.group(1) if m else None


# ─────────────────────────────────────────────────────────────────────────────
# Lavalink native search prefixes  (used when Arc is disabled / not applicable)
# ─────────────────────────────────────────────────────────────────────────────

_NATIVE_PREFIX: dict[Provider, str] = {
    Provider.YOUTUBE:       "",          # pass URL directly
    Provider.YOUTUBE_MUSIC: "",
    Provider.SPOTIFY:       "",          # LavaSrc handles open.spotify.com URLs
    Provider.SOUNDCLOUD:    "",          # pass URL directly
    Provider.BANDCAMP:      "",
    Provider.TWITCH:        "",
    Provider.HTTP:          "",
    Provider.PLAIN:         "ytsearch:", # text → YouTube search
}


# ─────────────────────────────────────────────────────────────────────────────
# Resolved query – the object returned to callers
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class ResolvedQuery:
    """
    Final resolution result.

    Attributes
    ----------
    lavalink_query:
        String to pass directly to ``wavelink.Playable.search()``.
        When ``via_arc`` is True this will be the full HTTP stream URL
        (``https://api.arcmusic.fun/media/VIDEO_ID.mp3``).
        Otherwise it is a native Lavalink search string or URL.
    provider:
        Detected source provider.
    original:
        The user's raw, unmodified input.
    video_id:
        YouTube video ID, if known.
    via_arc:
        True when the URL was resolved through the Arc API.
    """

    lavalink_query: str
    provider: Provider
    original: str
    video_id: str | None = None
    via_arc: bool = False
    title: str | None = None        # human-readable track title (from yt-dlp)
    thumbnail: str | None = None    # album-art URL for embeds
    duration: int | None = None     # duration in seconds


# ─────────────────────────────────────────────────────────────────────────────
# In-process LRU cache  (fallback when Redis is not configured)
# ─────────────────────────────────────────────────────────────────────────────


class _LRUCache:
    """Thread-safe (asyncio-safe) LRU dict with per-entry TTL."""

    def __init__(self, maxsize: int = 512) -> None:
        self._data: OrderedDict[str, tuple[str, float]] = OrderedDict()
        self._lock = asyncio.Lock()
        self._maxsize = maxsize

    async def get(self, key: str) -> str | None:
        async with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            value, expiry = entry
            if time.monotonic() > expiry:
                del self._data[key]
                return None
            # Move to end (most-recently used)
            self._data.move_to_end(key)
            return value

    async def set(self, key: str, value: str, ttl: int) -> None:
        async with self._lock:
            expiry = time.monotonic() + ttl
            self._data[key] = (value, expiry)
            self._data.move_to_end(key)
            if len(self._data) > self._maxsize:
                self._data.popitem(last=False)  # evict oldest


# ─────────────────────────────────────────────────────────────────────────────
# YouTube ID resolver  (runs yt-dlp in a thread executor to stay non-blocking)
# ─────────────────────────────────────────────────────────────────────────────


# Tuple returned by yt-dlp helper: (video_id, title, thumbnail_url, duration_seconds)
_YtMeta = tuple[str | None, str | None, str | None, int | None]


def _sync_resolve_youtube_meta(query: str) -> _YtMeta:
    """
    Synchronous helper: use yt-dlp to get YouTube result metadata.
    Returns (video_id, title, thumbnail_url, duration_seconds).
    Must be called via run_in_executor.
    """
    try:
        import yt_dlp  # noqa: PLC0415 – intentional late import

        is_url = query.startswith("http://") or query.startswith("https://") or "youtube.com" in query or "youtu.be" in query

        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
            "default_search": "ytsearch",
            "logger": logging.getLogger("yt_dlp"),
        }
        
        if not is_url:
            opts["extract_flat"] = "in_playlist"

        target = query if is_url else f"ytsearch1:{query}"

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(target, download=False)
            if not info:
                return None, None, None, None
            entries = info.get("entries") or []
            entry = entries[0] if entries else info
            vid = entry.get("id")
            title = entry.get("title")
            
            # Extract thumbnail: try flat field, fall back to last thumbnails list item (highest res)
            thumbnail = entry.get("thumbnail")
            thumbnails = entry.get("thumbnails") or []
            if not thumbnail and thumbnails:
                thumbnail = thumbnails[-1].get("url")
                
            duration = entry.get("duration")  # seconds (int or float)
            return vid, title, thumbnail, int(duration) if duration else None
    except Exception as exc:
        logger.warning("yt-dlp resolution failed for %r: %s", query, exc)
        return None, None, None, None


async def _resolve_youtube_meta(
    query: str, provider: Provider
) -> _YtMeta:
    """
    Async wrapper: resolve a user query to YouTube metadata.

    For YouTube URLs: extract the ID via regex (fast path), then fall through
    to yt-dlp for the rest of the metadata.
    For plain-text / Spotify: delegate entirely to yt-dlp in an executor.
    """
    # For Spotify: resolve track name via oEmbed first, then search YouTube
    if provider == Provider.SPOTIFY:
        title = await _spotify_track_title(query)
        if title:
            query = title

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _sync_resolve_youtube_meta, query)


async def _spotify_track_title(spotify_url: str) -> str | None:
    """
    Use Spotify's public oEmbed endpoint (no API key needed) to get a
    human-readable title string like "Song Name · Artist" for use in a
    YouTube search.
    """
    oembed_url = f"https://open.spotify.com/oembed?url={spotify_url}"
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=5)
        ) as s:
            async with s.get(oembed_url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("title")
    except Exception as exc:
        logger.debug("Spotify oEmbed failed for %r: %s", spotify_url, exc)
    return None


async def _extract_spotify_playlist(url: str) -> tuple[str | None, list[str]]:
    """Extract playlist/album title and list of track queries from Spotify URL."""
    m = re.search(r"open\.spotify\.com/(playlist|album)/([a-zA-Z0-9]+)", url)
    if not m:
        return None, []
    item_type, item_id = m.group(1), m.group(2)
    embed_url = f"https://open.spotify.com/embed/{item_type}/{item_id}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    try:
        async with aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as session:
            async with session.get(embed_url) as resp:
                if resp.status != 200:
                    return None, []
                html = await resp.text()
                match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.DOTALL)
                if not match:
                    return None, []
                data = json.loads(match.group(1))
                props = data.get("props", {}).get("pageProps", {})
                state = props.get("state", {}).get("data", {})
                entity = state.get("entity", {})
                playlist_title = entity.get("title") or entity.get("name") or f"Spotify {item_type.capitalize()}"
                
                track_list = entity.get("trackList", []) or entity.get("tracks", {}).get("items", [])
                tracks = []
                for t in track_list:
                    title = t.get("title") or t.get("name")
                    subtitle = t.get("subtitle") or ""
                    artists = ", ".join([a.get("name", "") for a in t.get("artists", [])]) if t.get("artists") else subtitle
                    if title:
                        tracks.append(f"{title} {artists}".strip())
                return playlist_title, tracks
    except Exception as exc:
        logger.warning("Spotify playlist extraction error for %s: %s", url, exc)
        return None, []


def _sync_extract_youtube_playlist(url: str) -> tuple[str | None, list[str]]:
    """Extract playlist title and video queries from a YouTube playlist URL using yt-dlp."""
    try:
        import yt_dlp
        opts = {
            "quiet": True,
            "extract_flat": "in_playlist",
            "skip_download": True,
            "noplaylist": False,
            "logger": logging.getLogger("yt_dlp"),
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                return None, []
            pl_title = info.get("title") or "YouTube Playlist"
            entries = info.get("entries") or []
            queries = []
            for e in entries:
                vid = e.get("id")
                title = e.get("title")
                if vid:
                    queries.append(f"https://www.youtube.com/watch?v={vid}")
                elif title:
                    queries.append(title)
            return pl_title, queries
    except Exception as exc:
        logger.warning("YouTube playlist extraction error for %s: %s", url, exc)
        return None, []



# ─────────────────────────────────────────────────────────────────────────────
# Arc Music API client
# ─────────────────────────────────────────────────────────────────────────────

_POLL_INITIAL_DELAY: float = 2.0    # seconds before first poll
_POLL_BACKOFF_FACTOR: float = 1.5   # multiply delay on each retry
_POLL_MAX_DELAY: float = 15.0       # cap individual poll interval


class ArcClient:
    """
    Async client for the Arc Music API (https://api.arcmusic.fun).

    Usage
    -----
    The cog creates one instance, calls ``await arc.start()`` in cog_load,
    calls ``await arc.close()`` in cog_unload, and uses
    ``await arc.resolve(query)`` for every play request.

    The client is entirely independent of Discord; it can be used from
    any async Python application.
    """

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._redis: Any | None = None               # redis.asyncio.Redis
        self._mem_cache: _LRUCache = _LRUCache(maxsize=1024)
        self._semaphore: asyncio.Semaphore = asyncio.Semaphore(
            cfg.arc.max_concurrency
        )
        self._enabled: bool = cfg.arc.enabled

        if self._enabled:
            logger.info(
                "Arc client initialised (base=%s, timeout=%.0fs, concurrency=%d, ttl=%ds)",
                cfg.arc.base_url,
                cfg.arc.job_timeout,
                cfg.arc.max_concurrency,
                cfg.arc.cache_ttl,
            )
        else:
            logger.info(
                "Arc API disabled (ARC_API_KEY not set) – using Lavalink native search."
            )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """
        Create the aiohttp session and connect to Redis (if configured).
        Call once at startup; idempotent.
        """
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(
                limit=20,                   # max total connections
                limit_per_host=10,          # max connections per host
                enable_cleanup_closed=True,
            )
            self._session = aiohttp.ClientSession(
                base_url=cfg.arc.base_url,
                connector=connector,
                timeout=aiohttp.ClientTimeout(total=15, connect=5),
                headers={
                    "User-Agent": "DiscordMusicBot/2.0 (aiohttp)",
                    "Accept": "application/json",
                },
            )

        if cfg.redis.enabled and self._redis is None:
            try:
                import redis.asyncio as aioredis  # noqa: PLC0415

                self._redis = aioredis.from_url(
                    cfg.redis.url,
                    encoding="utf-8",
                    decode_responses=True,
                    socket_connect_timeout=3,
                    socket_timeout=3,
                )
                # Verify connectivity
                await self._redis.ping()
                logger.info("Arc client connected to Redis at %s", cfg.redis.url)
            except Exception as exc:
                logger.warning(
                    "Redis unavailable (%s) – falling back to in-process cache.", exc
                )
                self._redis = None

    async def close(self) -> None:
        """Close the aiohttp session and Redis connection. Call on shutdown."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

        if self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception:
                pass
            self._redis = None

    # ── Public API ─────────────────────────────────────────────────────────────

    async def resolve_playlist(self, query: str) -> tuple[str | None, list[str]]:
        """
        Check if query is a Spotify/YouTube playlist or album and return
        (playlist_title, list_of_track_queries). Returns (None, []) if not a playlist.
        """
        q = query.strip()
        if "open.spotify.com/playlist/" in q or "open.spotify.com/album/" in q:
            return await _extract_spotify_playlist(q)
        
        if ("youtube.com/playlist" in q or "list=" in q) and "watch?v=" not in q:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, _sync_extract_youtube_playlist, q)
            
        return None, []

    async def resolve(self, query: str) -> ResolvedQuery:
        """
        Resolve a user query into a ``ResolvedQuery``.

        When Arc is enabled and the query can be mapped to a YouTube Video ID:
          - Return an Arc HTTP stream URL as ``lavalink_query``.
          - ``via_arc`` will be True.

        When Arc is disabled, times out, or the provider is unsupported:
          - Return the appropriate Lavalink native search string.
          - ``via_arc`` will be False.

        This method never raises – Arc failures always fall back gracefully.
        """
        provider = detect_provider(query)

        if not self._enabled:
            return self._native_fallback(query, provider)

        # Only YouTube and Spotify go through Arc; everything else uses
        # Lavalink's native source managers (SoundCloud, Bandcamp, etc.)
        if provider not in (
            Provider.YOUTUBE,
            Provider.YOUTUBE_MUSIC,
            Provider.SPOTIFY,
            Provider.PLAIN,
        ):
            return self._native_fallback(query, provider)

        try:
            return await self._resolve_via_arc(query, provider)
        except ArcError as exc:
            logger.warning(
                "Arc resolution failed for %r (%s) – falling back to Lavalink. Reason: %s",
                query,
                provider.name,
                exc,
            )
            return self._native_fallback(query, provider)
        except Exception as exc:
            logger.error(
                "Unexpected error in Arc resolution for %r: %s",
                query,
                exc,
                exc_info=True,
            )
            return self._native_fallback(query, provider)

    # ── Internal resolution pipeline ──────────────────────────────────────────

    async def _resolve_via_arc(
        self, query: str, provider: Provider
    ) -> ResolvedQuery:
        """Full Arc resolution pipeline: ID → cache → job → return URL."""
        # Step 1: Resolve YouTube metadata (ID + title + thumbnail + duration)
        video_id, title, thumbnail, duration = await _resolve_youtube_meta(
            query, provider
        )
        if not video_id:
            raise ArcResolveError(
                f"Could not resolve a YouTube Video ID for {query!r}"
            )

        logger.debug(
            "Resolved YouTube meta: %r → id=%s title=%r", query, video_id, title
        )

        # Step 2: Cache lookup
        cached_url = await self._cache_get(video_id)
        if cached_url:
            logger.info("Arc cache hit for video_id=%s", video_id)
            return ResolvedQuery(
                lavalink_query=cached_url,
                provider=provider,
                original=query,
                video_id=video_id,
                via_arc=True,
                title=title,
                thumbnail=thumbnail,
                duration=duration,
            )

        # Step 3: Submit Arc download job (rate-limited by semaphore)
        async with self._semaphore:
            public_url = await self._download_and_cache(video_id)

        return ResolvedQuery(
            lavalink_query=public_url,
            provider=provider,
            original=query,
            video_id=video_id,
            via_arc=True,
            title=title,
            thumbnail=thumbnail,
            duration=duration,
        )

    async def _download_and_cache(self, video_id: str) -> str:
        """Submit job, poll to completion, cache, and return full URL."""
        job_id = await self._start_job(video_id)
        public_path = await self._poll_until_done(job_id, video_id)

        # Build the full, absolute URL Lavalink can stream
        full_url = f"{cfg.arc.base_url.rstrip('/')}{public_path}"

        await self._cache_set(video_id, full_url)
        logger.info("Arc resolved and cached: video_id=%s → %s", video_id, full_url)
        return full_url

    # ── Arc API calls ─────────────────────────────────────────────────────────

    async def _start_job(self, video_id: str) -> str:
        """
        POST /youtube/v2/download → returns job_id.

        Raises
        ------
        ArcRateLimitError
            On HTTP 429.
        ArcHTTPError
            On any other non-200 response.
        ArcError
            On missing job_id in the response body.
        """
        assert self._session is not None, "ArcClient.start() was not called"

        params = {
            "query": video_id,
            "isVideo": "false",
            "api_key": cfg.arc.api_key,
        }

        logger.info("Arc: starting download job for video_id=%s", video_id)

        async with self._session.get(
            "/youtube/v2/download", params=params
        ) as resp:
            logger.debug(
                "Arc /youtube/v2/download → HTTP %d (video_id=%s)",
                resp.status,
                video_id,
            )

            if resp.status == 429:
                retry_after: float | None = None
                try:
                    retry_after = float(resp.headers.get("Retry-After", ""))
                except (TypeError, ValueError):
                    pass
                raise ArcRateLimitError(retry_after)

            if resp.status != 200:
                raise ArcHTTPError(resp.status, str(resp.url))

            data: dict[str, Any] = await resp.json()

        job_id: str | None = data.get("job_id")
        if not job_id:
            raise ArcError(
                f"Arc API response missing 'job_id' for video_id={video_id!r}: {data}"
            )

        logger.info(
            "Arc job started: job_id=%s status=%s (video_id=%s)",
            job_id,
            data.get("status"),
            video_id,
        )
        return job_id

    async def _poll_until_done(self, job_id: str, video_id: str) -> str:
        """
        Poll /youtube/jobStatus until status == "done" or timeout expires.

        Uses exponential backoff between polls.

        Returns
        -------
        str
            The ``public_url`` path (e.g. ``/media/gJLVTKhTnog.mp3``).

        Raises
        ------
        ArcTimeoutError
            If the job does not complete within ``cfg.arc.job_timeout`` seconds.
        ArcJobFailedError
            If the job reports a non-success result.
        ArcHTTPError
            On non-200 HTTP responses.
        """
        assert self._session is not None

        deadline = asyncio.get_event_loop().time() + cfg.arc.job_timeout
        delay = _POLL_INITIAL_DELAY
        attempt = 0

        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise ArcTimeoutError(job_id, cfg.arc.job_timeout)

            # Wait before polling (first wait gives the server time to start)
            await asyncio.sleep(min(delay, remaining))

            attempt += 1
            logger.debug(
                "Arc: polling job_id=%s attempt=%d delay=%.1fs",
                job_id,
                attempt,
                delay,
            )

            async with self._session.get(
                "/youtube/jobStatus", params={"job_id": job_id}
            ) as resp:
                logger.debug(
                    "Arc /youtube/jobStatus → HTTP %d (job_id=%s attempt=%d)",
                    resp.status,
                    job_id,
                    attempt,
                )

                if resp.status == 429:
                    retry_after = None
                    try:
                        retry_after = float(resp.headers.get("Retry-After", ""))
                    except (TypeError, ValueError):
                        pass
                    raise ArcRateLimitError(retry_after)

                if resp.status != 200:
                    raise ArcHTTPError(resp.status, str(resp.url))

                data: dict[str, Any] = await resp.json()

            job_data: dict[str, Any] = data.get("job", {})
            job_status: str = job_data.get("status", "")

            if job_status == "done":
                result: dict[str, Any] = job_data.get("result", {})
                if not result.get("success"):
                    raise ArcJobFailedError(job_id, f"result.success=False: {result}")

                public_url: str | None = result.get("public_url")
                if not public_url:
                    raise ArcJobFailedError(
                        job_id, f"'public_url' missing in result: {result}"
                    )

                logger.info(
                    "Arc job done: job_id=%s video_id=%s url=%s",
                    job_id,
                    video_id,
                    public_url,
                )
                return public_url

            if job_status in ("failed", "error"):
                raise ArcJobFailedError(job_id, job_status)

            # Still queued / processing → increase backoff
            delay = min(delay * _POLL_BACKOFF_FACTOR, _POLL_MAX_DELAY)

    # ── Cache helpers ─────────────────────────────────────────────────────────

    def _cache_key(self, video_id: str) -> str:
        return f"arc:url:{video_id}"

    async def _cache_get(self, video_id: str) -> str | None:
        """Return the cached public URL for a video ID, or None."""
        key = self._cache_key(video_id)
        if self._redis is not None:
            try:
                return await self._redis.get(key)
            except Exception as exc:
                logger.warning("Redis GET failed (%s); trying mem cache.", exc)
        return await self._mem_cache.get(key)

    async def _cache_set(self, video_id: str, url: str) -> None:
        """Store the public URL in Redis (primary) and mem cache (backup)."""
        key = self._cache_key(video_id)
        ttl = cfg.arc.cache_ttl

        if self._redis is not None:
            try:
                await self._redis.setex(key, ttl, url)
            except Exception as exc:
                logger.warning("Redis SETEX failed (%s); using mem cache only.", exc)

        await self._mem_cache.set(key, url, ttl)

    # ── Native fallback builder ───────────────────────────────────────────────

    @staticmethod
    def _native_fallback(query: str, provider: Provider) -> ResolvedQuery:
        """Build a native Lavalink search query for providers Arc cannot handle."""
        prefix = _NATIVE_PREFIX.get(provider, "ytsearch:")
        lavalink_query = f"{prefix}{query}" if prefix else query
        logger.debug(
            "Native fallback: provider=%s → %r", provider.name, lavalink_query
        )
        return ResolvedQuery(
            lavalink_query=lavalink_query,
            provider=provider,
            original=query,
            via_arc=False,
        )
