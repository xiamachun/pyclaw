"""
Tool registry and definitions for agent capabilities.

Design inspired by claude-code:
- Tools self-describe concurrency safety (concurrency_safety)
- Support dynamic determination (is_concurrency_safe method)
"""

import os
from enum import Enum
from typing import Dict, Any, Callable, List, Optional
from pydantic import BaseModel

from pyclaw.config.paths import get_paths as _get_paths
from pyclaw.constants import (
    DEFAULT_SHELL_TIMEOUT,
    MEMORY_CONTENT_PREVIEW_LENGTH,
    SKILL_SHELL_TIMEOUT,
)
_paths = _get_paths()
WORKSPACE_ROOT = str(_paths.workspace_dir)


class ConcurrencySafety(Enum):
    """Concurrency safety level"""
    SAFE = "safe"           # Can execute concurrently (e.g., read operations)
    UNSAFE = "unsafe"       # Must execute serially (e.g., write/shell)
    DYNAMIC = "dynamic"     # Dynamically determine based on parameters

def _validate_path(path: str) -> str:
    """Resolve and validate that path is within WORKSPACE_ROOT.
    
    Raises ValueError if path escapes the workspace.
    Returns the resolved absolute path.
    """
    resolved = os.path.realpath(os.path.join(WORKSPACE_ROOT, path) if not os.path.isabs(path) else path)
    workspace_resolved = os.path.realpath(WORKSPACE_ROOT)
    if not resolved.startswith(workspace_resolved + os.sep) and resolved != workspace_resolved:
        raise ValueError(f"Access denied: path '{path}' is outside workspace '{WORKSPACE_ROOT}'")
    return resolved


class ToolDefinition(BaseModel):
    """Definition of a tool available to agents.
    
    Tool definition inspired by claude-code:
    - concurrency_safety: Declare concurrency safety
    - is_concurrency_safe(): Dynamic determination method
    """
    
    model_config = {"arbitrary_types_allowed": True}

    name: str
    description: str
    parameters: Dict[str, Any]
    handler: Callable[[Dict[str, Any], Dict[str, Any]], Any]
    
    # Concurrency safety (default unsafe, must execute serially)
    concurrency_safety: ConcurrencySafety = ConcurrencySafety.UNSAFE
    
    def is_concurrency_safe(self, args: Dict[str, Any]) -> bool:
        """
        Determine if execution is safe under given parameters
        
        Args:
            args: Tool parameters
            
        Returns:
            Whether concurrent execution is safe
        """
        if self.concurrency_safety == ConcurrencySafety.SAFE:
            return True
        elif self.concurrency_safety == ConcurrencySafety.UNSAFE:
            return False
        else:
            # Dynamic determination - subclasses can override this method
            return False


class ToolRegistry:
    """Registry for managing available tools."""
    
    def __init__(self):
        """Initialize empty tool registry."""
        self._tools: Dict[str, ToolDefinition] = {}
    
    def register(self, tool_def: ToolDefinition) -> None:
        """
        Register a tool definition.
        
        Args:
            tool_def: ToolDefinition to register
        """
        self._tools[tool_def.name] = tool_def
    
    def get(self, name: str) -> Optional[ToolDefinition]:
        """
        Get tool definition by name.
        
        Args:
            name: Tool name
            
        Returns:
            ToolDefinition or None if not found
        """
        return self._tools.get(name)
    
    def list_all(self) -> List[ToolDefinition]:
        """
        Get list of all registered tools.
        
        Returns:
            List of ToolDefinition objects
        """
        return list(self._tools.values())
    
    def list_for_session(
        self,
        session: Dict[str, Any],
        policy: Dict[str, Any]
    ) -> List[ToolDefinition]:
        """
        Get tools available for a session based on security policy.
        
        Args:
            session: Session context
            policy: Security policy dict
            
        Returns:
            List of allowed ToolDefinition objects
        """
        # Get allowed tool names from policy
        allowed_tools = policy.get('allowed_tools', [])
        blocked_tools = policy.get('blocked_tools', [])
        
        # Filter tools
        available_tools = []
        for tool in self.list_all():
            if blocked_tools and tool.name in blocked_tools:
                continue
            if allowed_tools and tool.name not in allowed_tools:
                continue
            available_tools.append(tool)
        
        return available_tools
    
    def unregister(self, name: str) -> bool:
        """
        Unregister a tool by name.
        
        Args:
            name: Tool name
            
        Returns:
            True if tool was unregistered, False if not found
        """
        if name in self._tools:
            del self._tools[name]
            return True
        return False


