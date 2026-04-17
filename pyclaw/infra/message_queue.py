"""
Message Queue Manager

Inspired by claude-code design, implements a three-level priority message queue:
- now: Highest priority, process immediately
- next: Normal priority, process in queue order
- later: Lowest priority, process when idle

Features:
- Thread-safe
- Subscription mechanism support
- Async processing
- Persistence support (optional)
"""

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger("message_queue")


class Priority(Enum):
    """Message priority"""
    NOW = 0    # Highest priority, process immediately
    NEXT = 1   # Normal priority, process in queue order
    LATER = 2  # Lowest priority, process when idle


@dataclass
class QueuedMessage:
    """Message object in the message queue."""

    id: str
    content: Any
    priority: Priority
    created_at: float
    metadata: dict = field(default_factory=dict)
    
    def to_dict(self) -> dict:
        """Convert message to dictionary format.

        Returns:
            Message dictionary
        """
        return {
            "id": self.id,
            "content": self.content,
            "priority": self.priority.name,
            "created_at": self.created_at,
            "metadata": self.metadata,
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "QueuedMessage":
        """Create message object from dictionary.

        Args:
            data: Message dictionary

        Returns:
            QueuedMessage object
        """
        return cls(
            id=data["id"],
            content=data["content"],
            priority=Priority[data["priority"]],
            created_at=data["created_at"],
            metadata=data.get("metadata", {}),
        )


class MessageQueue:
    """Message queue manager.

    Implements a three-level priority message queue:
    - now: Highest priority, process immediately
    - next: Normal priority, process in queue order
    - later: Lowest priority, process when idle
    """
    
    def __init__(self, persist_path: Optional[Path] = None):
        """
        Initialize the message queue
        
        Args:
            persist_path: Optional persistence path for checkpoint recovery
        """
        self._queue: list[QueuedMessage] = []
        self._lock = asyncio.Lock()
        self._subscribers: list[Callable[[], None]] = []
        self._persist_path = persist_path
        self._processing_count = 0
        
        # Statistics
        self._stats = {
            "enqueued": 0,
            "dequeued": 0,
            "dropped": 0,
        }
        
        # Load persisted data
        if persist_path and persist_path.exists():
            self._load_from_disk()
    
    @property
    def size(self) -> int:
        """Number of messages in the queue.

        Returns:
            Message count
        """
        return len(self._queue)
    
    @property
    def is_empty(self) -> bool:
        """Check if queue is empty.

        Returns:
            True if queue is empty
        """
        return len(self._queue) == 0
    
    @property
    def processing_count(self) -> int:
        """Number of messages being processed.

        Returns:
            Count of processing messages
        """
        return self._processing_count
    
    def enqueue(
        self,
        content: Any,
        priority: Priority = Priority.NEXT,
        metadata: Optional[dict] = None,
    ) -> str:
        """
        Add a message to the queue
        
        Args:
            content: Message content
            priority: Priority, defaults to NEXT
            metadata: Metadata
            
        Returns:
            Message ID
        """
        msg_id = str(uuid.uuid4())[:8]
        msg = QueuedMessage(
            id=msg_id,
            content=content,
            priority=priority,
            created_at=time.time(),
            metadata=metadata or {},
        )
        
        # Insert by priority (maintain FIFO order within same priority)
        insert_idx = len(self._queue)
        for i, existing in enumerate(self._queue):
            if existing.priority.value > priority.value:
                insert_idx = i
                break
        
        self._queue.insert(insert_idx, msg)
        self._stats["enqueued"] += 1
        
        logger.debug("Message enqueued: %s (priority=%s, size=%s)", msg_id, priority.name, self.size)
        
        # Notify subscribers
        self._notify_subscribers()
        
        # Persist
        self._persist_to_disk()
        
        return msg_id
    
    def dequeue(self, filter_fn: Optional[Callable[[QueuedMessage], bool]] = None) -> Optional[QueuedMessage]:
        """
        Get and remove the highest priority message
        
        Args:
            filter_fn: Optional filter function, only return matching messages
            
        Returns:
            Message object, or None if queue is empty
        """
        if self.is_empty:
            return None
        
        # Find matching message
        for i, msg in enumerate(self._queue):
            if filter_fn is None or filter_fn(msg):
                self._queue.pop(i)
                self._stats["dequeued"] += 1
                self._processing_count += 1
                
                logger.debug("Message dequeued: %s (remaining=%s)", msg.id, self.size)
                
                self._notify_subscribers()
                self._persist_to_disk()
                
                return msg
        
        return None
    
    def peek(self) -> Optional[QueuedMessage]:
        """Peek at the highest priority message without removing it"""
        if self.is_empty:
            return None
        return self._queue[0]
    
    def complete(self, msg_id: str) -> None:
        """Mark message processing as complete"""
        self._processing_count = max(0, self._processing_count - 1)
        logger.debug("Message completed: %s (processing=%s)", msg_id, self._processing_count)
    
    def dequeue_all(self) -> list[QueuedMessage]:
        """Get and remove all messages"""
        messages = self._queue.copy()
        self._queue.clear()
        self._stats["dequeued"] += len(messages)
        self._processing_count += len(messages)
        
        self._notify_subscribers()
        self._persist_to_disk()
        
        return messages
    
    def drop(self, msg_id: str) -> bool:
        """Drop a specific message"""
        for i, msg in enumerate(self._queue):
            if msg.id == msg_id:
                self._queue.pop(i)
                self._stats["dropped"] += 1
                
                logger.debug("Message dropped: %s", msg_id)
                
                self._notify_subscribers()
                self._persist_to_disk()
                
                return True
        return False
    
    def subscribe(self, callback: Callable[[], None]) -> Callable[[], None]:
        """
        Subscribe to queue changes
        
        Args:
            callback: Callback function when queue changes
            
        Returns:
            Unsubscribe function
        """
        self._subscribers.append(callback)
        
        def unsubscribe():
            if callback in self._subscribers:
                self._subscribers.remove(callback)
        
        return unsubscribe
    
    def get_snapshot(self) -> list[dict]:
        """Get queue snapshot (for UI display)"""
        return [msg.to_dict() for msg in self._queue]
    
    def get_stats(self) -> dict:
        """Get statistics"""
        return {
            **self._stats,
            "size": self.size,
            "processing": self._processing_count,
        }
    
    def _notify_subscribers(self) -> None:
        """Notify all subscribers"""
        for callback in self._subscribers:
            try:
                callback()
            except Exception as e:
                logger.warning("Subscriber callback failed: %s", e)
    
    def _persist_to_disk(self) -> None:
        """Persist to disk"""
        if not self._persist_path:
            return
        
        try:
            data = {
                "queue": [msg.to_dict() for msg in self._queue],
                "stats": self._stats,
            }
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            self._persist_path.write_text(json.dumps(data, ensure_ascii=False))
        except Exception as e:
            logger.warning("Persistence failed: %s", e)
    
    def _load_from_disk(self) -> None:
        """Load from disk"""
        if not self._persist_path or not self._persist_path.exists():
            return
        
        try:
            data = json.loads(self._persist_path.read_text())
            self._queue = [QueuedMessage.from_dict(m) for m in data.get("queue", [])]
            self._stats = data.get("stats", self._stats)
            logger.info("Loaded %s messages from disk", len(self._queue))
        except Exception as e:
            logger.warning("Load failed: %s", e)


class AsyncMessageProcessor:
    """Async message processor.

    Retrieves messages from the queue and processes them asynchronously with concurrency control.
    """
    
    def __init__(
        self,
        queue: MessageQueue,
        handler: Callable[[QueuedMessage], Any],
        max_concurrency: int = 10,
    ):
        """
        Initialize the processor
        
        Args:
            queue: Message queue
            handler: Message handler function (async)
            max_concurrency: Maximum concurrency
        """
        self.queue = queue
        self.handler = handler
        self.max_concurrency = max_concurrency
        self._running = False
        self._semaphore: Optional[asyncio.Semaphore] = None
        self._tasks: set[asyncio.Task] = set()
    
    async def start(self) -> None:
        """Start the processor."""
        if self._running:
            return
        
        self._running = True
        self._semaphore = asyncio.Semaphore(self.max_concurrency)
        
        logger.info("Message processor started (max_concurrency=%s)", self.max_concurrency)
        
        # Subscribe to queue changes
        self.queue.subscribe(self._on_queue_update)
        
        # Process existing messages
        await self._process_pending()
    
    async def stop(self) -> None:
        """Stop the processor."""
        self._running = False
        
        # Wait for all tasks to complete
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        
        logger.info("Message processor stopped")
    
    def _on_queue_update(self) -> None:
        """Queue update callback"""
        if self._running:
            asyncio.create_task(self._process_pending())
    
    async def _process_pending(self) -> None:
        """Process pending messages"""
        while self._running and not self.queue.is_empty:
            # Acquire semaphore
            if self._semaphore:
                await self._semaphore.acquire()
            
            # Get message
            msg = self.queue.dequeue()
            if msg is None:
                if self._semaphore:
                    self._semaphore.release()
                break
            
            # Create processing task
            task = asyncio.create_task(self._handle_message(msg))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
    
    async def _handle_message(self, msg: QueuedMessage) -> None:
        """Handle a single message"""
        try:
            if asyncio.iscoroutinefunction(self.handler):
                await self.handler(msg)
            else:
                self.handler(msg)
        except Exception as e:
            logger.error("Failed to process message %s: %s", msg.id, e, exc_info=True)
        finally:
            self.queue.complete(msg.id)
            if self._semaphore:
                self._semaphore.release()


# Global message queue instance
_global_queue: Optional[MessageQueue] = None


def get_message_queue() -> MessageQueue:
    """Get the global message queue instance.

    Returns:
        Message queue instance
    """
    global _global_queue
    if _global_queue is None:
        from pyclaw.config.paths import get_paths as _get_paths
        persist_path = _get_paths().message_queue_file
        _global_queue = MessageQueue(persist_path=persist_path)
    return _global_queue