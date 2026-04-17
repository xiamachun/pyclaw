"""PyClaw exception hierarchy."""


class PyClawError(Exception):
    """Base exception for all PyClaw errors."""


class ConfigError(PyClawError):
    """Configuration-related error."""


class SecurityError(PyClawError):
    """Security policy violation."""


class AuthenticationError(SecurityError):
    """Authentication failure."""


class AuthorizationError(SecurityError):
    """Authorization / permission failure."""


class ChannelError(PyClawError):
    """Channel communication error."""


class PluginError(PyClawError):
    """Plugin loading or execution error."""


class SkillError(PyClawError):
    """Skill loading or parsing error."""


class SessionError(PyClawError):
    """Session management error."""


class AgentError(PyClawError):
    """Agent runtime error."""


class ToolError(AgentError):
    """Tool execution error."""


class SandboxError(SecurityError):
    """Sandbox isolation error."""


class MemoryStoreError(PyClawError):
    """Memory system error."""


class MediaError(PyClawError):
    """Media processing error."""


class GatewayError(PyClawError):
    """Gateway server error."""
