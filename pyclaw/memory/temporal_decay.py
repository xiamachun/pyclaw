"""Temporal decay for memory search results."""
import math
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

DATED_PATH_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})\.md$")


def extract_timestamp(filepath: str) -> Optional[datetime]:
    """Extract timestamp from file path or file modification time.

    Tries dated filename first (e.g., 2024-01-15.md), falls back to mtime.
    """
    match = DATED_PATH_RE.search(filepath)
    if match:
        year = int(match.group(1))
        month = int(match.group(2))
        day = int(match.group(3))
        try:
            return datetime(year, month, day, tzinfo=timezone.utc)
        except ValueError:
            pass

    # Fall back to file modification time
    if os.path.exists(filepath):
        mtime = os.path.getmtime(filepath)
        return datetime.fromtimestamp(mtime, tz=timezone.utc)

    return None


def apply_temporal_decay(
    results: List[Dict[str, Any]],
    half_life_days: int = 30,
    score_key: str = "score",
    path_key: str = "path",
) -> List[Dict[str, Any]]:
    """Apply temporal decay to search results.

    Formula: score *= exp(-ln(2) / half_life_days * age_in_days)

    Args:
        results: List of search result dicts.
        half_life_days: Number of days for score to decay by half.
        score_key: Key for the score value in result dicts.
        path_key: Key for the file path in result dicts.

    Returns:
        Results with decayed scores, re-sorted by new score descending.
    """
    if not results:
        return results

    now = datetime.now(tz=timezone.utc)
    lambda_decay = math.log(2) / half_life_days

    decayed: List[Dict[str, Any]] = []
    for item in results:
        filepath = item.get(path_key, "")
        # Try to get path from metadata
        if not filepath and "metadata" in item:
            filepath = item["metadata"].get("path", "")

        timestamp = extract_timestamp(filepath)
        if timestamp:
            age_days = (now - timestamp).total_seconds() / 86400
            decay_multiplier = math.exp(-lambda_decay * max(0, age_days))
            new_item = dict(item)
            new_item[score_key] = item.get(score_key, 0.0) * decay_multiplier
            decayed.append(new_item)
        else:
            decayed.append(item)

    # Re-sort by decayed score
    decayed.sort(key=lambda x: x.get(score_key, 0.0), reverse=True)
    return decayed
