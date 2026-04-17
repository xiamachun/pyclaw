"""Memory directory indexer - indexes md files into the memory store."""
import hashlib
import logging
import os
import uuid
from typing import Any, Dict, List, Optional

from pyclaw.memory.models import MemoryEntry
from pyclaw.constants import DEFAULT_CHUNK_SIZE, DEFAULT_CHUNK_OVERLAP

logger = logging.getLogger(__name__)


def chunk_markdown(
    content: str, max_chars: int = DEFAULT_CHUNK_SIZE, overlap_chars: int = DEFAULT_CHUNK_OVERLAP
) -> List[Dict[str, Any]]:
    """Split markdown content into overlapping chunks.

    Default: ~400 tokens * 4 chars = 1600 chars, overlap ~80 tokens * 4 = 320 chars.
    """
    lines = content.split("\n")
    chunks: List[Dict[str, Any]] = []
    current_chunk: List[str] = []
    current_length = 0
    start_line = 0

    for i, line in enumerate(lines):
        line_len = len(line) + 1  # +1 for newline
        if current_length + line_len > max_chars and current_chunk:
            chunk_text = "\n".join(current_chunk)
            chunk_hash = hashlib.sha256(chunk_text.encode()).hexdigest()[:16]
            chunks.append({
                "text": chunk_text,
                "start_line": start_line,
                "end_line": i - 1,
                "hash": chunk_hash,
            })
            # Overlap: keep last overlap_chars worth of lines
            overlap_text = ""
            overlap_lines: List[str] = []
            for prev_line in reversed(current_chunk):
                if len(overlap_text) + len(prev_line) + 1 > overlap_chars:
                    break
                overlap_lines.insert(0, prev_line)
                overlap_text = "\n".join(overlap_lines)
            current_chunk = overlap_lines
            current_length = len(overlap_text)
            start_line = i - len(overlap_lines)

        current_chunk.append(line)
        current_length += line_len

    if current_chunk:
        chunk_text = "\n".join(current_chunk)
        chunk_hash = hashlib.sha256(chunk_text.encode()).hexdigest()[:16]
        chunks.append({
            "text": chunk_text,
            "start_line": start_line,
            "end_line": len(lines) - 1,
            "hash": chunk_hash,
        })

    return chunks


async def index_memory_directory(
    memory_dir: str,
    memory_store,
    embedding_provider,
    model_name: str = "text-embedding-3-small",
    chunking: Optional[Dict[str, int]] = None,
) -> Dict[str, Any]:
    """Index all .md files in the memory directory.

    Args:
        memory_dir: Path to the directory containing .md files.
        memory_store: MemoryStore instance with add() and get_by_hash() methods.
        embedding_provider: EmbeddingProvider instance with embed() method.
        model_name: Name of the embedding model used (stored in metadata).
        chunking: Optional dict with "tokens" and "overlap" keys.

    Returns:
        Stats dict: {"files_indexed": N, "chunks_added": N, "chunks_skipped": N}
    """
    if not os.path.isdir(memory_dir):
        logger.info("Memory directory does not exist: %s", memory_dir)
        return {"files_indexed": 0, "chunks_added": 0, "chunks_skipped": 0}

    chunk_config = chunking or {"tokens": 400, "overlap": 80}
    max_chars = chunk_config.get("tokens", 400) * 4
    overlap_chars = chunk_config.get("overlap", 80) * 4

    stats = {"files_indexed": 0, "chunks_added": 0, "chunks_skipped": 0}

    for filename in os.listdir(memory_dir):
        if not filename.endswith(".md"):
            continue

        filepath = os.path.join(memory_dir, filename)
        if not os.path.isfile(filepath):
            continue

        try:
            with open(filepath, "r", encoding="utf-8") as file_handle:
                content = file_handle.read()
        except Exception as read_error:
            logger.warning("Failed to read %s: %s", filepath, read_error)
            continue

        if not content.strip():
            continue

        chunks = chunk_markdown(content, max_chars=max_chars, overlap_chars=overlap_chars)
        stats["files_indexed"] += 1

        for chunk in chunks:
            existing = await memory_store.get_by_hash(chunk["hash"], filepath)
            if existing:
                # If the existing entry has no embedding, delete it and re-index
                existing_entry = await memory_store.get(existing["entry_id"])
                if existing_entry and existing_entry.embedding is not None and len(existing_entry.embedding) > 0:
                    stats["chunks_skipped"] += 1
                    continue
                # Remove stale entry (missing embedding) so we can retry
                logger.info("Re-indexing chunk (missing embedding): %s", existing["entry_id"])
                await memory_store.delete(existing["entry_id"])

            try:
                embedding = await embedding_provider.embed(chunk["text"])
                logger.info(
                    "Embedding generated for chunk from %s: type=%s len=%s",
                    filepath,
                    type(embedding).__name__,
                    len(embedding) if embedding else 0,
                )
            except Exception as embed_error:
                logger.warning("Failed to embed chunk from %s: %s", filepath, embed_error)
                continue

            if not embedding:
                logger.warning("Empty embedding returned for chunk from %s, skipping", filepath)
                continue

            entry = MemoryEntry(
                entry_id=str(uuid.uuid4()),
                session_id="memory_index",
                agent_name="system",
                content=chunk["text"],
                embedding=embedding,
                metadata={
                    "source": "memory",
                    "path": filepath,
                    "filename": filename,
                    "start_line": chunk["start_line"],
                    "end_line": chunk["end_line"],
                    "hash": chunk["hash"],
                    "model": model_name,
                },
            )
            await memory_store.add(entry)
            stats["chunks_added"] += 1

    logger.info("Memory indexing complete: %s", stats)
    return stats
