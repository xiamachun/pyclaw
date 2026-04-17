"""
Context caching for LLM conversations.

Caches conversation contexts to:
- Reduce redundant LLM calls
- Speed up response times
- Lower API costs

Implements LRU eviction and TTL-based expiration.
"""

import hashlib
import json
import logging
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from collections import OrderedDict
import re

from pyclaw.constants import (
    CONTEXT_CACHE_MAX_SIZE,
    CONTEXT_CACHE_TTL_SECONDS,
    HISTORY_CONTENT_MAX_CHARS,
)

logger = logging.getLogger(__name__)


@dataclass
class CacheEntry:
    """A cached context entry."""
    
    context: List[Dict[str, Any]]  # The cached messages
    response: Optional[str] = None  # Cached response (if any)
    timestamp: datetime = field(default_factory=datetime.now)
    hits: int = 0  # Number of times this entry was accessed
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def is_expired(self, ttl_seconds: int) -> bool:
        """Check if this entry has expired."""
        return datetime.now() > self.timestamp + timedelta(seconds=ttl_seconds)
    
    def touch(self) -> None:
        """Update access time and hit count."""
        self.hits += 1


class ContextCache:
    """
    LRU cache for conversation contexts.
    
    Features:
    - LRU eviction when max size is reached
    - TTL-based expiration
    - Session-based cache keys
    - Pattern-based invalidation
    """
    
    def __init__(
        self,
        max_size: int = CONTEXT_CACHE_MAX_SIZE,
        ttl_seconds: int = CONTEXT_CACHE_TTL_SECONDS,
    ):
        """
        Initialize the context cache.
        
        Args:
            max_size: Maximum number of entries
            ttl_seconds: Time-to-live in seconds
        """
        self._cache: OrderedDict[str, CacheEntry] = OrderedDict()
        self._max_size = max_size
        self._ttl = ttl_seconds
        
        # Statistics
        self._hits = 0
        self._misses = 0
    
    def _generate_key(
        self,
        session_id: str,
        messages: List[Dict[str, Any]],
    ) -> str:
        """
        Generate a cache key for the given context.
        
        Args:
            session_id: Session identifier
            messages: List of messages
            
        Returns:
            Cache key string
        """
        # Create a stable hash of the messages
        # Only include role and content, ignore timestamps etc.
        normalized = [
            {"role": m.get("role"), "content": m.get("content", "")[:HISTORY_CONTENT_MAX_CHARS]}
            for m in messages
        ]
        content_hash = hashlib.md5(
            json.dumps(normalized, sort_keys=True).encode()
        ).hexdigest()
        
        return f"{session_id}:{content_hash}"
    
    def get(
        self,
        session_id: str,
        messages: List[Dict[str, Any]],
    ) -> Optional[CacheEntry]:
        """
        Get a cached context entry.
        
        Args:
            session_id: Session identifier
            messages: Messages to look up
            
        Returns:
            CacheEntry if found and valid, None otherwise
        """
        key = self._generate_key(session_id, messages)
        
        if key not in self._cache:
            self._misses += 1
            return None
        
        entry = self._cache[key]
        
        # Check expiration
        if entry.is_expired(self._ttl):
            del self._cache[key]
            self._misses += 1
            return None
        
        # Move to end (LRU)
        self._cache.move_to_end(key)
        entry.touch()
        self._hits += 1
        
        logger.debug("Cache hit for session %s", session_id)
        return entry
    
    def put(
        self,
        session_id: str,
        messages: List[Dict[str, Any]],
        response: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Store a context in the cache.
        
        Args:
            session_id: Session identifier
            messages: Messages to cache
            response: Optional response to cache with context
            metadata: Optional metadata
            
        Returns:
            The cache key
        """
        key = self._generate_key(session_id, messages)
        
        # Remove oldest entries if at capacity
        while len(self._cache) >= self._max_size:
            oldest_key = next(iter(self._cache))
            del self._cache[oldest_key]
            logger.debug("Evicted cache entry: %s", oldest_key)
        
        self._cache[key] = CacheEntry(
            context=messages.copy(),
            response=response,
            metadata=metadata or {},
        )
        
        logger.debug("Cached context for session %s", session_id)
        return key
    
    def invalidate(
        self,
        pattern: str,
    ) -> int:
        """
        Invalidate cache entries matching a pattern.
        
        Args:
            pattern: Regex pattern to match against keys
            
        Returns:
            Number of entries invalidated
        """
        compiled = re.compile(pattern)
        keys_to_remove = [
            key for key in self._cache.keys()
            if compiled.search(key)
        ]
        
        for key in keys_to_remove:
            del self._cache[key]
        
        if keys_to_remove:
            logger.info("Invalidated %d cache entries", len(keys_to_remove))
        
        return len(keys_to_remove)
    
    def invalidate_session(self, session_id: str) -> int:
        """
        Invalidate all cache entries for a session.
        
        Args:
            session_id: Session identifier
            
        Returns:
            Number of entries invalidated
        """
        return self.invalidate(f"^{re.escape(session_id)}:")
    
    def clear(self) -> int:
        """
        Clear all cache entries.
        
        Returns:
            Number of entries cleared
        """
        count = len(self._cache)
        self._cache.clear()
        self._hits = 0
        self._misses = 0
        logger.info("Cleared %d cache entries", count)
        return count
    
    def cleanup_expired(self) -> int:
        """
        Remove all expired entries.
        
        Returns:
            Number of entries removed
        """
        keys_to_remove = [
            key for key, entry in self._cache.items()
            if entry.is_expired(self._ttl)
        ]
        
        for key in keys_to_remove:
            del self._cache[key]
        
        return len(keys_to_remove)
    
    def get_stats(self) -> Dict[str, Any]:
        """
        Get cache statistics.
        
        Returns:
            Dict with statistics
        """
        total_requests = self._hits + self._misses
        hit_rate = (self._hits / total_requests * 100) if total_requests > 0 else 0
        
        # Count expired entries
        expired = sum(
            1 for entry in self._cache.values()
            if entry.is_expired(self._ttl)
        )
        
        return {
            "size": len(self._cache),
            "max_size": self._max_size,
            "ttl_seconds": self._ttl,
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate_percent": round(hit_rate, 2),
            "expired_entries": expired,
            "valid_entries": len(self._cache) - expired,
        }
    
    def get_session_entries(self, session_id: str) -> List[Dict[str, Any]]:
        """
        Get all cache entries for a session.
        
        Args:
            session_id: Session identifier
            
        Returns:
            List of entry info dicts
        """
        prefix = f"{session_id}:"
        entries = []
        
        for key, entry in self._cache.items():
            if key.startswith(prefix):
                entries.append({
                    "key": key,
                    "message_count": len(entry.context),
                    "has_response": entry.response is not None,
                    "timestamp": entry.timestamp.isoformat(),
                    "hits": entry.hits,
                    "is_expired": entry.is_expired(self._ttl),
                })
        
        return entries


# Global singleton instance
_cache_instance: Optional[ContextCache] = None


def get_context_cache(
    max_size: int = CONTEXT_CACHE_MAX_SIZE,
    ttl_seconds: int = CONTEXT_CACHE_TTL_SECONDS,
) -> ContextCache:
    """
    Get the global ContextCache instance.
    
    Args:
        max_size: Maximum cache size (only used on first call)
        ttl_seconds: TTL in seconds (only used on first call)
        
    Returns:
        ContextCache instance
    """
    global _cache_instance
    if _cache_instance is None:
        _cache_instance = ContextCache(
            max_size=max_size,
            ttl_seconds=ttl_seconds,
        )
    return _cache_instance
