"""
Hook handler registration and management.

Provides:
- Handler registration with priorities
- Async handler execution
- Handler filtering and grouping
"""

import asyncio
import logging
from typing import Optional, Dict, Any, List, Callable, Awaitable, Set
from datetime import datetime
from dataclasses import dataclass, field
import time

from pyclaw.hooks.events import HookEvent, HookEventPayload, HookResult

logger = logging.getLogger(__name__)


# Type aliases
HookHandler = Callable[[HookEventPayload], Awaitable[Optional[HookResult]]]


@dataclass
class HandlerRegistration:
    """Registration info for a hook handler."""
    
    handler: HookHandler
    name: str
    priority: int = 0  # Higher priority handlers run first
    events: Set[HookEvent] = field(default_factory=set)
    enabled: bool = True
    description: Optional[str] = None
    source: Optional[str] = None  # e.g., "skill:ppt-maker" or "plugin:debug"
    
    # Statistics
    call_count: int = 0
    total_time_ms: int = 0
    last_called: Optional[datetime] = None
    error_count: int = 0


class HookRegistry:
    """
    Central registry for hook handlers.
    
    Features:
    - Priority-based handler ordering
    - Event filtering
    - Handler statistics
    - Concurrent execution support
    """
    
    def __init__(self):
        """Initialize the hook registry."""
        # All registered handlers
        self._handlers: Dict[str, HandlerRegistration] = {}
        
        # Index by event for fast lookup
        self._event_handlers: Dict[HookEvent, List[str]] = {}
        
        # Lock for modifications
        self._lock = asyncio.Lock()
    
    def register(
        self,
        handler: HookHandler,
        events: List[HookEvent],
        name: Optional[str] = None,
        priority: int = 0,
        description: Optional[str] = None,
        source: Optional[str] = None,
    ) -> str:
        """
        Register a handler for specific events.
        
        Args:
            handler: The async handler function
            events: List of events to handle
            name: Handler name (auto-generated if not provided)
            priority: Priority (higher = runs first)
            description: Handler description
            source: Source identifier
            
        Returns:
            Handler registration ID
        """
        if name is None:
            name = f"handler_{len(self._handlers)}_{handler.__name__}"
        
        registration = HandlerRegistration(
            handler=handler,
            name=name,
            priority=priority,
            events=set(events),
            description=description,
            source=source,
        )
        
        self._handlers[name] = registration
        
        # Add to event index
        for event in events:
            if event not in self._event_handlers:
                self._event_handlers[event] = []
            self._event_handlers[event].append(name)
            # Sort by priority (descending)
            self._event_handlers[event].sort(
                key=lambda n: -self._handlers[n].priority
            )
        
        logger.info("Registered hook handler: %s for %d events", name, len(events))
        return name
    
    def unregister(self, name: str) -> bool:
        """
        Unregister a handler.
        
        Args:
            name: Handler name
            
        Returns:
            True if unregistered
        """
        if name not in self._handlers:
            return False
        
        registration = self._handlers[name]
        
        # Remove from event index
        for event in registration.events:
            if event in self._event_handlers:
                if name in self._event_handlers[event]:
                    self._event_handlers[event].remove(name)
        
        del self._handlers[name]
        logger.info("Unregistered hook handler: %s", name)
        return True
    
    def get_handlers(self, event: HookEvent) -> List[HandlerRegistration]:
        """
        Get all handlers for an event.
        
        Args:
            event: The event
            
        Returns:
            List of handler registrations (sorted by priority)
        """
        handler_names = self._event_handlers.get(event, [])
        return [
            self._handlers[name]
            for name in handler_names
            if name in self._handlers and self._handlers[name].enabled
        ]
    
    def enable(self, name: str) -> bool:
        """Enable a handler."""
        if name in self._handlers:
            self._handlers[name].enabled = True
            return True
        return False
    
    def disable(self, name: str) -> bool:
        """Disable a handler."""
        if name in self._handlers:
            self._handlers[name].enabled = False
            return True
        return False
    
    async def trigger(
        self,
        payload: HookEventPayload,
        stop_on_error: bool = False,
    ) -> List[HookResult]:
        """
        Trigger an event and execute all handlers.
        
        Args:
            payload: The event payload
            stop_on_error: Stop execution if a handler fails
            
        Returns:
            List of handler results
        """
        handlers = self.get_handlers(payload.event)
        if not handlers:
            return []
        
        results: List[HookResult] = []
        
        for registration in handlers:
            start_time = time.time()
            
            try:
                result = await registration.handler(payload)
                
                # Update statistics
                duration_ms = int((time.time() - start_time) * 1000)
                registration.call_count += 1
                registration.total_time_ms += duration_ms
                registration.last_called = datetime.now()
                
                if result:
                    result.handler_name = registration.name
                    result.execution_time_ms = duration_ms
                    results.append(result)
                    
                    # Check if we should stop processing
                    if not result.continue_processing:
                        logger.debug(
                            f"Handler {registration.name} stopped event processing"
                        )
                        break
                
            except Exception as e:
                registration.error_count += 1
                logger.error(
                    f"Hook handler {registration.name} failed: {e}",
                    exc_info=True
                )
                
                if stop_on_error:
                    raise
                
                results.append(HookResult(
                    handler_name=registration.name,
                    error=str(e),
                ))
        
        return results
    
    async def trigger_concurrent(
        self,
        payload: HookEventPayload,
        timeout: float = 30.0,
    ) -> List[HookResult]:
        """
        Trigger an event and execute all handlers concurrently.
        
        Note: Handlers cannot stop processing in concurrent mode.
        
        Args:
            payload: The event payload
            timeout: Maximum time to wait for all handlers
            
        Returns:
            List of handler results
        """
        handlers = self.get_handlers(payload.event)
        if not handlers:
            return []
        
        async def run_handler(reg: HandlerRegistration) -> HookResult:
            start_time = time.time()
            try:
                result = await reg.handler(payload)
                duration_ms = int((time.time() - start_time) * 1000)
                
                reg.call_count += 1
                reg.total_time_ms += duration_ms
                reg.last_called = datetime.now()
                
                if result:
                    result.handler_name = reg.name
                    result.execution_time_ms = duration_ms
                    return result
                
                return HookResult(handler_name=reg.name)
                
            except Exception as e:
                reg.error_count += 1
                return HookResult(handler_name=reg.name, error=str(e))
        
        tasks = [run_handler(h) for h in handlers]
        
        try:
            results = await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=timeout,
            )
            
            # Filter out exceptions and convert to HookResult
            final_results = []
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    final_results.append(HookResult(
                        handler_name=handlers[i].name,
                        error=str(result),
                    ))
                else:
                    final_results.append(result)
            
            return final_results
            
        except asyncio.TimeoutError:
            logger.warning("Hook handlers timed out for event %s", payload.event)
            return [HookResult(error="timeout")]
    
    def get_stats(self) -> Dict[str, Any]:
        """Get registry statistics."""
        total_calls = sum(h.call_count for h in self._handlers.values())
        total_errors = sum(h.error_count for h in self._handlers.values())
        
        return {
            "handler_count": len(self._handlers),
            "event_count": len(self._event_handlers),
            "total_calls": total_calls,
            "total_errors": total_errors,
            "handlers": {
                name: {
                    "events": [e.value for e in reg.events],
                    "enabled": reg.enabled,
                    "priority": reg.priority,
                    "call_count": reg.call_count,
                    "error_count": reg.error_count,
                    "avg_time_ms": (
                        reg.total_time_ms / reg.call_count
                        if reg.call_count > 0 else 0
                    ),
                }
                for name, reg in self._handlers.items()
            },
        }
    
    def list_handlers(
        self,
        event: Optional[HookEvent] = None,
        enabled_only: bool = True,
    ) -> List[Dict[str, Any]]:
        """
        List registered handlers.
        
        Args:
            event: Filter by event
            enabled_only: Only include enabled handlers
            
        Returns:
            List of handler info dicts
        """
        handlers = []
        
        for name, reg in self._handlers.items():
            if enabled_only and not reg.enabled:
                continue
            
            if event and event not in reg.events:
                continue
            
            handlers.append({
                "name": name,
                "events": [e.value for e in reg.events],
                "priority": reg.priority,
                "enabled": reg.enabled,
                "description": reg.description,
                "source": reg.source,
                "call_count": reg.call_count,
            })
        
        return handlers


