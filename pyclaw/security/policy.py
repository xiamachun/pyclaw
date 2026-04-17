import os
import re
from typing import Optional, Set, Dict, Any, List


# Configurable URL patterns to block in browser tool.
# Override via PYCLAW_BLOCKED_URL_PATTERNS env var (comma-separated regex patterns).
# Default: empty list (no URLs blocked).
_env_patterns = os.environ.get("PYCLAW_BLOCKED_URL_PATTERNS", "")
_OFFICE_URL_PATTERNS: List[str] = [
    p.strip() for p in _env_patterns.split(",") if p.strip()
]


class BrowserPolicy:
    def __init__(self, block_office_urls: bool = True):
        self.block_office_urls = block_office_urls
        self._office_patterns = [re.compile(pattern) for pattern in _OFFICE_URL_PATTERNS]
    
    def check_url(self, url: str) -> tuple[bool, Optional[str]]:
        if not self.block_office_urls:
            return False, None
        
        for pattern in self._office_patterns:
            if pattern.search(url):
                return True, f"URL matches blocked office application pattern: {pattern.pattern}"
        
        return False, None


class ChannelPolicy:
    def __init__(self, allowlist: Optional[Set[str]] = None):
        self.allowlist = allowlist or set()
    
    def validate_channel(self, channel_id: str) -> tuple[bool, Optional[str]]:
        if not self.allowlist:
            return True, None
        
        if channel_id in self.allowlist:
            return True, None
        
        return False, f"Channel '{channel_id}' is not in the allowlist"


class PluginPolicy:
    def __init__(self, allowlist: Optional[Set[str]] = None, blocklist: Optional[Set[str]] = None):
        self.allowlist = allowlist or set()
        self.blocklist = blocklist or set()
    
    def validate_plugin_source(self, source: str) -> tuple[bool, Optional[str]]:
        if source in self.blocklist:
            return False, f"Plugin source '{source}' is in the blocklist"
        
        if self.allowlist and source not in self.allowlist:
            return False, f"Plugin source '{source}' is not in the allowlist"
        
        return True, None


class SandboxPolicy:
    def __init__(self, mode: str = "restricted"):
        self.mode = mode
    
    def should_sandbox(self, session: Dict[str, Any]) -> bool:
        if self.mode == "off":
            return False
        
        if self.mode == "strict":
            return True
        
        if self.mode == "restricted":
            return session.get("requires_sandbox", False)
        
        return False


class ToolPermissionPolicy:
    def __init__(self, permissions: Optional[Dict[str, Set[str]]] = None):
        self.permissions = permissions or {}
    
    def check_permission(self, tool_name: str, session: Dict[str, Any]) -> tuple[bool, Optional[str]]:
        if not self.permissions:
            return True, None
        
        session_id = session.get("id", "default")
        allowed_tools = self.permissions.get(session_id, set())
        
        if not allowed_tools:
            return True, None
        
        if tool_name in allowed_tools:
            return True, None
        
        return False, f"Tool '{tool_name}' is not permitted for session '{session_id}'"
