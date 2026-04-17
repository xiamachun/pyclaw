"""
Execution approval type definitions.

Defines approval request types, decisions, and policies.
"""

from enum import Enum
from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta
from pydantic import BaseModel, Field
import uuid


class ApprovalType(str, Enum):
    """Types of operations requiring approval."""
    
    EXEC = "exec"              # Shell command execution
    FILE_WRITE = "file_write"  # File write operations
    FILE_DELETE = "file_delete"  # File deletion
    NETWORK = "network"        # Network requests
    PLUGIN = "plugin"          # Plugin operations
    INSTALL = "install"        # Package installation
    SYSTEM = "system"          # System-level operations


class ApprovalDecision(str, Enum):
    """Possible approval decisions."""
    
    PENDING = "pending"              # Waiting for decision
    ALLOW_ONCE = "allow_once"        # Allow this specific request
    ALLOW_ALWAYS = "allow_always"    # Allow all similar requests
    DENY = "deny"                    # Deny this request
    DENY_ALWAYS = "deny_always"      # Deny all similar requests
    EXPIRED = "expired"              # Request expired without decision


class ApprovalStatus(str, Enum):
    """Status of an approval request."""
    
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class ApprovalRequest(BaseModel):
    """Approval request object.

    Represents a request that needs approval before executing an operation.
    """
    
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    type: ApprovalType = Field(..., description="Type of operation")
    
    # Operation details
    command: Optional[str] = Field(None, description="Command to execute")
    args: Dict[str, Any] = Field(default_factory=dict, description="Operation arguments")
    working_dir: Optional[str] = Field(None, description="Working directory")
    
    # Context
    session_id: Optional[str] = Field(None, description="Related session")
    user_id: Optional[str] = Field(None, description="Requesting user")
    channel: Optional[str] = Field(None, description="Source channel")
    
    # Risk assessment
    risk_level: str = Field("medium", description="Risk level: low, medium, high, critical")
    risk_factors: List[str] = Field(default_factory=list, description="Why this is risky")
    
    # Status
    status: ApprovalStatus = Field(default=ApprovalStatus.PENDING)
    decision: Optional[ApprovalDecision] = None
    decision_reason: Optional[str] = None
    decided_by: Optional[str] = None
    decided_at: Optional[datetime] = None
    
    # Timing
    created_at: datetime = Field(default_factory=datetime.now)
    expires_at: datetime = Field(
        default_factory=lambda: datetime.now() + timedelta(minutes=5)
    )
    
    # Metadata
    metadata: Dict[str, Any] = Field(default_factory=dict)
    
    model_config = {"arbitrary_types_allowed": True}
    
    def is_expired(self) -> bool:
        """Check if request has expired.

        Returns:
            True if expired
        """
        return datetime.now() > self.expires_at
    
    def get_pattern(self) -> str:
        """Get pattern string for matching similar requests.

        Used for allow_always/deny_always policies.

        Returns:
            Pattern string
        """
        if self.type == ApprovalType.EXEC:
            # Extract command name for pattern
            cmd = self.command or ""
            parts = cmd.strip().split()
            if parts:
                return f"exec:{parts[0]}"
            return "exec:*"
        
        elif self.type == ApprovalType.FILE_WRITE:
            # Use path pattern
            path = self.args.get("path", "")
            return f"file_write:{path}"
        
        elif self.type == ApprovalType.NETWORK:
            # Use domain
            url = self.args.get("url", "")
            return f"network:{url}"
        
        return f"{self.type.value}:*"