# Decorator for registering handlers

def hook_handler(
    *events: HookEvent,
    name: Optional[str] = None,
    priority: int = 0,
):
    """
    Decorator to register a function as a hook handler.
    
    Usage:
        @hook_handler(HookEvent.MESSAGE_RECEIVED, HookEvent.TOOL_CALL)
        async def my_handler(payload: HookEventPayload) -> Optional[HookResult]:
            # Handle event
            pass
    """
    def decorator(func: HookHandler) -> HookHandler:
        # Store registration info on the function
        func._hook_events = list(events)
        func._hook_name = name or func.__name__
        func._hook_priority = priority
        return func
    
    return decorator


# Global registry instance
_registry_instance: Optional[HookRegistry] = None


def get_hook_registry() -> HookRegistry:
    """Get the global HookRegistry instance."""
    global _registry_instance
    if _registry_instance is None:
        _registry_instance = HookRegistry()
    return _registry_instance


def register_decorated_handlers(module: Any) -> int:
    """
    Register all decorated handlers from a module.
    
    Args:
        module: Python module to scan
        
    Returns:
        Number of handlers registered
    """
    registry = get_hook_registry()
    count = 0
    
    for attr_name in dir(module):
        attr = getattr(module, attr_name)
        if hasattr(attr, "_hook_events"):
            registry.register(
                handler=attr,
                events=attr._hook_events,
                name=attr._hook_name,
                priority=attr._hook_priority,
            )
            count += 1
    
    return count
