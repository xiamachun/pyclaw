"""
Task data models for the task registry.

Defines the task state machine and related data structures.
"""

from enum import Enum
from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta
from pydantic import BaseModel, Field
import uuid


class TaskStatus(Enum):
    """Task execution status."""
    
    QUEUED = "queued"        # Task is waiting to be executed
    RUNNING = "running"      # Task is currently executing
    SUCCEEDED = "succeeded"  # Task completed successfully
    FAILED = "failed"        # Task failed with an error
    TIMED_OUT = "timed_out"  # Task exceeded timeout
    CANCELLED = "cancelled"  # Task was cancelled
    BLOCKED = "blocked"      # Task is blocked waiting for input
    
    def is_terminal(self) -> bool:
        """Check if this is a terminal state."""
        return self in {
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.TIMED_OUT,
            TaskStatus.CANCELLED,
        }
    
    def is_active(self) -> bool:
        """Check if this is an active (running) state."""
        return self in {TaskStatus.RUNNING, TaskStatus.BLOCKED}


class DeliveryStatus(Enum):
    """Task result delivery status."""
    
    PENDING = "pending"              # Waiting to be delivered
    DELIVERED = "delivered"          # Successfully delivered
    FAILED = "failed"                # Delivery failed
    SESSION_QUEUED = "session_queued"  # Queued for session delivery
    PARENT_MISSING = "parent_missing"  # Parent task not found
    NOT_APPLICABLE = "not_applicable"  # No delivery needed


class TaskType(Enum):
    """Type of task."""
    
    AGENT = "agent"          # Agent execution task
    CRON = "cron"            # Scheduled cron task
    SHELL = "shell"          # Shell command task
    HTTP = "http"            # HTTP request task
    PLUGIN = "plugin"        # Plugin execution task
    CUSTOM = "custom"        # Custom task type


class TaskPriority(Enum):
    """Task priority levels."""
    
    LOW = 0
    NORMAL = 1
    HIGH = 2
    CRITICAL = 3


class TaskEvent(BaseModel):
    """An event in the task's lifecycle."""
    
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    task_id: str
    event_type: str  # e.g., "status_changed", "progress", "error", "log"
    payload: Dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=datetime.now)
    
    model_config = {"arbitrary_types_allowed": True}


class Task(BaseModel):
    """A task in the task registry."""
    
    # Identity
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    run_id: Optional[str] = None  # Group related tasks
    owner_key: Optional[str] = None  # Session owner
    related_session_key: Optional[str] = None  # Related session
    
    # Task info
    name: str
    description: Optional[str] = None
    task_type: TaskType = TaskType.AGENT
    priority: TaskPriority = TaskPriority.NORMAL
    
    # Status
    status: TaskStatus = TaskStatus.QUEUED
    delivery_status: DeliveryStatus = DeliveryStatus.PENDING
    
    # Execution details
    input_data: Dict[str, Any] = Field(default_factory=dict)
    result: Optional[str] = None
    error: Optional[str] = None
    progress_percent: int = 0
    
    # Timing
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    timeout_seconds: int = 300  # 5 minutes default
    
    # Retention
    cleanup_after: Optional[datetime] = None  # When to clean up this task
    
    # Metadata
    metadata: Dict[str, Any] = Field(default_factory=dict)
    tags: List[str] = Field(default_factory=list)
    
    model_config = {"arbitrary_types_allowed": True}
    
    def is_overdue(self) -> bool:
        """Check if this task has exceeded its timeout."""
        if not self.started_at or self.status.is_terminal():
            return False
        
        elapsed = (datetime.now() - self.started_at).total_seconds()
        return elapsed > self.timeout_seconds
    
    def get_duration_seconds(self) -> Optional[float]:
        """Get the task execution duration in seconds."""
        if not self.started_at:
            return None
        
        end_time = self.completed_at or datetime.now()
        return (end_time - self.started_at).total_seconds()
    
    def should_cleanup(self) -> bool:
        """Check if this task should be cleaned up."""
        if not self.cleanup_after:
            return False
        return datetime.now() >= self.cleanup_after


class TaskCreate(BaseModel):
    """Data for creating a new task."""
    
    name: str
    description: Optional[str] = None
    task_type: TaskType = TaskType.AGENT
    priority: TaskPriority = TaskPriority.NORMAL
    
    run_id: Optional[str] = None
    owner_key: Optional[str] = None
    related_session_key: Optional[str] = None
    
    input_data: Dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: int = 300
    
    metadata: Dict[str, Any] = Field(default_factory=dict)
    tags: List[str] = Field(default_factory=list)
    
    # Retention: default 7 days
    retention_days: int = 7


class TaskUpdate(BaseModel):
    """Data for updating an existing task."""
    
    status: Optional[TaskStatus] = None
    delivery_status: Optional[DeliveryStatus] = None
    result: Optional[str] = None
    error: Optional[str] = None
    progress_percent: Optional[int] = None
    metadata: Optional[Dict[str, Any]] = None
    tags: Optional[List[str]] = None


class TaskQuery(BaseModel):
    """Query parameters for listing tasks."""
    
    run_id: Optional[str] = None
    owner_key: Optional[str] = None
    related_session_key: Optional[str] = None
    task_type: Optional[TaskType] = None
    status: Optional[List[TaskStatus]] = None
    delivery_status: Optional[List[DeliveryStatus]] = None
    tags: Optional[List[str]] = None
    
    # Pagination
    limit: int = 50
    offset: int = 0
    
    # Sorting
    sort_by: str = "created_at"
    sort_desc: bool = True


class TaskSnapshot(BaseModel):
    """A snapshot of task state for display."""
    
    id: str
    name: str
    status: TaskStatus
    progress_percent: int
    duration_seconds: Optional[float]
    error: Optional[str]
    created_at: datetime
    
    @classmethod
    def from_task(cls, task: Task) -> "TaskSnapshot":
        return cls(
            id=task.id,
            name=task.name,
            status=task.status,
            progress_percent=task.progress_percent,
            duration_seconds=task.get_duration_seconds(),
            error=task.error,
            created_at=task.created_at,
        )
