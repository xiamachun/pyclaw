"""
High-level memory management interface.
"""

from typing import Optional

from .embeddings import EmbeddingProvider
from .models import MemoryEntry, MemorySearchResult
from .store import MemoryStore


class MemoryManager:
    """Manages memory storage, retrieval, and search operations."""
    
    def __init__(
        self,
        store: MemoryStore,
        config: dict,
        embedding_provider: EmbeddingProvider,
    ):
        """Initialize the memory manager."""
        self.store = store
        self.config = config
        self.embedding_provider = embedding_provider
    
    async def remember(
        self,
        session_id: str,
        agent_name: str,
        content: str,
        metadata: Optional[dict] = None,
    ) -> MemoryEntry:
        """Store a new memory entry."""
        from .models import MemoryEntry
        from datetime import datetime, timedelta
        
        # Get embedding for the content
        embedding = await self._get_embedding(content)
        
        # Calculate expiration if configured
        expires_at = None
        ttl_days = self.config.get("ttl_days")
        if ttl_days:
            expires_at = datetime.utcnow() + timedelta(days=ttl_days)
        
        entry = MemoryEntry(
            session_id=session_id,
            agent_name=agent_name,
            content=content,
            embedding=embedding,
            metadata=metadata or {},
            expires_at=expires_at,
        )
        
        await self.store.add(entry)
        return entry
    
    async def recall(
        self,
        query: str,
        session_id: Optional[str] = None,
        agent_name: Optional[str] = None,
        limit: int = 10,
    ) -> list[MemorySearchResult]:
        """Retrieve relevant memories using hybrid search."""
        # Get query embedding
        query_embedding = await self._get_embedding(query)
        
        # Perform both BM25 and vector searches
        bm25_results = await self.store.search_bm25(query, limit * 2)
        vector_results = await self.store.search_vector(query_embedding, limit * 2)
        
        # Filter by session_id and agent_name if provided
        if session_id or agent_name:
            bm25_results = [
                r for r in bm25_results
                if (not session_id or r.entry.session_id == session_id)
                and (not agent_name or r.entry.agent_name == agent_name)
            ]
            vector_results = [
                r for r in vector_results
                if (not session_id or r.entry.session_id == session_id)
                and (not agent_name or r.entry.agent_name == agent_name)
            ]
        
        # Combine results using hybrid search
        bm25_weight = self.config.get("bm25_weight", 0.3)
        vector_weight = self.config.get("vector_weight", 0.7)
        
        return self._hybrid_search(
            bm25_results,
            vector_results,
            bm25_weight,
            vector_weight,
        )[:limit]
    
    async def forget(self, entry_id: str) -> None:
        """Delete a specific memory entry."""
        await self.store.delete(entry_id)
    
    async def cleanup(self) -> int:
        """Remove expired entries and return count of deleted entries."""
        return await self.store.cleanup_expired()
    
    async def _get_embedding(self, text: str) -> list[float]:
        """Get embedding for text using the configured provider."""
        return await self.embedding_provider.embed(text)
    
    def _hybrid_search(
        self,
        bm25_results: list[MemorySearchResult],
        vector_results: list[MemorySearchResult],
        bm25_weight: float,
        vector_weight: float,
    ) -> list[MemorySearchResult]:
        """Combine BM25 and vector search results with weighted scoring."""
        combined = {}
        
        # Normalize and combine BM25 scores (lower is better in SQLite FTS5, so invert)
        if bm25_results:
            min_bm25 = min(r.score for r in bm25_results)
            max_bm25 = max(r.score for r in bm25_results)
            bm25_range = max_bm25 - min_bm25

            for result in bm25_results:
                # When there is only one result (or all scores are equal),
                # use the raw absolute score clamped to [0, 1] instead of
                # normalizing to zero.
                if bm25_range == 0:
                    normalized_score = min(abs(result.score), 1.0)
                else:
                    normalized_score = 1 - ((result.score - min_bm25) / bm25_range)
                combined[result.entry.entry_id] = {
                    "entry": result.entry,
                    "bm25_score": normalized_score,
                    "vector_score": 0.0,
                }

        # Normalize and combine vector scores (cosine similarity, higher is better)
        if vector_results:
            min_vector = min(r.score for r in vector_results)
            max_vector = max(r.score for r in vector_results)
            vector_range = max_vector - min_vector

            for result in vector_results:
                # When there is only one result (or all scores are equal),
                # use the raw cosine similarity directly (already in [0, 1]).
                if vector_range == 0:
                    normalized_score = result.score
                else:
                    normalized_score = (result.score - min_vector) / vector_range
                if result.entry.entry_id in combined:
                    combined[result.entry.entry_id]["vector_score"] = normalized_score
                else:
                    combined[result.entry.entry_id] = {
                        "entry": result.entry,
                        "bm25_score": 0.0,
                        "vector_score": normalized_score,
                    }
        
        # Calculate weighted hybrid scores.
        # When a result only appears in one channel, redistribute the weight
        # so that the missing channel's zero score does not drag down the total.
        has_bm25 = bool(bm25_results)
        has_vector = bool(vector_results)

        hybrid_results = []
        for entry_id, data in combined.items():
            appeared_in_bm25 = data["bm25_score"] > 0 or (has_bm25 and not has_vector)
            appeared_in_vector = data["vector_score"] > 0 or (has_vector and not has_bm25)

            if appeared_in_bm25 and appeared_in_vector:
                effective_bm25_w = bm25_weight
                effective_vector_w = vector_weight
            elif appeared_in_vector:
                effective_bm25_w = 0.0
                effective_vector_w = 1.0
            elif appeared_in_bm25:
                effective_bm25_w = 1.0
                effective_vector_w = 0.0
            else:
                effective_bm25_w = bm25_weight
                effective_vector_w = vector_weight

            hybrid_score = (
                data["bm25_score"] * effective_bm25_w
                + data["vector_score"] * effective_vector_w
            )
            hybrid_results.append(
                MemorySearchResult(
                    entry=data["entry"],
                    score=hybrid_score,
                    source="hybrid",
                )
            )
        
        # Sort by hybrid score (higher is better)
        hybrid_results.sort(key=lambda x: x.score, reverse=True)
        return hybrid_results
