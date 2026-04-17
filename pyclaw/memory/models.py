"""
Data models for the memory system.
"""

from datetime import datetime
from typing import Optional
from uuid import uuid4

from pydantic import BaseModel, Field


class MemoryEntry(BaseModel):
    """A single memory entry stored in the memory system."""
    
    entry_id: str = Field(default_factory=lambda: str(uuid4()))
    session_id: str
    agent_name: str
    content: str
    embedding: Optional[list[float]] = None
    metadata: dict = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    expires_at: Optional[datetime] = None


class MemorySearchResult(BaseModel):
    """Result of a memory search operation."""
    
    entry: MemoryEntry
    score: float
    source: str  # "vector", "bm25", or "hybrid"
