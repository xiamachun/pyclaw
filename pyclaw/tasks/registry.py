"""
Task registry with SQLite persistence.

Provides:
- Task lifecycle management
- Persistent storage
- Event logging
- Query and indexing
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import Optional, Dict, Any, List, Callable, Awaitable
from datetime import datetime, timedelta

import aiosqlite

from pyclaw.tasks.models import (
    Task,
    TaskStatus,
    DeliveryStatus,
    TaskEvent,
    TaskCreate,
    TaskUpdate,
    TaskQuery,
)

logger = logging.getLogger(__name__)


class TaskRegistry:
    """
    Central registry for task management.
    
    Features:
    - SQLite persistence
    - Task lifecycle management
    - Event logging
    - Indexed queries
    """
    
    def __init__(
        self,
        db_path: Optional[Path] = None,
        retention_days: int = 7,
    ):
        """
        Initialize the task registry.
        
        Args:
            db_path: Path to SQLite database (str or Path)
            retention_days: Default retention period
        """
        if db_path is None:
            from pyclaw.config.paths import get_paths as _get_paths
            db_path = _get_paths().tasks_db
        elif isinstance(db_path, str):
            db_path = Path(db_path)
        
        self._db_path = db_path
        self._retention_days = retention_days
        self._db: Optional[aiosqlite.Connection] = None
        
        # In-memory indexes for fast lookup
        self._tasks_by_id: Dict[str, Task] = {}
        self._tasks_by_run: Dict[str, List[str]] = {}  # run_id -> task_ids
        self._tasks_by_owner: Dict[str, List[str]] = {}  # owner_key -> task_ids
        
        # Event listeners
        self._listeners: List[Callable[[str, TaskEvent], Awaitable[None]]] = []
        
        # Lock for concurrent access
        self._lock = asyncio.Lock()
    
    async def initialize(self) -> None:
        """Initialize the database and load existing tasks."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        
        self._db = await aiosqlite.connect(str(self._db_path))
        
        # Create tables
        await self._db.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                run_id TEXT,
                owner_key TEXT,
                related_session_key TEXT,
                name TEXT NOT NULL,
                description TEXT,
                task_type TEXT NOT NULL,
                priority INTEGER DEFAULT 1,
                status TEXT NOT NULL,
                delivery_status TEXT NOT NULL,
                input_data TEXT,
                result TEXT,
                error TEXT,
                progress_percent INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                timeout_seconds INTEGER DEFAULT 300,
                cleanup_after TEXT,
                metadata TEXT,
                tags TEXT
            );
            
            CREATE INDEX IF NOT EXISTS idx_tasks_run_id ON tasks(run_id);
            CREATE INDEX IF NOT EXISTS idx_tasks_owner_key ON tasks(owner_key);
            CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
            CREATE INDEX IF NOT EXISTS idx_tasks_created_at ON tasks(created_at);
            
            CREATE TABLE IF NOT EXISTS task_events (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload TEXT,
                timestamp TEXT NOT NULL,
                FOREIGN KEY (task_id) REFERENCES tasks(id)
            );
            
            CREATE INDEX IF NOT EXISTS idx_task_events_task_id ON task_events(task_id);
        """)
        
        await self._db.commit()
        
        # Load tasks into memory
        await self._load_tasks()
        
        logger.info("TaskRegistry initialized with %s tasks", len(self._tasks_by_id))
    
    async def _load_tasks(self) -> None:
        """Load all active tasks into memory."""
        if not self._db:
            return
        
        # Only load non-terminal tasks and recently completed ones
        cutoff = (datetime.now() - timedelta(hours=24)).isoformat()
        
        async with self._db.execute(
            """
            SELECT * FROM tasks 
            WHERE status NOT IN ('succeeded', 'failed', 'timed_out', 'cancelled')
               OR updated_at > ?
            """,
            (cutoff,)
        ) as cursor:
            async for row in cursor:
                task = self._row_to_task(row)
                self._index_task(task)
    
    def _row_to_task(self, row: tuple) -> Task:
        """Convert a database row to a Task object."""
        cols = [
            "id", "run_id", "owner_key", "related_session_key",
            "name", "description", "task_type", "priority",
            "status", "delivery_status", "input_data", "result",
            "error", "progress_percent", "created_at", "updated_at",
            "started_at", "completed_at", "timeout_seconds",
            "cleanup_after", "metadata", "tags"
        ]
        data = dict(zip(cols, row))
        
        # Parse JSON fields
        data["input_data"] = json.loads(data["input_data"] or "{}")
        data["metadata"] = json.loads(data["metadata"] or "{}")
        data["tags"] = json.loads(data["tags"] or "[]")
        
        # Parse datetime fields
        for field in ["created_at", "updated_at", "started_at", "completed_at", "cleanup_after"]:
            if data[field]:
                data[field] = datetime.fromisoformat(data[field])
        
        # Parse enums
        data["status"] = TaskStatus(data["status"])
        data["delivery_status"] = DeliveryStatus(data["delivery_status"])
        
        return Task(**data)
    
    def _index_task(self, task: Task) -> None:
        """Add a task to in-memory indexes."""
        self._tasks_by_id[task.id] = task
        
        if task.run_id:
            if task.run_id not in self._tasks_by_run:
                self._tasks_by_run[task.run_id] = []
            if task.id not in self._tasks_by_run[task.run_id]:
                self._tasks_by_run[task.run_id].append(task.id)
        
        if task.owner_key:
            if task.owner_key not in self._tasks_by_owner:
                self._tasks_by_owner[task.owner_key] = []
            if task.id not in self._tasks_by_owner[task.owner_key]:
                self._tasks_by_owner[task.owner_key].append(task.id)
    
    def _remove_from_index(self, task: Task) -> None:
        """Remove a task from in-memory indexes."""
        if task.id in self._tasks_by_id:
            del self._tasks_by_id[task.id]
        
        if task.run_id and task.run_id in self._tasks_by_run:
            if task.id in self._tasks_by_run[task.run_id]:
                self._tasks_by_run[task.run_id].remove(task.id)
        
        if task.owner_key and task.owner_key in self._tasks_by_owner:
            if task.id in self._tasks_by_owner[task.owner_key]:
                self._tasks_by_owner[task.owner_key].remove(task.id)
    
    async def create_task(self, create: TaskCreate) -> Task:
        """
        Create a new task.
        
        Args:
            create: Task creation data
            
        Returns:
            The created Task
        """
        async with self._lock:
            task = Task(
                name=create.name,
                description=create.description,
                task_type=create.task_type,
                priority=create.priority,
                run_id=create.run_id,
                owner_key=create.owner_key,
                related_session_key=create.related_session_key,
                input_data=create.input_data,
                timeout_seconds=create.timeout_seconds,
                metadata=create.metadata,
                tags=create.tags,
                cleanup_after=datetime.now() + timedelta(days=create.retention_days),
            )
            
            await self._persist_task(task)
            self._index_task(task)
            
            # Log event
            await self._log_event(task.id, "created", {"name": task.name})
            
            logger.info("Created task %s: %s", task.id, task.name)
            return task
    
    async def _persist_task(self, task: Task) -> None:
        """Persist a task to the database."""
        if not self._db:
            return
        
        await self._db.execute(
            """
            INSERT OR REPLACE INTO tasks 
            (id, run_id, owner_key, related_session_key, name, description,
             task_type, priority, status, delivery_status, input_data, result,
             error, progress_percent, created_at, updated_at, started_at,
             completed_at, timeout_seconds, cleanup_after, metadata, tags)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task.id, task.run_id, task.owner_key, task.related_session_key,
                task.name, task.description, task.task_type.value, task.priority.value,
                task.status.value, task.delivery_status.value,
                json.dumps(task.input_data), task.result, task.error,
                task.progress_percent,
                task.created_at.isoformat(), task.updated_at.isoformat(),
                task.started_at.isoformat() if task.started_at else None,
                task.completed_at.isoformat() if task.completed_at else None,
                task.timeout_seconds,
                task.cleanup_after.isoformat() if task.cleanup_after else None,
                json.dumps(task.metadata), json.dumps(task.tags),
            )
        )
        await self._db.commit()
    
    async def get_task(self, task_id: str) -> Optional[Task]:
        """
        Get a task by ID.
        
        Args:
            task_id: The task ID
            
        Returns:
            The Task or None
        """
        # Check memory first
        if task_id in self._tasks_by_id:
            return self._tasks_by_id[task_id]
        
        # Check database
        if not self._db:
            return None
        
        async with self._db.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                task = self._row_to_task(row)
                self._index_task(task)
                return task
        
        return None
    
    async def update_task(self, task_id: str, update: TaskUpdate) -> Optional[Task]:
        """
        Update a task.
        
        Args:
            task_id: The task ID
            update: Update data
            
        Returns:
            The updated Task or None
        """
        async with self._lock:
            task = await self.get_task(task_id)
            if not task:
                return None
            
            old_status = task.status
            
            # Apply updates
            if update.status is not None:
                task.status = update.status
                
                # Track timing
                if update.status == TaskStatus.RUNNING and not task.started_at:
                    task.started_at = datetime.now()
                elif update.status.is_terminal() and not task.completed_at:
                    task.completed_at = datetime.now()
            
            if update.delivery_status is not None:
                task.delivery_status = update.delivery_status
            
            if update.result is not None:
                task.result = update.result
            
            if update.error is not None:
                task.error = update.error
            
            if update.progress_percent is not None:
                task.progress_percent = update.progress_percent
            
            if update.metadata is not None:
                task.metadata.update(update.metadata)
            
            if update.tags is not None:
                task.tags = update.tags
            
            task.updated_at = datetime.now()
            
            await self._persist_task(task)
            
            # Log status change
            if update.status and update.status != old_status:
                await self._log_event(task_id, "status_changed", {
                    "old_status": old_status.value,
                    "new_status": update.status.value,
                })
            
            return task
    
    async def delete_task(self, task_id: str) -> bool:
        """
        Delete a task.
        
        Args:
            task_id: The task ID
            
        Returns:
            True if deleted
        """
        async with self._lock:
            task = await self.get_task(task_id)
            if not task:
                return False
            
            self._remove_from_index(task)
            
            if self._db:
                await self._db.execute(
                    "DELETE FROM task_events WHERE task_id = ?", (task_id,)
                )
                await self._db.execute(
                    "DELETE FROM tasks WHERE id = ?", (task_id,)
                )
                await self._db.commit()
            
            logger.info("Deleted task %s", task_id)
            return True
    
    async def list_tasks(self, query: Optional[TaskQuery] = None) -> List[Task]:
        """
        List tasks matching the query.
        
        Args:
            query: Query parameters
            
        Returns:
            List of matching tasks
        """
        if query is None:
            query = TaskQuery()
        
        # Use in-memory index for common queries
        if query.run_id and query.run_id in self._tasks_by_run:
            task_ids = self._tasks_by_run[query.run_id]
            tasks = [self._tasks_by_id[tid] for tid in task_ids if tid in self._tasks_by_id]
        elif query.owner_key and query.owner_key in self._tasks_by_owner:
            task_ids = self._tasks_by_owner[query.owner_key]
            tasks = [self._tasks_by_id[tid] for tid in task_ids if tid in self._tasks_by_id]
        else:
            # Fall back to database query
            if not self._db:
                return list(self._tasks_by_id.values())
            
            sql = "SELECT * FROM tasks WHERE 1=1"
            params = []
            
            if query.run_id:
                sql += " AND run_id = ?"
                params.append(query.run_id)
            
            if query.owner_key:
                sql += " AND owner_key = ?"
                params.append(query.owner_key)
            
            if query.status:
                placeholders = ",".join("?" * len(query.status))
                sql += f" AND status IN ({placeholders})"
                params.extend(s.value for s in query.status)
            
            if query.task_type:
                sql += " AND task_type = ?"
                params.append(query.task_type.value)
            
            sql += f" ORDER BY {query.sort_by} {'DESC' if query.sort_desc else 'ASC'}"
            sql += f" LIMIT {query.limit} OFFSET {query.offset}"
            
            tasks = []
            async with self._db.execute(sql, params) as cursor:
                async for row in cursor:
                    tasks.append(self._row_to_task(row))
            
            return tasks
        
        # Apply additional filters
        if query.status:
            tasks = [t for t in tasks if t.status in query.status]
        
        if query.task_type:
            tasks = [t for t in tasks if t.task_type == query.task_type]
        
        # Sort
        reverse = query.sort_desc
        tasks.sort(key=lambda t: getattr(t, query.sort_by), reverse=reverse)
        
        # Paginate
        return tasks[query.offset:query.offset + query.limit]
    
    async def _log_event(
        self,
        task_id: str,
        event_type: str,
        payload: Dict[str, Any],
    ) -> TaskEvent:
        """Log a task event."""
        event = TaskEvent(
            task_id=task_id,
            event_type=event_type,
            payload=payload,
        )
        
        if self._db:
            await self._db.execute(
                """
                INSERT INTO task_events (id, task_id, event_type, payload, timestamp)
                VALUES (?, ?, ?, ?, ?)
                """,
                (event.id, event.task_id, event.event_type,
                 json.dumps(event.payload), event.timestamp.isoformat())
            )
            await self._db.commit()
        
        # Notify listeners
        for listener in self._listeners:
            try:
                await listener(task_id, event)
            except Exception as e:
                logger.error("Event listener error: %s", e, exc_info=True)
        
        return event
    
    async def get_events(
        self,
        task_id: str,
        limit: int = 100,
    ) -> List[TaskEvent]:
        """
        Get events for a task.
        
        Args:
            task_id: The task ID
            limit: Maximum events to return
            
        Returns:
            List of TaskEvent objects
        """
        if not self._db:
            return []
        
        events = []
        async with self._db.execute(
            """
            SELECT id, task_id, event_type, payload, timestamp 
            FROM task_events 
            WHERE task_id = ? 
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (task_id, limit)
        ) as cursor:
            async for row in cursor:
                events.append(TaskEvent(
                    id=row[0],
                    task_id=row[1],
                    event_type=row[2],
                    payload=json.loads(row[3] or "{}"),
                    timestamp=datetime.fromisoformat(row[4]),
                ))
        
        return events
    
    def add_listener(
        self,
        listener: Callable[[str, TaskEvent], Awaitable[None]],
    ) -> None:
        """Add an event listener."""
        self._listeners.append(listener)
    
    def remove_listener(
        self,
        listener: Callable[[str, TaskEvent], Awaitable[None]],
    ) -> None:
        """Remove an event listener."""
        if listener in self._listeners:
            self._listeners.remove(listener)
    
    async def cleanup_expired(self) -> int:
        """
        Clean up expired tasks.
        
        Returns:
            Number of tasks cleaned up
        """
        count = 0
        now = datetime.now()
        
        # Find expired tasks
        expired_ids = [
            task_id for task_id, task in self._tasks_by_id.items()
            if task.should_cleanup()
        ]
        
        for task_id in expired_ids:
            await self.delete_task(task_id)
            count += 1
        
        if count > 0:
            logger.info("Cleaned up %s expired tasks", count)
        
        return count
    
    async def get_stats(self) -> Dict[str, Any]:
        """Get registry statistics."""
        status_counts = {}
        for task in self._tasks_by_id.values():
            status = task.status.value
            status_counts[status] = status_counts.get(status, 0) + 1
        
        return {
            "total_tasks": len(self._tasks_by_id),
            "by_status": status_counts,
            "runs": len(self._tasks_by_run),
            "owners": len(self._tasks_by_owner),
        }
    
    async def close(self) -> None:
        """Close the registry."""
        if self._db:
            await self._db.close()
            self._db = None
        
        self._tasks_by_id.clear()
        self._tasks_by_run.clear()
        self._tasks_by_owner.clear()


# Global singleton
_registry_instance: Optional[TaskRegistry] = None


async def get_task_registry() -> TaskRegistry:
    """Get the global TaskRegistry instance."""
    global _registry_instance
    if _registry_instance is None:
        _registry_instance = TaskRegistry()
        await _registry_instance.initialize()
    return _registry_instance
