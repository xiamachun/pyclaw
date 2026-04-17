"""
Path Management for PyClaw

This module manages all file system paths used by PyClaw, including
state directories, configuration files, logs, and data storage.
"""

import os
from pathlib import Path
from typing import Optional


class PyClawPaths:
    """Manages all file system paths for PyClaw."""
    
    def __init__(self, state_dir: Optional[str] = None):
        """Initialize PyClawPaths with optional state directory override."""
        # State directory - can be overridden by PYCLAW_STATE_DIR env var
        self.state_dir = Path(state_dir or os.environ.get(
            'PYCLAW_STATE_DIR', 
            str(Path.home() / '.pyclaw')
        ))
        
        # Configuration file: project root pyclaw.json by default
        config_path = os.environ.get('PYCLAW_CONFIG_PATH')
        if config_path:
            self.config_file = Path(config_path)
        else:
            # Look for pyclaw.json in project root first, fall back to state dir
            project_root_config = Path(__file__).resolve().parent.parent.parent / 'pyclaw.json'
            if project_root_config.exists():
                self.config_file = project_root_config
            else:
                self.config_file = self.state_dir / 'pyclaw.json'
        
        # Log directory - can be overridden by PYCLAW_LOG_DIR env var
        log_dir = os.environ.get('PYCLAW_LOG_DIR')
        self.log_dir = Path(log_dir) if log_dir else self.state_dir / 'logs'
        
        # Workspace directory - can be overridden by PYCLAW_WORKSPACE env var
        workspace_dir = os.environ.get('PYCLAW_WORKSPACE')
        self.workspace_dir = Path(workspace_dir) if workspace_dir else self.state_dir / 'workspace'
        self.workspace_skills_dir = self.workspace_dir / 'skills'
        self.workspace_memory_dir = self.workspace_dir / 'memory'
        
        # State subdirectory for SQLite databases
        self.state_subdir = self.state_dir / 'state'
        
        # Memory database - can be overridden by PYCLAW_MEMORY_DB env var
        memory_db = os.environ.get('PYCLAW_MEMORY_DB')
        self.memory_db = Path(memory_db) if memory_db else self.state_subdir / 'memory.sqlite'
        
        # Other databases
        self.tasks_db = self.state_subdir / 'tasks.sqlite'
        self.approvals_db = self.state_subdir / 'approvals.sqlite'
        self.costs_db = self.state_subdir / 'costs.sqlite'
        self.cron_db = self.state_dir / 'cron_jobs.db'
        
        # Log files
        self.gateway_log = self.state_dir / 'gateway.log'
        self.dingtalk_log = self.state_dir / 'dingtalk.log'
        
        # DingTalk files
        self.dingtalk_sessions_file = self.state_dir / 'dingtalk_sessions.json'
        self.dingtalk_webhooks_file = self.state_dir / 'dingtalk_webhooks.json'
        
        # Configuration files
        self.auth_profiles_file = self.state_dir / 'auth_profiles.json'
        self.message_queue_file = self.state_dir / 'message_queue.json'
        self.env_file = self.state_dir / '.env'
        
        # Other directories
        self.sessions_dir = self.state_dir / 'sessions'
        self.media_dir = self.state_dir / 'media'
        self.plugins_dir = self.state_dir / 'plugins'
        self.skills_dir = self.state_dir / 'skills'
    
    def ensure_dirs(self) -> None:
        """Create all necessary directories if they don't exist."""
        directories = [
            self.state_dir,
            self.log_dir,
            self.sessions_dir,
            self.media_dir,
            self.plugins_dir,
            self.skills_dir,
            self.state_subdir,
            self.workspace_dir,
            self.workspace_memory_dir,
        ]
        
        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)
    
    def __repr__(self) -> str:
        """String representation of PyClawPaths."""
        return f"PyClawPaths(state_dir={self.state_dir}, config_file={self.config_file})"


_global_paths: PyClawPaths | None = None


def get_paths() -> PyClawPaths:
    """Get or create the global PyClawPaths singleton."""
    global _global_paths
    if _global_paths is None:
        _global_paths = PyClawPaths()
    return _global_paths