async def shell_tool_handler(args: Dict[str, Any], session: Dict[str, Any]) -> str:
    """
    Built-in shell tool handler.
    
    Args:
        args: Tool arguments (command, timeout)
        session: Session context
        
    Returns:
        Command output as string
    """
    import subprocess
    import os
    
    command = args.get('command', '')
    timeout = int(args.get('timeout', DEFAULT_SHELL_TIMEOUT))
    
    try:
        # Security: block directory deletion commands
        dangerous_patterns = ["rm -r", "rm -rf", "rmdir", "rm -R"]
        for pattern in dangerous_patterns:
            if pattern in command:
                return f"Security: directory deletion command '{pattern}' is not allowed"
        
        # Load environment variables from ~/.pyclaw/.env
        env = os.environ.copy()
        pyclaw_env_file = str(_paths.env_file)
        if os.path.exists(pyclaw_env_file):
            with open(pyclaw_env_file, "r") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        key, _, value = line.partition("=")
                        env[key.strip()] = value.strip()
        
        # Determine if conda environment is needed
        # Simple commands don't need conda activation: curl, echo, cat, ls, pwd, date, whoami, which, env
        simple_commands = ['curl', 'echo', 'cat', 'ls', 'pwd', 'date', 'whoami', 'which', 'env', 'grep', 'head', 'tail', 'wc']
        cmd_first_word = command.strip().split()[0] if command.strip() else ''
        needs_conda = not any(command.strip().startswith(cmd) for cmd in simple_commands)
        
        if needs_conda:
            # Commands requiring Python environment, activate conda
            # Auto-detect Python environment: use the same interpreter running this process
            import sys
            python_dir = os.path.dirname(sys.executable)
            env_prefix = f'export PATH="{python_dir}:$PATH" && cd {WORKSPACE_ROOT} && '
            full_command = env_prefix + command
        else:
            # Simple commands execute directly
            full_command = command

        import logging as _logging
        _shell_logger = _logging.getLogger("pyclaw.agents.tools.shell")

        _shell_logger.info("Shell exec: command=%s, timeout=%d, cwd=%s", command, timeout, WORKSPACE_ROOT)

        result = subprocess.run(
            full_command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=WORKSPACE_ROOT,
            executable="/bin/bash",
            env=env,
        )
        output = f"Exit code: {result.returncode}\nStdout:\n{result.stdout}\nStderr:\n{result.stderr}"
        if result.returncode != 0:
            _shell_logger.warning(
                "Shell FAILED: exit_code=%d, command=%s\nStdout: %s\nStderr: %s",
                result.returncode, command,
                result.stdout[:MEMORY_CONTENT_PREVIEW_LENGTH] if result.stdout else "(empty)",
                result.stderr[:MEMORY_CONTENT_PREVIEW_LENGTH] if result.stderr else "(empty)",
            )
            return f"ERROR: Command failed with exit code {result.returncode}. You MUST fix the error and retry.\n{output}"

        # Detect pip install commands and remind the LLM that installing
        # a package is never the final step — it must continue with the
        # actual task (write script → execute → verify).
        command_lower = command.strip().lower()
        if command_lower.startswith("pip install") or command_lower.startswith("pip3 install"):
            output += (
                "\n\n[REMINDER] Package installation succeeded. "
                "This is only a preparation step — you MUST now continue with "
                "the actual task: write the script with file_write, then execute "
                "it with shell, then verify the output. Do NOT stop here."
            )

        return output
    except subprocess.TimeoutExpired:
        return f"Command timed out after {timeout} seconds"
    except Exception as e:
        return f"Error executing command: {str(e)}"


