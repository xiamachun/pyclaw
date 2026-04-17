"""Session data models.

Enhanced features:
- Session queue mode
- Cost tracking
- Session isolation
"""

import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Optional, List, Dict

from pydantic import BaseModel, Field


class SessionStatus(str, Enum):
    """Session status enumeration."""
    ACTIVE = "active"
    PAUSED = "paused"
    CLOSED = "closed"
    QUEUED = "queued"  # Waiting for processing


class SessionType(str, Enum):
    """Session type enumeration."""
    MAIN = "main"          # Main session, full permissions
    ISOLATED = "isolated"  # Isolated session, restricted tools
    GROUP = "group"        # Group session
    CRON = "cron"          # Scheduled task session
    TEMP = "temp"          # Temporary session


class SessionMode(str, Enum):
    """Session processing mode."""
    DIRECT = "direct"      # Direct response
    QUEUE = "queue"        # Queue mode
    FIFO = "fifo"          # First-in-first-out


class TranscriptEntry(BaseModel):
    """Transcript entry."""
    role: str = Field(..., description="Role: user/assistant/system/tool")
    content: str = Field(..., description="Message content")
    timestamp: datetime = Field(default_factory=datetime.utcnow, description="Timestamp")
    tool_name: Optional[str] = Field(None, description="Tool name (when role=tool)")
    tool_result: Optional[str] = Field(None, description="Tool execution result")


class SessionCost(BaseModel):
    """Session cost tracking."""
    session_id: str = Field(..., description="Session ID")
    message_count: int = Field(default=0, description="Number of messages")
    input_tokens: int = Field(default=0, description="Input token count")
    output_tokens: int = Field(default=0, description="Output token count")
    total_tokens: int = Field(default=0, description="Total token count")
    total_cost_usd: float = Field(default=0.0, description="Total cost (USD)")
    model: Optional[str] = Field(None, description="Model used")
    last_updated: datetime = Field(default_factory=datetime.utcnow, description="Last update time")
    
    def add_usage(
        self,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float = 0.0,
    ) -> None:
        """Add usage statistics."""
        self.message_count += 1
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.total_tokens = self.input_tokens + self.output_tokens
        self.total_cost_usd += cost_usd
        self.last_updated = datetime.utcnow()


class Session(BaseModel):
    """Session model."""
    session_id: str = Field(default_factory=lambda: str(uuid.uuid4()), description="Session ID")
    agent_name: str = Field(..., description="Agent name")
    session_type: SessionType = Field(..., description="Session type")
    session_mode: SessionMode = Field(default=SessionMode.DIRECT, description="Processing mode")
    status: SessionStatus = Field(default=SessionStatus.ACTIVE, description="Session status")
    created_at: datetime = Field(default_factory=datetime.utcnow, description="Creation time")
    updated_at: datetime = Field(default_factory=datetime.utcnow, description="Update time")
    channel: Optional[str] = Field(None, description="Associated channel")
    peer: Optional[str] = Field(None, description="Peer identifier")
    transcript: list[TranscriptEntry] = Field(default_factory=list, description="Transcript")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Metadata")
    
    # Cost tracking
    cost: Optional[SessionCost] = Field(None, description="Cost statistics")
    
    # Queue related
    queue_position: Optional[int] = Field(None, description="Queue position")
    priority: int = Field(default=1, description="Priority (1=normal, 2=high, 3=urgent)")

    @property
    def is_main(self) -> bool:
        """Check if this is a main session."""
        return self.session_type == SessionType.MAIN
    
    @property
    def is_isolated(self) -> bool:
        """Check if this is an isolated session."""
        return self.session_type == SessionType.ISOLATED
    
    def get_cost(self) -> SessionCost:
        """Get or initialize cost tracking."""
        if self.cost is None:
            self.cost = SessionCost(session_id=self.session_id)
        return self.cost
