"""
Session transcript persistence using JSONL files.

Mirrors OpenClaw's session transcript design:
  - Each session gets its own .jsonl file
  - Messages are appended in real-time (one JSON object per line)
  - On reconnect, the file is read to restore conversation history
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

from pyclaw.config.paths import get_paths as _get_paths
SESSIONS_DIR = str(_get_paths().sessions_dir)


def _ensure_sessions_dir() -> None:
    os.makedirs(SESSIONS_DIR, exist_ok=True)


def transcript_path(session_id: str) -> str:
    """Return the .jsonl file path for a given session_id."""
    _ensure_sessions_dir()
    return os.path.join(SESSIONS_DIR, f"{session_id}.jsonl")


def append_message(session_id: str, role: str, content: str) -> None:
    """Append a single message to the session's JSONL transcript file.

    Each line written is a JSON object with the shape:
        {"role": "user"|"assistant", "content": "...", "timestamp": "..."}

    Args:
        session_id: Unique session identifier.
        role: "user" or "assistant".
        content: Message text.
    """
    path = transcript_path(session_id)
    record = {
        "role": role,
        "content": content,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    try:
        with open(path, "a", encoding="utf-8") as file_handle:
            file_handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as error:
        logger.warning("Failed to append transcript for session %s: %s", session_id, error)


def read_messages(session_id: str) -> list[dict]:
    """Read all messages from a session's JSONL transcript file.

    Returns a list of dicts with keys "role" and "content", suitable for
    use as conversation history passed to the LLM.

    Args:
        session_id: Unique session identifier.

    Returns:
        List of {"role": ..., "content": ...} dicts, oldest first.
    """
    path = transcript_path(session_id)
    if not os.path.exists(path):
        return []

    messages: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as file_handle:
            for line in file_handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    role = record.get("role", "")
                    content = record.get("content", "")
                    if role in ("user", "assistant") and content:
                        messages.append({"role": role, "content": content})
                except json.JSONDecodeError as decode_error:
                    logger.warning(
                        "Skipping malformed transcript line in %s: %s", path, decode_error
                    )
    except OSError as error:
        logger.warning("Failed to read transcript for session %s: %s", session_id, error)

    return messages


def session_exists(session_id: str) -> bool:
    """Return True if a transcript file exists for the given session_id."""
    return os.path.exists(transcript_path(session_id))


def list_sessions() -> list[str]:
    """Return a list of all session IDs that have transcript files."""
    _ensure_sessions_dir()
    session_ids = []
    for filename in os.listdir(SESSIONS_DIR):
        if filename.endswith(".jsonl"):
            session_ids.append(filename[: -len(".jsonl")])
    return session_ids
