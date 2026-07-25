"""
services/lyrics.py – Fetch and parse synced (LRC) lyrics from LRCLIB.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import List, Tuple

import aiohttp

logger = logging.getLogger(__name__)


@dataclass
class ParsedLyrics:
    title: str
    artist: str
    is_synced: bool
    synced_lines: List[Tuple[float, str]]  # list of (timestamp_in_seconds, line_text)
    plain_text: str

    def get_synced_window(self, current_time: float, context_lines: int = 2) -> str:
        """
        Return a formatted string representing the lyrics around current_time,
        with the active line highlighted.
        """
        if not self.is_synced or not self.synced_lines:
            # Fallback to plain text snippet if not synced
            return self.plain_text[:1000] if self.plain_text else "No lyrics available."

        # Find the active line index
        active_idx = 0
        for i, (ts, text) in enumerate(self.synced_lines):
            if current_time >= ts:
                active_idx = i
            else:
                break

        start_idx = max(0, active_idx - context_lines)
        end_idx = min(len(self.synced_lines), active_idx + context_lines + 3)

        formatted_lines = []
        for i in range(start_idx, end_idx):
            ts, text = self.synced_lines[i]
            if not text.strip():
                continue
            if i == active_idx:
                formatted_lines.append(f"👉 **{text}**")
            else:
                formatted_lines.append(f"  *{text}*")

        return "\n".join(formatted_lines) if formatted_lines else "♪ Instrumental / Music Playing ♪"


_LRC_TIMESTAMP_RE = re.compile(r"\[(\d{2}):(\d{2})\.(\d{2,3})\](.*)")


def _parse_lrc(synced_text: str) -> List[Tuple[float, str]]:
    """Parse LRC text into sorted list of (time_in_seconds, text)."""
    lines: List[Tuple[float, str]] = []
    for line in synced_text.splitlines():
        match = _LRC_TIMESTAMP_RE.match(line.strip())
        if match:
            minutes = int(match.group(1))
            seconds = int(match.group(2))
            millis = int(match.group(3))
            if len(match.group(3)) == 2:
                millis *= 10
            total_sec = minutes * 60 + seconds + (millis / 1000.0)
            text = match.group(4).strip()
            lines.append((total_sec, text))
    lines.sort(key=lambda x: x[0])
    return lines


async def fetch_lyrics(query: str) -> ParsedLyrics | None:
    """
    Fetch synced/plain lyrics from LRCLIB for the given query/track title.
    """
    # Clean query (remove (Official Video), [MV], ft., etc.)
    clean_q = re.sub(r"[\(\[\{].*?[\)\]\}]", "", query)
    clean_q = re.sub(r"\b(official video|audio|lyric video|hd|4k)\b", "", clean_q, flags=re.IGNORECASE).strip()

    url = f"https://lrclib.net/api/search?q={aiohttp.helpers.quote(clean_q)}"
    headers = {
        "User-Agent": "MusicBot/1.0 (Discord Bot)"
    }

    try:
        async with aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                results = await resp.json()
                if not results or not isinstance(results, list):
                    return None

                # Find result with synced lyrics if possible
                best_item = None
                for item in results:
                    if item.get("syncedLyrics"):
                        best_item = item
                        break
                if not best_item:
                    best_item = results[0]

                synced_raw = best_item.get("syncedLyrics")
                plain_raw = best_item.get("plainLyrics") or ""
                title = best_item.get("trackName") or query
                artist = best_item.get("artistName") or ""

                if synced_raw:
                    parsed_lines = _parse_lrc(synced_raw)
                    if parsed_lines:
                        return ParsedLyrics(
                            title=title,
                            artist=artist,
                            is_synced=True,
                            synced_lines=parsed_lines,
                            plain_text=plain_raw,
                        )

                if plain_raw:
                    return ParsedLyrics(
                        title=title,
                        artist=artist,
                        is_synced=False,
                        synced_lines=[],
                        plain_text=plain_raw,
                    )
    except Exception as exc:
        logger.warning("Error fetching lyrics for %r: %s", query, exc)

    return None