async def file_read_tool_handler(args: Dict[str, Any], session: Dict[str, Any]) -> str:
    """
    Built-in file read tool handler.
    
    Args:
        args: Tool arguments (path, start_line, end_line)
        session: Session context
        
    Returns:
        File content as string
    """
    path = args.get('path', '')
    start_line = args.get('start_line', 1)
    end_line = args.get('end_line', None)
    
    try:
        path = _validate_path(path)
        with open(path, 'r') as f:
            lines = f.readlines()
        
        if end_line is None:
            end_line = len(lines)
        
        content = ''.join(lines[start_line - 1:end_line])
        return content
    except FileNotFoundError:
        return f"File not found: {path}"
    except Exception as e:
        return f"Error reading file: {str(e)}"


BINARY_FILE_EXTENSIONS = {
    '.ppt', '.pptx', '.doc', '.docx', '.xls', '.xlsx',
    '.pdf', '.zip', '.tar', '.gz', '.bz2', '.7z', '.rar',
    '.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp', '.ico', '.svg',
    '.mp3', '.mp4', '.avi', '.mov', '.wav', '.flac',
    '.exe', '.dll', '.so', '.dylib', '.bin', '.dat',
    '.woff', '.woff2', '.ttf', '.otf', '.eot',
}

async def file_write_tool_handler(args: Dict[str, Any], session: Dict[str, Any]) -> str:
    """
    Built-in file write tool handler.
    
    Args:
        args: Tool arguments (path, content, mode)
        session: Session context
        
    Returns:
        Success message as string
    """
    path = args.get('path', '')
    content = args.get('content', '')
    mode = args.get('mode', 'w')
    
    try:
        path = _validate_path(path)
        if os.path.isdir(path):
            return f"ERROR: path '{path}' is a directory, not a file"

        # Block binary file formats — these cannot be created with plain text
        _, ext = os.path.splitext(path)
        if ext.lower() in BINARY_FILE_EXTENSIONS:
            return (
                f"ERROR: Cannot write binary format '{ext}' with file_write. "
                f"file_write only supports plain text files (.txt, .py, .md, .json, .html, .css, .js, etc.). "
                f"To create a {ext} file, write a Python script that uses the appropriate library "
                f"(e.g. python-pptx for .pptx, python-docx for .docx, openpyxl for .xlsx) "
                f"and execute it with the shell tool. "
                f"Use `from pptx.util import Pt` for font sizes (e.g. `Pt(24)` not raw `24`)."
            )

        with open(path, mode) as f:
            f.write(content)
        return f"Successfully wrote to {path}"
    except Exception as e:
        return f"ERROR: Failed to write file: {str(e)}"


async def web_search_tool_handler(args: Dict[str, Any], session: Dict[str, Any]) -> str:
    """
    Built-in web search tool handler.
    
    Args:
        args: Tool arguments (query, num_results)
        session: Session context
        
    Returns:
        Search results as string
    """
    # Placeholder implementation
    query = args.get('query', '')
    num_results = args.get('num_results', 5)
    
    # In a real implementation, this would call a search API
    return f"Search results for '{query}' (showing {num_results} results):\n[Placeholder - implement actual search API integration]"


async def memory_save_tool_handler(args: Dict[str, Any], session: Dict[str, Any]) -> str:
    """
    Save important information to long-term memory
    
    Args:
        args: Tool arguments (content, category)
        session: Session context (should contain memory_manager)
        
    Returns:
        Save result
    """
    content = args.get('content', '')
    category = args.get('category', 'fact')
    
    if not content:
        return "ERROR: Must provide content to save"
    
    # Get memory_manager from session
    memory_manager = session.get('memory_manager')
    if memory_manager is None:
        # If no memory_manager, try to load global instance
        try:
            from pyclaw.memory.store import MemoryStore
            from pyclaw.memory.manager import MemoryManager
            from pyclaw.memory.embeddings import LocalEmbeddingProvider
            import os
            
            memory_db_path = str(_paths.memory_db)
            os.makedirs(os.path.dirname(memory_db_path), exist_ok=True)
            
            memory_store = MemoryStore(db_path=memory_db_path)
            await memory_store.initialize()
            
            embedding_provider = LocalEmbeddingProvider()
            memory_manager = MemoryManager(
                store=memory_store,
                config={},
                embedding_provider=embedding_provider,
            )
        except Exception as e:
            return f"ERROR: Failed to initialize memory system: {e}"
    
    session_id = session.get('session_id', 'default')
    
    try:
        # Save memory
        entry = await memory_manager.remember(
            session_id=session_id,
            agent_name="pyclaw",
            content=content,
            metadata={"category": category},
        )
        return f"✅ Saved to long-term memory: {content[:50]}..."
    except Exception as e:
        return f"ERROR: Failed to save memory: {e}"


