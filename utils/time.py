"""
utils/time.py – Utilities for formatting durations and timestamps.
"""

from __future__ import annotations


def ms_to_str(milliseconds: int) -> str:
    """Convert milliseconds into a human-readable duration string.

    Examples
    --------
    >>> ms_to_str(65_000)
    '1:05'
    >>> ms_to_str(3_661_000)
    '1:01:01'
    """
    if milliseconds <= 0:
        return "0:00"

    total_seconds = milliseconds // 1000
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def progress_bar(position_ms: int, length_ms: int, width: int = 20) -> str:
    """Return a Unicode block progress bar.

    Parameters
    ----------
    position_ms:
        Current playback position in milliseconds.
    length_ms:
        Total track length in milliseconds.
    width:
        Number of characters in the bar.
    """
    if length_ms <= 0:
        ratio = 0.0
    else:
        ratio = max(0.0, min(1.0, position_ms / length_ms))

    filled = round(ratio * width)
    bar = "█" * filled + "░" * (width - filled)
    return f"[{bar}]"
