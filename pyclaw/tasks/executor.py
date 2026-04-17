"""
Task executor for running tasks.

Manages task execution lifecycle with:
- Concurrent execution control
- Timeout handling
- Progress tracking
- Result delivery
"""

import asyncio
import logging
from typing import Optional, Dict, Any, Callable, Awaitable, List
from datetime import datetime
from dataclasses import dataclass

from pyclaw.tasks.models import (
    Task,
    TaskStatus,
    DeliveryStatus,
    TaskUpdate,
    TaskType,
)
from pyclaw.tasks.registry import TaskRegistry
from pyclaw.constants import DEFAULT_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)


@dataclass
class ExecutionResult:
    """Result of task execution."""
    success: bool
    result: Optional[str] = None
    error: Optional[str] = None
    duration_seconds: float = 0


# Task handler type
TaskHandler = Callable[[Task, Dict[str, Any]], Awaitable[ExecutionResult]]


class TaskExecutor:
    """
    Executes tasks with lifecycle management.
    
    Features:
    - Concurrent execution with limits
    - Timeout handling
    - Automatic retry for transient failures
    - Progress tracking
    """
    
    def __init__(
        self,
        registry: TaskRegistry,
        max_concurrent: int = 10,
        default_timeout: int = DEFAULT_TIMEOUT_SECONDS,
    ):
        """
        Initialize the executor.
        
        Args:
            registry: Task registry
            max_concurrent: Maximum concurrent tasks
            default_timeout: Default timeout in seconds
        """
        self.registry = registry
        self.max_concurrent = max_concurrent
        self.default_timeout = default_timeout
        
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._handlers: Dict[TaskType, TaskHandler] = {}
        self._running_tasks: Dict[str, asyncio.Task] = {}
        
        # Shutdown flag
        self._shutdown = False
    
    def register_handler(
        self,
        task_type: TaskType,
        handler: TaskHandler,
    ) -> None:
        """
        Register a handler for a task type.
        
        Args:
            task_type: Type of task
            handler: Handler function
        """
        self._handlers[task_type] = handler
        logger.info("Registered handler for task type: %s", task_type.value)
    
    def unregister_handler(self, task_type: TaskType) -> None:
        """Unregister a handler."""
        if task_type in self._handlers:
            del self._handlers[task_type]
    
    async def execute(
        self,
        task_id: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> Optional[ExecutionResult]:
        """
        Execute a task.
        
        Args:
            task_id: ID of task to execute
            context: Execution context
            
        Returns:
            ExecutionResult or None if task not found
        """
        task = await self.registry.get_task(task_id)
        if not task:
            logger.warning("Task not found: %s", task_id)
            return None
        
        # Check if already running
        if task_id in self._running_tasks:
            logger.warning("Task already running: %s", task_id)
            return None
        
        # Get handler
        handler = self._handlers.get(task.task_type)
        if not handler:
            error = f"No handler for task type: {task.task_type.value}"
            await self.registry.update_task(task_id, TaskUpdate(
                status=TaskStatus.FAILED,
                error=error,
            ))
            return ExecutionResult(success=False, error=error)
        
        # Mark as running
        await self.registry.update_task(task_id, TaskUpdate(
            status=TaskStatus.RUNNING,
        ))
        
        # Execute with timeout
        start_time = datetime.now()
        context = context or {}
        
        try:
            async with self._semaphore:
                # Create execution task
                exec_task = asyncio.create_task(
                    handler(task, context)
                )
                self._running_tasks[task_id] = exec_task
                
                try:
                    timeout = task.timeout_seconds or self.default_timeout
                    result = await asyncio.wait_for(exec_task, timeout=timeout)
                except asyncio.TimeoutError:
                    result = ExecutionResult(
                        success=False,
                        error=f"Task timed out after {timeout}s",
                    )
                finally:
                    if task_id in self._running_tasks:
                        del self._running_tasks[task_id]
            
            duration = (datetime.now() - start_time).total_seconds()
            result.duration_seconds = duration
            
            # Update task status
            if result.success:
                await self.registry.update_task(task_id, TaskUpdate(
                    status=TaskStatus.SUCCEEDED,
                    result=result.result,
                    progress_percent=100,
                ))
            else:
                # Check if timeout
                if "timed out" in (result.error or "").lower():
                    status = TaskStatus.TIMED_OUT
                else:
                    status = TaskStatus.FAILED
                
                await self.registry.update_task(task_id, TaskUpdate(
                    status=status,
                    error=result.error,
                ))
            
            return result
            
        except Exception as e:
            duration = (datetime.now() - start_time).total_seconds()
            error = f"{type(e).__name__}: {str(e)}"
            
            await self.registry.update_task(task_id, TaskUpdate(
                status=TaskStatus.FAILED,
                error=error,
            ))
            
            logger.error("Task %s failed: %s", task_id, error, exc_info=True)
            return ExecutionResult(
                success=False,
                error=error,
                duration_seconds=duration,
            )
    
    async def cancel(self, task_id: str) -> bool:
        """
        Cancel a running task.
        
        Args:
            task_id: ID of task to cancel
            
        Returns:
            True if cancelled
        """
        # Cancel running task
        if task_id in self._running_tasks:
            self._running_tasks[task_id].cancel()
            del self._running_tasks[task_id]
        
        # Update status
        task = await self.registry.get_task(task_id)
        if task and not task.status.is_terminal():
            await self.registry.update_task(task_id, TaskUpdate(
                status=TaskStatus.CANCELLED,
            ))
            logger.info("Cancelled task: %s", task_id)
            return True
        
        return False
    
    async def update_progress(
        self,
        task_id: str,
        progress_percent: int,
        message: Optional[str] = None,
    ) -> None:
        """
        Update task progress.
        
        Args:
            task_id: Task ID
            progress_percent: Progress percentage (0-100)
            message: Optional progress message
        """
        metadata = {"progress_message": message} if message else None
        await self.registry.update_task(task_id, TaskUpdate(
            progress_percent=min(100, max(0, progress_percent)),
            metadata=metadata,
        ))
    
    def get_running_tasks(self) -> List[str]:
        """Get list of currently running task IDs."""
        return list(self._running_tasks.keys())
    
    async def shutdown(self, timeout: float = 30.0) -> None:
        """
        Gracefully shutdown the executor.
        
        Args:
            timeout: Maximum time to wait for tasks to complete
        """
        self._shutdown = True
        
        if not self._running_tasks:
            return
        
        logger.info("Shutting down executor with %s running tasks", len(self._running_tasks))
        
        # Wait for tasks to complete or timeout
        tasks = list(self._running_tasks.values())
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            # Cancel remaining tasks
            for task in tasks:
                task.cancel()
            
            logger.warning("Force-cancelled %s tasks on shutdown", len(tasks))


# Built-in task handlers


async def shell_task_handler(
    task: Task,
    context: Dict[str, Any],
) -> ExecutionResult:
    """Handler for shell command tasks."""
    import subprocess
    
    command = task.input_data.get("command", "")
    timeout = task.timeout_seconds or 300
    
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        
        output = f"Exit code: {result.returncode}\n"
        output += f"Stdout:\n{result.stdout}\n"
        if result.stderr:
            output += f"Stderr:\n{result.stderr}"
        
        return ExecutionResult(
            success=result.returncode == 0,
            result=output,
            error=result.stderr if result.returncode != 0 else None,
        )
    except subprocess.TimeoutExpired:
        return ExecutionResult(
            success=False,
            error=f"Command timed out after {timeout}s",
        )
    except Exception as e:
        return ExecutionResult(
            success=False,
            error=str(e),
        )


async def http_task_handler(
    task: Task,
    context: Dict[str, Any],
) -> ExecutionResult:
    """Handler for HTTP request tasks."""
    import httpx
    
    url = task.input_data.get("url", "")
    method = task.input_data.get("method", "GET").upper()
    headers = task.input_data.get("headers", {})
    body = task.input_data.get("body")
    timeout = task.timeout_seconds or 30
    
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.request(
                method=method,
                url=url,
                headers=headers,
                json=body if isinstance(body, dict) else None,
                content=body if isinstance(body, str) else None,
            )
            
            result = {
                "status_code": response.status_code,
                "headers": dict(response.headers),
                "body": response.text[:10000],  # Limit body size
            }
            
            return ExecutionResult(
                success=response.is_success,
                result=str(result),
                error=None if response.is_success else f"HTTP {response.status_code}",
            )
    except Exception as e:
        return ExecutionResult(
            success=False,
            error=str(e),
        )


def register_builtin_handlers(executor: TaskExecutor) -> None:
    """Register built-in task handlers."""
    executor.register_handler(TaskType.SHELL, shell_task_handler)
    executor.register_handler(TaskType.HTTP, http_task_handler)