class ApprovalPolicy(BaseModel):
    """Approval policy object.

    Policy for automatically approving or denying requests.
    """
    
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    
    # Matching
    pattern: str = Field(..., description="Pattern to match (regex)")
    type: Optional[ApprovalType] = Field(None, description="Specific type to match")
    
    # Decision
    decision: ApprovalDecision = Field(..., description="Decision to apply")
    
    # Scope
    session_id: Optional[str] = Field(None, description="Apply to specific session")
    user_id: Optional[str] = Field(None, description="Apply to specific user")
    
    # Validity
    created_at: datetime = Field(default_factory=datetime.now)
    expires_at: Optional[datetime] = Field(None, description="Policy expiration")
    created_by: Optional[str] = Field(None, description="Who created this policy")
    
    # Metadata
    description: Optional[str] = Field(None)
    enabled: bool = Field(True)
    
    model_config = {"arbitrary_types_allowed": True}
    
    def is_valid(self) -> bool:
        """Check if policy is still valid.

        Returns:
            True if policy is valid
        """
        if not self.enabled:
            return False
        if self.expires_at and datetime.now() > self.expires_at:
            return False
        return True
    
    def matches(self, request: ApprovalRequest) -> bool:
        """Check if policy matches request.

        Args:
            request: Approval request

        Returns:
            True if policy matches
        """
        import re
        
        if not self.is_valid():
            return False
        
        # Check type
        if self.type and self.type != request.type:
            return False
        
        # Check session scope
        if self.session_id and self.session_id != request.session_id:
            return False
        
        # Check user scope
        if self.user_id and self.user_id != request.user_id:
            return False
        
        # Check pattern
        request_pattern = request.get_pattern()
        try:
            if re.match(self.pattern, request_pattern):
                return True
            # Also try matching against the command directly
            if request.command and re.match(self.pattern, request.command):
                return True
        except re.error:
            pass
        
        return False


class ApprovalEvent(BaseModel):
    """Event emitted when approval status changes."""
    
    request_id: str
    event_type: str  # "created", "approved", "denied", "expired"
    decision: Optional[ApprovalDecision] = None
    timestamp: datetime = Field(default_factory=datetime.now)
    metadata: Dict[str, Any] = Field(default_factory=dict)


# Risk assessment helpers

def assess_risk(
    type: ApprovalType,
    command: Optional[str] = None,
    args: Optional[Dict[str, Any]] = None,
) -> tuple[str, List[str]]:
    """Assess risk level of operation.

    Args:
        type: Operation type
        command: Command (for exec type)
        args: Operation parameters

    Returns:
        Tuple of (risk level, list of risk factors)
    """
    risk_factors = []
    args = args or {}
    
    # Shell command risks
    if type == ApprovalType.EXEC and command:
        cmd_lower = command.lower()
        
        # High risk patterns
        if any(p in cmd_lower for p in ["rm -rf", "rm -r", "rmdir", "del /s"]):
            risk_factors.append("Recursive deletion")
        if any(p in cmd_lower for p in ["sudo", "su ", "runas"]):
            risk_factors.append("Elevated privileges")
        if any(p in cmd_lower for p in ["curl | bash", "wget | bash", "|sh", "|bash"]):
            risk_factors.append("Remote code execution")
        if any(p in cmd_lower for p in ["chmod", "chown", "icacls"]):
            risk_factors.append("Permission changes")
        
        # Medium risk patterns
        if any(p in cmd_lower for p in ["pip install", "npm install", "apt install"]):
            risk_factors.append("Package installation")
        if any(p in cmd_lower for p in ["git push", "git commit"]):
            risk_factors.append("Repository modification")
    
    # File operation risks
    if type == ApprovalType.FILE_WRITE:
        path = args.get("path", "").lower()
        if any(p in path for p in [".env", "config", "secret", "key", "password"]):
            risk_factors.append("Sensitive file")
        if any(p in path for p in ["/etc/", "/usr/", "/bin/", "system32"]):
            risk_factors.append("System path")
    
    # Network risks
    if type == ApprovalType.NETWORK:
        url = args.get("url", "").lower()
        if "http://" in url:
            risk_factors.append("Unencrypted connection")
    
    # Determine level
    if len(risk_factors) >= 3:
        return "critical", risk_factors
    elif len(risk_factors) >= 2:
        return "high", risk_factors
    elif len(risk_factors) >= 1:
        return "medium", risk_factors
    else:
        return "low", risk_factors