async def memory_search_tool_handler(args: Dict[str, Any], session: Dict[str, Any]) -> str:
    """
    Search long-term memory
    
    Args:
        args: Tool arguments (query, limit)
        session: Session context (should contain memory_manager)
        
    Returns:
        Search results
    """
    query = args.get('query', '')
    limit = int(args.get('limit', 5))
    
    if not query:
        return "ERROR: Must provide search query"
    
    # Get memory_manager from session
    memory_manager = session.get('memory_manager')
    if memory_manager is None:
        try:
            from pyclaw.memory.store import MemoryStore
            from pyclaw.memory.manager import MemoryManager
            from pyclaw.memory.embeddings import LocalEmbeddingProvider
            import os
            
            memory_db_path = str(_paths.memory_db)
            memory_store = MemoryStore(db_path=memory_db_path)
            await memory_store.initialize()
            
            embedding_provider = LocalEmbeddingProvider()
            memory_manager = MemoryManager(
                store=memory_store,
                config={},
                embedding_provider=embedding_provider,
            )
        except Exception as e:
            return f"ERROR: Failed to initialize memory system: {e}"
    
    session_id = session.get('session_id')
    
    try:
        # Search memory
        results = await memory_manager.recall(
            query=query,
            session_id=session_id,
            limit=limit,
        )
        
        if not results:
            return "No relevant memories found"
        
        output = f"Found {len(results)} relevant memories:\n"
        for i, mem in enumerate(results, 1):
            score_pct = int(mem.score * 100)
            output += f"{i}. [{score_pct}%] {mem.entry.content}\n"
        
        return output
    except Exception as e:
        return f"ERROR: Failed to search memory: {e}"


async def cron_create_tool_handler(args: Dict[str, Any], session: Dict[str, Any]) -> str:
    """
    Create a scheduled cron job
    
    Args:
        args: Tool parameters (name, trigger_type, trigger_args, action_type, action_args)
        session: Session context
        
    Returns:
        Creation result
    """
    name = args.get('name', '')
    description = args.get('description', '')
    trigger_type = args.get('trigger_type', 'cron')
    trigger_args = args.get('trigger_args', {})
    action_type = args.get('action_type', 'dingtalk')  # Default to DingTalk
    action_args = args.get('action_args', {})
    
    if not name:
        return "ERROR: Task name is required"
    
    try:
        from pyclaw.cron.scheduler import CronScheduler, CronJobCreate
        
        # Get global scheduler instance
        scheduler = session.get('cron_scheduler')
        if scheduler is None:
            # Try to create temporary scheduler
            scheduler = CronScheduler()
            await scheduler.start()
        
        # Deduplication check: check if a task already exists at the same time
        existing_jobs = scheduler.list_jobs()
        hour = trigger_args.get('hour')
        minute = trigger_args.get('minute')
        
        for job in existing_jobs:
            job_hour = job.trigger_args.get('hour')
            job_minute = job.trigger_args.get('minute')
            if job_hour == hour and job_minute == minute:
                # A task already exists at the same time, return existing task information
                return f"""⚠️ A scheduled task already exists at this time, no need to create a duplicate!

Task ID: {job.id}
Name: {job.name}
Trigger time: {job_hour}:{job_minute:02d}
Action type: {job.action_type}
Status: {'✅Enabled' if job.enabled else '⛔Disabled'}"""
        
        # Create task
        job_create = CronJobCreate(
            name=name,
            description=description,
            trigger_type=trigger_type,
            trigger_args=trigger_args,
            action_type=action_type,
            action_args=action_args,
        )
        
        # add_job() is a synchronous method
        job_info = scheduler.add_job(job_create)
        
        # Build clean return information
        next_run = job_info.next_run_time or "TBD"
        return f"""✅ Scheduled task created successfully!

Task ID: {job_info.id}
Name: {job_info.name}
Trigger type: {trigger_type}
Action type: {action_type}
Next run: {next_run}

Message content: {action_args.get('message', action_args)}"""
        
    except Exception as e:
        return f"ERROR: Failed to create scheduled task: {e}"


