"""Hook manager for event-driven extensions."""

import logging
from collections import defaultdict
from enum import Enum
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)


class HookEvent(str, Enum):
    """Events that can be hooked into."""
    
    BEFORE_MESSAGE = "before_message"
    AFTER_MESSAGE = "after_message"
    BEFORE_TOOL = "before_tool"
    AFTER_TOOL = "after_tool"
    SESSION_CREATED = "session_created"
    SESSION_CLOSED = "session_closed"
    CHANNEL_CONNECTED = "channel_connected"
    CHANNEL_DISCONNECTED = "channel_disconnected"
    CONFIG_CHANGED = "config_changed"
    ERROR = "error"


HookHandler = Callable[[HookEvent, dict], Awaitable[None]]


class HookManager:
    """Manager for registering and triggering hooks."""
    
    def __init__(self):
        """Initialize hook manager."""
        self._handlers: dict[HookEvent, list[HookHandler]] = defaultdict(list)
        
        logger.info("HookManager initialized")
    
    def register(self, event: HookEvent, handler: HookHandler) -> None:
        """Register a handler for a specific event.
        
        Args:
            event: The event to hook into.
            handler: The async handler function.
        """
        self._handlers[event].append(handler)
        logger.debug("Registered handler for event: %s", event)
    
    def unregister(self, event: HookEvent, handler: HookHandler) -> None:
        """Unregister a handler for a specific event.
        
        Args:
            event: The event to unhook from.
            handler: The handler function to remove.
        """
        if event in self._handlers:
            try:
                self._handlers[event].remove(handler)
                logger.debug("Unregistered handler for event: %s", event)
            except ValueError:
                logger.warning("Handler not found for event: %s", event)
    
    async def emit(self, event: HookEvent, **kwargs) -> None:
        """Emit an event and trigger all registered handlers.
        
        Args:
            event: The event to emit.
            **kwargs: Event data to pass to handlers.
            
        Raises:
            Exception: If any handler raises an exception.
        """
        if event not in self._handlers:
            return
        
        logger.debug("Emitting event: %s with %d handlers", event, len(self._handlers[event]))
        
        for handler in self._handlers[event]:
            try:
                await handler(event, kwargs)
            except Exception as e:
                logger.error("Handler failed for event %s: %s", event, e, exc_info=True)
                raise
    
    async def emit_safe(self, event: HookEvent, **kwargs) -> None:
        """Emit an event safely, catching and logging exceptions.
        
        Args:
            event: The event to emit.
            **kwargs: Event data to pass to handlers.
        """
        if event not in self._handlers:
            return
        
        logger.debug("Emitting event safely: %s with %d handlers", event, len(self._handlers[event]))
        
        for handler in self._handlers[event]:
            try:
                await handler(event, kwargs)
            except Exception as e:
                logger.error("Handler failed for event %s: %s", event, e, exc_info=True)
                # Continue with other handlers even if one fails
