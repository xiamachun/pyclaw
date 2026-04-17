"""
Workspace prompt file loading and injection.

Reads markdown files from the workspace directory and builds
system prompt context for agents.
"""

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

WORKSPACE_FILES = [
    "AGENTS.md",
    "SOUL.md",
    "TOOLS.md",
    "IDENTITY.md",
    "USER.md",
    "HEARTBEAT.md",
    "BOOTSTRAP.md",
    "MEMORY.md",
]

MAX_SINGLE_FILE_BYTES = 2 * 1024 * 1024  # 2 MB
MAX_TOTAL_CHARS = 150_000
HEAD_RATIO = 0.70
TAIL_RATIO = 0.20

from pyclaw.config.paths import get_paths as _get_paths
DEFAULT_WORKSPACE_DIR = str(_get_paths().workspace_dir)

DEFAULT_IDENTITY_CONTENT = """\
# Identity

You are PyClaw, a helpful AI assistant.
Follow the user's instructions carefully and provide accurate, concise responses.
"""


def _truncate_content(content: str, max_bytes: int = MAX_SINGLE_FILE_BYTES) -> str:
    """Truncate content that exceeds max_bytes (head 70% + tail 20%)."""
    encoded = content.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return content

    head_size = int(max_bytes * HEAD_RATIO)
    tail_size = int(max_bytes * TAIL_RATIO)

    head_part = encoded[:head_size].decode("utf-8", errors="replace")
    tail_part = encoded[-tail_size:].decode("utf-8", errors="replace")

    return head_part + "\n\n... [truncated] ...\n\n" + tail_part


def load_workspace_files(workspace_dir: str = DEFAULT_WORKSPACE_DIR) -> list[dict]:
    """Load workspace markdown files from the given directory.

    Returns a list of dicts: [{"name": "SOUL.md", "path": "/full/path", "content": "..."}]
    Files exceeding 2 MB are truncated (head 70% + tail 20%).
    Total content is capped at 150,000 characters.
    """
    workspace_path = Path(workspace_dir).expanduser()
    if not workspace_path.is_dir():
        logger.debug("Workspace directory does not exist: %s", workspace_path)
        return []

    loaded_files: list[dict] = []
    total_chars = 0

    for filename in WORKSPACE_FILES:
        file_path = workspace_path / filename
        if not file_path.is_file():
            continue

        try:
            raw_content = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError as err:
            logger.warning("Failed to read workspace file %s: %s", file_path, err)
            continue

        content = _truncate_content(raw_content)

        remaining_budget = MAX_TOTAL_CHARS - total_chars
        if remaining_budget <= 0:
            logger.info("Total content limit reached, skipping remaining files")
            break

        if len(content) > remaining_budget:
            content = content[:remaining_budget]

        loaded_files.append({
            "name": filename,
            "path": str(file_path),
            "content": content,
        })
        total_chars += len(content)

    logger.debug(
        "Loaded %d workspace files (%d total chars)", len(loaded_files), total_chars
    )
    return loaded_files


def build_workspace_system_prompt(files: list[dict]) -> str:
    """Format loaded workspace files into a system prompt section.

    Returns a string with "## {filename}\\n\\n{content}\\n" for each file,
    wrapped in a "Project Context" header.
    """
    if not files:
        return ""

    sections = ["# Project Context\n"]
    for file_entry in files:
        sections.append(f"## {file_entry['name']}\n\n{file_entry['content']}\n")

    return "\n".join(sections)


def ensure_workspace(workspace_dir: str = DEFAULT_WORKSPACE_DIR) -> None:
    """Ensure the workspace directory structure exists.

    Creates the workspace directory and memory subdirectory.
    If IDENTITY.md does not exist, creates it with default content.
    """
    workspace_path = Path(workspace_dir).expanduser()

    workspace_path.mkdir(parents=True, exist_ok=True)
    (workspace_path / "memory").mkdir(parents=True, exist_ok=True)

    identity_path = workspace_path / "IDENTITY.md"
    if not identity_path.exists():
        identity_path.write_text(DEFAULT_IDENTITY_CONTENT, encoding="utf-8")
        logger.info("Created default IDENTITY.md at %s", identity_path)
