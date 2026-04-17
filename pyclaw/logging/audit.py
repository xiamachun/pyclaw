"""
Audit logging for security and compliance.

Provides structured audit logging for authentication, tool execution,
configuration changes, and other security-relevant events.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import structlog


class AuditLogger:
    """
    Audit logger for security and compliance events.
    
    Logs events to both structlog and an optional JSONL audit file.
    """
    
    def __init__(self, audit_log_path: Optional[str] = None) -> None:
        """
        Initialize audit logger.
        
        Args:
            audit_log_path: Optional path to audit log file (JSONL format)
        """
        self._audit_log_path = Path(audit_log_path) if audit_log_path else None
        self._logger = structlog.get_logger('audit')
        
        # Ensure audit log directory exists
        if self._audit_log_path:
            self._audit_log_path.parent.mkdir(parents=True, exist_ok=True)
    
    def _write_audit_line(self, record: dict[str, Any]) -> None:
        """
        Write a single audit record to the audit log file.
        
        Args:
            record: Audit record dictionary
        """
        if not self._audit_log_path:
            return
        
        record['timestamp'] = datetime.utcnow().isoformat()
        with open(self._audit_log_path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record) + '\n')
    
    def _emit(self, event_type: str, **kwargs: Any) -> None:
        """
        Emit an audit event to both structlog and audit file.
        
        Args:
            event_type: Type of audit event
            **kwargs: Additional event data
        """
        # Log to structlog
        self._logger.info(event_type, **kwargs)
        
        # Write to audit file
        audit_record = {'event_type': event_type, **kwargs}
        self._write_audit_line(audit_record)
    
    def log_auth_attempt(self, success: bool, source: str, reason: Optional[str] = None) -> None:
        """
        Log an authentication attempt.
        
        Args:
            success: Whether authentication was successful
            source: Source of the authentication attempt
            reason: Optional reason for failure
        """
        self._emit(
            'auth_attempt',
            success=success,
            source=source,
            reason=reason,
        )
    
    def log_tool_execution(
        self,
        tool_name: str,
        session_id: str,
        approved: bool,
        command: str,
    ) -> None:
        """
        Log a tool execution event.
        
        Args:
            tool_name: Name of the tool being executed
            session_id: Session identifier
            approved: Whether the execution was approved
            command: Command being executed
        """
        self._emit(
            'tool_execution',
            tool_name=tool_name,
            session_id=session_id,
            approved=approved,
            command=command,
        )
    
    def log_channel_message(self, channel: str, direction: str, peer: str) -> None:
        """
        Log a channel message event.
        
        Args:
            channel: Channel name
            direction: Message direction (sent/received)
            peer: Peer identifier
        """
        self._emit(
            'channel_message',
            channel=channel,
            direction=direction,
            peer=peer,
        )
    
    def log_config_change(self, key: str, old_value: Any, new_value: Any) -> None:
        """
        Log a configuration change event.
        
        Args:
            key: Configuration key that changed
            old_value: Previous value
            new_value: New value
        """
        self._emit(
            'config_change',
            key=key,
            old_value=old_value,
            new_value=new_value,
        )
    
    def log_plugin_load(self, plugin_id: str, success: bool, reason: Optional[str] = None) -> None:
        """
        Log a plugin load event.
        
        Args:
            plugin_id: Plugin identifier
            success: Whether the plugin loaded successfully
            reason: Optional reason for failure
        """
        self._emit(
            'plugin_load',
            plugin_id=plugin_id,
            success=success,
            reason=reason,
        )
    
    def log_session_event(self, session_id: str, event: str, details: Optional[dict[str, Any]] = None) -> None:
        """
        Log a session event.
        
        Args:
            session_id: Session identifier
            event: Event type
            details: Optional event details
        """
        self._emit(
            'session_event',
            session_id=session_id,
            event=event,
            details=details or {},
        )
    
    def log_security_finding(self, code: str, severity: str, message: str) -> None:
        """
        Log a security finding.
        
        Args:
            code: Finding code/identifier
            severity: Severity level (low, medium, high, critical)
            message: Finding message
        """
        self._emit(
            'security_finding',
            code=code,
            severity=severity,
            message=message,
        )
    
    def log_data_cleanup(self, target: str, records_removed: int) -> None:
        """
        Log a data cleanup event.
        
        Args:
            target: Target of cleanup operation
            records_removed: Number of records removed
        """
        self._emit(
            'data_cleanup',
            target=target,
            records_removed=records_removed,
        )