async def cron_list_tool_handler(args: Dict[str, Any], session: Dict[str, Any]) -> str:
    """
    List all scheduled cron jobs
    """
    try:
        from pyclaw.cron.scheduler import CronScheduler
        
        scheduler = session.get('cron_scheduler')
        if scheduler is None:
            scheduler = CronScheduler()
            await scheduler.start()
        
        # list_jobs() is a synchronous method, no await needed
        jobs = scheduler.list_jobs()
        
        if not jobs:
            return "No scheduled tasks currently"
        
        output = f"Total {len(jobs)} scheduled tasks:\n\n"
        for job in jobs:
            status = "✅Enabled" if job.enabled else "⛔Disabled"
            next_run = job.next_run_time or "TBD"
            output += f"- [{job.id}] {job.name} ({status})\n"
            output += f"  Trigger: {job.trigger_type} | Action: {job.action_type}\n"
            output += f"  Next run: {next_run}\n\n"
        
        return output
    except Exception as e:
        return f"ERROR: Failed to query scheduled tasks: {e}"


async def cron_delete_tool_handler(args: Dict[str, Any], session: Dict[str, Any]) -> str:
    """
    Delete a scheduled cron job
    """
    job_id = args.get('job_id', '')
    if not job_id:
        return "ERROR: Task ID is required"
    
    try:
        from pyclaw.cron.scheduler import CronScheduler
        
        scheduler = session.get('cron_scheduler')
        if scheduler is None:
            scheduler = CronScheduler()
            await scheduler.start()
        
        # delete_job() is a synchronous method
        success = scheduler.delete_job(job_id)
        if success:
            return f"✅ Scheduled task deleted: {job_id}"
        else:
            return f"ERROR: Task does not exist: {job_id}"
    except Exception as e:
        return f"ERROR: Failed to delete scheduled task: {e}"


