"""
Hook event definitions and payload models.

Defines all available hook events and their payloads.
"""

from enum import Enum
from typing import Optional, Dict, Any, List
from datetime import datetime
from pydantic import BaseModel, Field


class HookEvent(str, Enum):
    """
    All available hook events.
    
    Events follow the pattern: category:action
    """
    
    # Session lifecycle
    SESSION_START = "session:start"
    SESSION_END = "session:end"
    SESSION_PAUSE = "session:pause"
    SESSION_RESUME = "session:resume"
    
    # Message events
    MESSAGE_RECEIVED = "message:received"
    MESSAGE_SENT = "message:sent"
    MESSAGE_PROCESSING = "message:processing"
    
    # Tool events
    TOOL_CALL = "tool:call"
    TOOL_RESULT = "tool:result"
    TOOL_ERROR = "tool:error"
    
    # Agent events
    AGENT_START = "agent:start"
    AGENT_END = "agent:end"
    AGENT_ITERATION = "agent:iteration"
    
    # Error events
    ERROR = "error"
    ERROR_RECOVERED = "error:recovered"
    
    # Channel events
    CHANNEL_CONNECTED = "channel:connected"
    CHANNEL_DISCONNECTED = "channel:disconnected"
    CHANNEL_MESSAGE = "channel:message"
    
    # Cron events
    CRON_TRIGGERED = "cron:triggered"
    CRON_COMPLETED = "cron:completed"
    CRON_FAILED = "cron:failed"
    
    # Config events
    CONFIG_CHANGED = "config:changed"
    CONFIG_RELOADED = "config:reloaded"
    
    # System events
    STARTUP = "system:startup"
    SHUTDOWN = "system:shutdown"
    HEALTH_CHECK = "system:health_check"
    
    # Memory events
    MEMORY_SAVED = "memory:saved"
    MEMORY_RECALLED = "memory:recalled"
    
    # Approval events
    APPROVAL_REQUESTED = "approval:requested"
    APPROVAL_GRANTED = "approval:granted"
    APPROVAL_DENIED = "approval:denied"
    
    @classmethod
    def get_category(cls, event: "HookEvent") -> str:
        """Get the category part of an event."""
        return event.value.split(":")[0]
    
    @classmethod
    def get_action(cls, event: "HookEvent") -> str:
        """Get the action part of an event."""
        parts = event.value.split(":")
        return parts[1] if len(parts) > 1 else ""


class HookEventPayload(BaseModel):
    """
    Payload for hook events.
    
    Contains all context needed to process the event.
    """
    
    event: HookEvent = Field(..., description="The event type")
    timestamp: datetime = Field(default_factory=datetime.now, description="Event timestamp")
    
    # Context
    session_id: Optional[str] = Field(None, description="Related session ID")
    channel: Optional[str] = Field(None, description="Channel name")
    user_id: Optional[str] = Field(None, description="User identifier")
    
    # Event-specific data
    data: Dict[str, Any] = Field(default_factory=dict, description="Event-specific data")
    
    # Metadata
    source: Optional[str] = Field(None, description="Event source component")
    correlation_id: Optional[str] = Field(None, description="Correlation ID for tracing")
    
    model_config = {"arbitrary_types_allowed": True}
    
    def get(self, key: str, default: Any = None) -> Any:
        """Get a value from event data."""
        return self.data.get(key, default)
    
    def set(self, key: str, value: Any) -> None:
        """Set a value in event data."""
        self.data[key] = value


class HookResult(BaseModel):
    """
    Result from a hook handler.
    
    Handlers can return modifications to be applied.
    """
    
    # Whether to continue processing
    continue_processing: bool = Field(True, description="Continue to next handler")
    
    # Modifications to apply
    modifications: Dict[str, Any] = Field(default_factory=dict, description="Modifications to apply")
    
    # Handler info
    handler_name: Optional[str] = Field(None, description="Name of the handler")
    execution_time_ms: Optional[int] = Field(None, description="Execution time in ms")
    
    # Errors
    error: Optional[str] = Field(None, description="Error message if failed")


# Convenience functions for creating payloads

def create_session_event(
    event: HookEvent,
    session_id: str,
    **kwargs,
) -> HookEventPayload:
    """Create a session-related event payload."""
    return HookEventPayload(
        event=event,
        session_id=session_id,
        data=kwargs,
    )


def create_message_event(
    event: HookEvent,
    session_id: str,
    content: str,
    role: str = "user",
    **kwargs,
) -> HookEventPayload:
    """Create a message-related event payload."""
    return HookEventPayload(
        event=event,
        session_id=session_id,
        data={
            "content": content,
            "role": role,
            **kwargs,
        },
    )


def create_tool_event(
    event: HookEvent,
    session_id: str,
    tool_name: str,
    tool_args: Optional[Dict[str, Any]] = None,
    result: Optional[str] = None,
    error: Optional[str] = None,
) -> HookEventPayload:
    """Create a tool-related event payload."""
    return HookEventPayload(
        event=event,
        session_id=session_id,
        data={
            "tool_name": tool_name,
            "tool_args": tool_args or {},
            "result": result,
            "error": error,
        },
    )


def create_error_event(
    session_id: Optional[str],
    error: Exception,
    context: Optional[Dict[str, Any]] = None,
) -> HookEventPayload:
    """Create an error event payload."""
    return HookEventPayload(
        event=HookEvent.ERROR,
        session_id=session_id,
        data={
            "error_type": type(error).__name__,
            "error_message": str(error),
            "context": context or {},
        },
    )
