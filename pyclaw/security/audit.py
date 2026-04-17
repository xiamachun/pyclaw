import os
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Dict, Any


class AuditSeverity(Enum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFO = "INFO"


@dataclass
class AuditFinding:
    severity: AuditSeverity
    code: str
    title: str
    message: str
    suggestion: str


@dataclass
class AuditReport:
    findings: List[AuditFinding] = field(default_factory=list)
    
    @property
    def critical_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == AuditSeverity.CRITICAL)
    
    @property
    def high_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == AuditSeverity.HIGH)
    
    @property
    def passed(self) -> bool:
        return self.critical_count == 0 and self.high_count == 0
    
    def summary(self) -> str:
        if self.passed:
            return "✓ Security audit passed. All critical and high severity checks passed."
        
        lines = [f"✗ Security audit failed with {len(self.findings)} finding(s):"]
        lines.append(f"  - Critical: {self.critical_count}")
        lines.append(f"  - High: {self.high_count}")
        lines.append(f"  - Medium: {sum(1 for f in self.findings if f.severity == AuditSeverity.MEDIUM)}")
        lines.append(f"  - Low: {sum(1 for f in self.findings if f.severity == AuditSeverity.LOW)}")
        return "\n".join(lines)


class SecurityAuditor:
    def __init__(self):
        pass
    
    def audit(self, config: Dict[str, Any], paths: Dict[str, str]) -> AuditReport:
        findings = []
        
        findings.extend(self._check_bind_address(config))
        findings.extend(self._check_auth_token(config))
        findings.extend(self._check_sandbox(config))
        findings.extend(self._check_credential_redaction(config))
        findings.extend(self._check_audit_logging(config))
        findings.extend(self._check_plugin_policy(config))
        findings.extend(self._check_security_skill(config))
        findings.extend(self._check_data_ttl(config))
        findings.extend(self._check_browser_policy(config))
        findings.extend(self._check_file_permissions(paths))
        
        return AuditReport(findings=findings)
    
    def _check_bind_address(self, config: Dict[str, Any]) -> List[AuditFinding]:
        findings = []
        bind_address = config.get("bind_address", "127.0.0.1")
        
        if not bind_address.startswith("127.0.0.1") and not bind_address.startswith("::1"):
            findings.append(AuditFinding(
                severity=AuditSeverity.CRITICAL,
                code="SEC001",
                title="Server bound to non-loopback address",
                message=f"Server is configured to bind to {bind_address}, which may expose it to the network.",
                suggestion="Bind to 127.0.0.1 or ::1 (localhost) for local development only."
            ))
        
        return findings
    
    def _check_auth_token(self, config: Dict[str, Any]) -> List[AuditFinding]:
        findings = []
        auth_token = config.get("auth_token", "")
        
        if len(auth_token) < 16:
            findings.append(AuditFinding(
                severity=AuditSeverity.HIGH,
                code="SEC002",
                title="Weak authentication token",
                message=f"Authentication token is too short ({len(auth_token)} characters).",
                suggestion="Use a strong authentication token with at least 16 characters."
            ))
        
        return findings
    
    def _check_sandbox(self, config: Dict[str, Any]) -> List[AuditFinding]:
        findings = []
        sandbox_config = config.get("sandbox", {})
        mode = sandbox_config.get("mode", "off")
        
        if mode == "off":
            findings.append(AuditFinding(
                severity=AuditSeverity.HIGH,
                code="SEC003",
                title="Sandbox mode is disabled",
                message="Sandbox mode is set to 'off', which may allow unrestricted code execution.",
                suggestion="Enable sandbox mode by setting sandbox.mode to 'restricted' or 'strict'."
            ))
        
        return findings
    
    def _check_credential_redaction(self, config: Dict[str, Any]) -> List[AuditFinding]:
        findings = []
        redaction_config = config.get("credential_redaction", {})
        enabled = redaction_config.get("enabled", True)
        
        if not enabled:
            findings.append(AuditFinding(
                severity=AuditSeverity.HIGH,
                code="SEC004",
                title="Credential redaction is disabled",
                message="Automatic credential redaction is disabled, which may expose sensitive data in logs.",
                suggestion="Enable credential redaction by setting credential_redaction.enabled to true."
            ))
        
        return findings
    
    def _check_audit_logging(self, config: Dict[str, Any]) -> List[AuditFinding]:
        findings = []
        audit_config = config.get("audit_logging", {})
        enabled = audit_config.get("enabled", True)
        
        if not enabled:
            findings.append(AuditFinding(
                severity=AuditSeverity.MEDIUM,
                code="SEC005",
                title="Audit logging is disabled",
                message="Security audit logging is disabled, reducing security monitoring capabilities.",
                suggestion="Enable audit logging by setting audit_logging.enabled to true."
            ))
        
        return findings
    
    def _check_plugin_policy(self, config: Dict[str, Any]) -> List[AuditFinding]:
        findings = []
        plugin_config = config.get("plugin_policy", {})
        allowlist = plugin_config.get("allowlist", {})
        enabled = allowlist.get("enabled", True)
        
        if not enabled:
            findings.append(AuditFinding(
                severity=AuditSeverity.HIGH,
                code="SEC006",
                title="Plugin allowlist is disabled",
                message="Plugin allowlist is disabled, allowing any plugin to be loaded.",
                suggestion="Enable plugin allowlist by setting plugin_policy.allowlist.enabled to true."
            ))
        
        return findings
    
    def _check_security_skill(self, config: Dict[str, Any]) -> List[AuditFinding]:
        findings = []
        skill_config = config.get("security_skill", {})
        enabled = skill_config.get("enabled", True)
        
        if not enabled:
            findings.append(AuditFinding(
                severity=AuditSeverity.HIGH,
                code="SEC007",
                title="Security skill is disabled",
                message="Security skill is disabled, reducing automated security checks.",
                suggestion="Enable security skill by setting security_skill.enabled to true."
            ))
        
        return findings
    
    def _check_data_ttl(self, config: Dict[str, Any]) -> List[AuditFinding]:
        findings = []
        data_config = config.get("data", {})
        ttl_days = data_config.get("ttl_days", 90)
        
        if ttl_days > 90:
            findings.append(AuditFinding(
                severity=AuditSeverity.MEDIUM,
                code="SEC008",
                title="Data TTL exceeds recommended limit",
                message=f"Data retention period is {ttl_days} days, which exceeds the recommended 90 days.",
                suggestion="Reduce data TTL to 90 days or less by setting data.ttl_days."
            ))
        
        return findings
    
    def _check_browser_policy(self, config: Dict[str, Any]) -> List[AuditFinding]:
        findings = []
        browser_config = config.get("browser_policy", {})
        office_block = browser_config.get("office_block", {})
        enabled = office_block.get("enabled", True)
        
        if not enabled:
            findings.append(AuditFinding(
                severity=AuditSeverity.MEDIUM,
                code="SEC009",
                title="Office URL blocking is disabled",
                message="Blocking of office application URLs is disabled, which may allow access to internal systems.",
                suggestion="Enable office URL blocking by setting browser_policy.office_block.enabled to true."
            ))
        
        return findings
    
    def _check_file_permissions(self, paths: Dict[str, str]) -> List[AuditFinding]:
        findings = []
        state_dir = paths.get("state_dir")
        
        if state_dir and os.path.exists(state_dir):
            try:
                stat_info = os.stat(state_dir)
                mode = stat_info.st_mode & 0o777
                
                if mode & 0o077:
                    findings.append(AuditFinding(
                        severity=AuditSeverity.MEDIUM,
                        code="SEC010",
                        title="State directory has overly permissive permissions",
                        message=f"State directory {state_dir} has permissions {oct(mode)}, allowing group/other access.",
                        suggestion="Restrict permissions to 0700 (owner only) for the state directory."
                    ))
            except (OSError, PermissionError):
                pass
        
        return findings