def register_builtin_tools(registry: ToolRegistry) -> None:
    """
    Register all built-in tools to the registry.
    
    Concurrency safety configuration (inspired by claude-code):
    - shell: UNSAFE - may modify system state
    - file_read: SAFE - read-only operation
    - file_write: UNSAFE - write operations may conflict
    - web_search: SAFE - read-only operation
    - memory_save: UNSAFE - writes to memory store
    - memory_search: SAFE - read-only memory store
    
    Args:
        registry: ToolRegistry to register tools to
    """
    # Shell tool - UNSAFE (serial execution)
    registry.register(ToolDefinition(
        name="shell",
        description="Execute shell commands",
        parameters={
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to execute"
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds",
                    "default": SKILL_SHELL_TIMEOUT
                }
            },
            "required": ["command"]
        },
        handler=shell_tool_handler,
        concurrency_safety=ConcurrencySafety.UNSAFE,  # Serial execution
    ))
    
    # File read tool - SAFE (concurrent)
    registry.register(ToolDefinition(
        name="file_read",
        description="Read file contents",
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to file"
                },
                "start_line": {
                    "type": "integer",
                    "description": "Start line (1-indexed)",
                    "default": 1
                },
                "end_line": {
                    "type": "integer",
                    "description": "End line (1-indexed, optional)"
                }
            },
            "required": ["path"]
        },
        handler=file_read_tool_handler,
        concurrency_safety=ConcurrencySafety.SAFE,  # Concurrent execution
    ))
    
    # File write tool - UNSAFE (serial execution)
    registry.register(ToolDefinition(
        name="file_write",
        description="Write content to file",
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to file"
                },
                "content": {
                    "type": "string",
                    "description": "Content to write"
                },
                "mode": {
                    "type": "string",
                    "description": "Write mode (w for write, a for append)",
                    "default": "w"
                }
            },
            "required": ["path", "content"]
        },
        handler=file_write_tool_handler,
        concurrency_safety=ConcurrencySafety.UNSAFE,  # Serial execution
    ))
    
    # Web search tool - SAFE (concurrent)
    registry.register(ToolDefinition(
        name="web_search",
        description="Search the web for information",
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query"
                },
                "num_results": {
                    "type": "integer",
                    "description": "Number of results to return",
                    "default": 5
                }
            },
            "required": ["query"]
        },
        handler=web_search_tool_handler,
        concurrency_safety=ConcurrencySafety.SAFE,  # Concurrent execution
    ))
    
    # Memory save tool - save important information to long-term memory
    registry.register(ToolDefinition(
        name="memory_save",
        description="Save important information to long-term memory. Use this when the user tells you something important to remember, like their name, preferences, or key facts.",
        parameters={
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The important information to remember (e.g., 'User's name is Alice', 'User prefers Python over JavaScript')"
                },
                "category": {
                    "type": "string",
                    "description": "Category of memory: 'user_info', 'preference', 'fact', 'task'",
                    "default": "fact"
                }
            },
            "required": ["content"]
        },
        handler=memory_save_tool_handler,
        concurrency_safety=ConcurrencySafety.UNSAFE,  # Serial execution
    ))
    
    # Memory search tool - search long-term memory
    registry.register(ToolDefinition(
        name="memory_search",
        description="Search long-term memory for relevant information. Use this when you need to recall something the user told you before.",
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query (e.g., 'user name', 'favorite color')"
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of results",
                    "default": 5
                }
            },
            "required": ["query"]
        },
        handler=memory_search_tool_handler,
        concurrency_safety=ConcurrencySafety.SAFE,  # Concurrent execution
    ))
    
    # Cron create tool - create scheduled task directly
    registry.register(ToolDefinition(
        name="cron_create",
        description="""Create a scheduled cron job. Use this tool directly when user asks for scheduled tasks/reminders.

IMPORTANT: 
- Use action_type='dingtalk' to send message to user's DingTalk
- Default trigger_type is 'cron' for daily tasks
- For daily reminders, set trigger_args like {"hour": 11, "minute": 30}

Examples:
1. "Every day at 11:30 remind me to drink water"
   -> name="health_reminder", trigger_type="cron", trigger_args={"hour":11,"minute":30},
      action_type="dingtalk", action_args={"message":"💧 Remember to take a break and drink some water!"}

2. "Every 2 hours remind me to rest"
   -> trigger_type="interval", trigger_args={"hours":2}""",
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Task name"
                },
                "description": {
                    "type": "string",
                    "description": "Task description",
                    "default": ""
                },
                "trigger_type": {
                    "type": "string",
                    "description": "Trigger type: cron(scheduled), interval(interval), date(one-time)",
                    "enum": ["cron", "interval", "date"]
                },
                "trigger_args": {
                    "type": "object",
                    "description": "Trigger parameters. cron: {hour,minute,second,day_of_week}; interval: {hours,minutes,seconds}; date: {run_date}"
                },
                "action_type": {
                    "type": "string",
                    "description": "Action type: dingtalk(send DingTalk, default), message(print log), shell, http",
                    "enum": ["dingtalk", "message", "shell", "http"],
                    "default": "dingtalk"
                },
                "action_args": {
                    "type": "object",
                    "description": "Action parameters. dingtalk/message: {message}; shell: {command}; http: {url,method}"
                }
            },
            "required": ["name", "trigger_type", "trigger_args", "action_args"]
        },
        handler=cron_create_tool_handler,
        concurrency_safety=ConcurrencySafety.UNSAFE,  # Serial execution
    ))
    
    # Cron list tool - query scheduled tasks
    registry.register(ToolDefinition(
        name="cron_list",
        description="List all scheduled cron jobs. Use this before creating new jobs to avoid duplicates.",
        parameters={
            "type": "object",
            "properties": {},
            "required": []
        },
        handler=cron_list_tool_handler,
        concurrency_safety=ConcurrencySafety.SAFE,
    ))
    
    # Cron delete tool - delete scheduled task
    registry.register(ToolDefinition(
        name="cron_delete",
        description="Delete a scheduled cron job by its ID.",
        parameters={
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "The job ID to delete"
                }
            },
            "required": ["job_id"]
        },
        handler=cron_delete_tool_handler,
        concurrency_safety=ConcurrencySafety.UNSAFE,
    ))
