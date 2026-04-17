"""
SQLite-based storage for memory entries with BM25 and vector search support.
"""

import aiosqlite
import json
import pickle
import re
from datetime import datetime
from typing import Optional


def build_fts_query(raw: str) -> str | None:
    """Build a safe FTS5 MATCH query from raw user input.

    Extracts Unicode letter/digit/underscore tokens, wraps each in double
    quotes, and joins them with AND.  Returns ``None`` when no valid tokens
    are found so the caller can skip the BM25 search gracefully.

    This mirrors OpenClaw's ``buildFtsQuery`` implementation.
    """
    tokens = re.findall(r"[\w]+", raw, flags=re.UNICODE)
    tokens = [t.strip() for t in tokens if t.strip()]
    if not tokens:
        return None
    quoted = [f'"{t.replace(chr(34), "")}"' for t in tokens]
    return " AND ".join(quoted)

from .models import MemoryEntry, MemorySearchResult


class MemoryStore:
    """SQLite-based storage for memory entries with full-text and vector search."""
    
    def __init__(self, db_path: str):
        """Initialize the memory store with a database path."""
        self.db_path = db_path
        self._connection: Optional[aiosqlite.Connection] = None
    
    async def initialize(self) -> None:
        """Create database tables and indexes."""
        self._connection = await aiosqlite.connect(self.db_path)
        
        # Create main entries table
        await self._connection.execute("""
            CREATE TABLE IF NOT EXISTS entries (
                entry_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                agent_name TEXT NOT NULL,
                content TEXT NOT NULL,
                embedding BLOB,
                metadata TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT
            )
        """)
        
        # Create FTS5 virtual table for BM25 search
        await self._connection.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(
                entry_id,
                content,
                content='entries',
                content_rowid='rowid'
            )
        """)
        
        # Create indexes for common queries
        await self._connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_session_id 
            ON entries(session_id)
        """)
        await self._connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_agent_name 
            ON entries(agent_name)
        """)
        await self._connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_expires_at 
            ON entries(expires_at)
        """)
        
        await self._connection.commit()
    
    async def add(self, entry: MemoryEntry) -> None:
        """Add a memory entry to storage."""
        embedding_blob = None
        if entry.embedding is not None and len(entry.embedding) > 0:
            embedding_blob = pickle.dumps(entry.embedding)
        
        metadata_json = json.dumps(entry.metadata)
        
        await self._connection.execute(
            """
            INSERT INTO entries 
            (entry_id, session_id, agent_name, content, embedding, metadata, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry.entry_id,
                entry.session_id,
                entry.agent_name,
                entry.content,
                embedding_blob,
                metadata_json,
                entry.created_at.isoformat(),
                entry.expires_at.isoformat() if entry.expires_at else None,
            ),
        )
        
        # Add to FTS table
        await self._connection.execute(
            """
            INSERT INTO entries_fts (entry_id, content)
            VALUES (?, ?)
            """,
            (entry.entry_id, entry.content),
        )
        
        await self._connection.commit()
    
    async def get(self, entry_id: str) -> Optional[MemoryEntry]:
        """Retrieve a memory entry by ID."""
        cursor = await self._connection.execute(
            """
            SELECT entry_id, session_id, agent_name, content, embedding, 
                   metadata, created_at, expires_at
            FROM entries WHERE entry_id = ?
            """,
            (entry_id,),
        )
        row = await cursor.fetchone()
        
        if not row:
            return None
        
        embedding = None
        if row[4]:
            embedding = pickle.loads(row[4])
        
        return MemoryEntry(
            entry_id=row[0],
            session_id=row[1],
            agent_name=row[2],
            content=row[3],
            embedding=embedding,
            metadata=json.loads(row[5]),
            created_at=datetime.fromisoformat(row[6]),
            expires_at=datetime.fromisoformat(row[7]) if row[7] else None,
        )
    
    async def delete(self, entry_id: str) -> None:
        """Delete a memory entry by ID."""
        await self._connection.execute(
            "DELETE FROM entries WHERE entry_id = ?",
            (entry_id,),
        )
        await self._connection.execute(
            "DELETE FROM entries_fts WHERE entry_id = ?",
            (entry_id,),
        )
        await self._connection.commit()
    
    async def search_bm25(self, query: str, limit: int = 10) -> list[MemorySearchResult]:
        """Search using BM25 full-text search."""
        fts_query = build_fts_query(query)
        if fts_query is None:
            return []

        cursor = await self._connection.execute(
            """
            SELECT e.entry_id, e.session_id, e.agent_name, e.content, e.embedding,
                   e.metadata, e.created_at, e.expires_at, bm25(entries_fts) as score
            FROM entries e
            JOIN entries_fts fts ON e.entry_id = fts.entry_id
            WHERE entries_fts MATCH ?
            ORDER BY score
            LIMIT ?
            """,
            (fts_query, limit),
        )
        
        results = []
        async for row in cursor:
            embedding = None
            if row[4]:
                embedding = pickle.loads(row[4])
            
            entry = MemoryEntry(
                entry_id=row[0],
                session_id=row[1],
                agent_name=row[2],
                content=row[3],
                embedding=embedding,
                metadata=json.loads(row[5]),
                created_at=datetime.fromisoformat(row[6]),
                expires_at=datetime.fromisoformat(row[7]) if row[7] else None,
            )
            
            results.append(
                MemorySearchResult(entry=entry, score=row[8], source="bm25")
            )
        
        return results
    
    async def search_vector(self, embedding: list[float], limit: int = 10) -> list[MemorySearchResult]:
        """Search using cosine similarity on embeddings."""
        cursor = await self._connection.execute(
            """
            SELECT entry_id, session_id, agent_name, content, embedding,
                   metadata, created_at, expires_at
            FROM entries
            WHERE embedding IS NOT NULL
            """
        )
        
        results = []
        async for row in cursor:
            stored_embedding = pickle.loads(row[4])
            score = self._cosine_similarity(embedding, stored_embedding)
            
            entry = MemoryEntry(
                entry_id=row[0],
                session_id=row[1],
                agent_name=row[2],
                content=row[3],
                embedding=stored_embedding,
                metadata=json.loads(row[5]),
                created_at=datetime.fromisoformat(row[6]),
                expires_at=datetime.fromisoformat(row[7]) if row[7] else None,
            )
            
            results.append(
                MemorySearchResult(entry=entry, score=score, source="vector")
            )
        
        # Sort by score (higher is better) and return top results
        results.sort(key=lambda x: x.score, reverse=True)
        return results[:limit]
    
    def _cosine_similarity(self, vec1: list[float], vec2: list[float]) -> float:
        """Calculate cosine similarity between two vectors."""
        import math
        
        dot_product = sum(a * b for a, b in zip(vec1, vec2))
        magnitude1 = math.sqrt(sum(a * a for a in vec1))
        magnitude2 = math.sqrt(sum(b * b for b in vec2))
        
        if magnitude1 == 0 or magnitude2 == 0:
            return 0.0
        
        return dot_product / (magnitude1 * magnitude2)
    
    async def cleanup_expired(self) -> int:
        """Remove expired entries and return count of deleted entries."""
        from datetime import datetime
        
        now = datetime.utcnow().isoformat()
        cursor = await self._connection.execute(
            """
            DELETE FROM entries
            WHERE expires_at IS NOT NULL AND expires_at < ?
            """,
            (now,),
        )
        
        deleted_count = cursor.rowcount
        
        # Also cleanup FTS table
        await self._connection.execute(
            """
            DELETE FROM entries_fts
            WHERE entry_id NOT IN (SELECT entry_id FROM entries)
            """
        )
        
        await self._connection.commit()
        return deleted_count
    
    async def get_by_hash(self, chunk_hash: str, filepath: str) -> Optional[dict]:
        """Check if a chunk with the given hash already exists for the given file.

        Searches the metadata JSON column for matching hash and path values.
        """
        cursor = await self._connection.execute(
            """
            SELECT entry_id, metadata
            FROM entries
            WHERE metadata LIKE ? AND metadata LIKE ?
            LIMIT 1
            """,
            (
                f'%"hash": "{chunk_hash}"%',
                f'%"path": "{filepath}"%',
            ),
        )
        row = await cursor.fetchone()
        if row:
            return {"entry_id": row[0], "metadata": json.loads(row[1])}
        return None

    async def count(self) -> int:
        """Return total number of entries in storage."""
        cursor = await self._connection.execute("SELECT COUNT(*) FROM entries")
        row = await cursor.fetchone()
        return row[0] if row else 0
    
    async def close(self) -> None:
        """Close the database connection."""
        if self._connection:
            await self._connection.close()
            self._connection = None
