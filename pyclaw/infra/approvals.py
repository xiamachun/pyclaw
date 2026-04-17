"""
Execution approval manager.

Manages approval requests, policies, and decisions for risky operations.

Features:
- Approval request lifecycle
- Policy-based auto-approval
- WebSocket notifications
- Persistence
"""

import asyncio
import json
import logging
import re
from typing import Optional, Dict, Any, List, Callable, Awaitable
from datetime import datetime, timedelta
from pathlib import Path
import aiosqlite

from pyclaw.infra.approval_types import (
    ApprovalType,
    ApprovalDecision,
    ApprovalStatus,
    ApprovalRequest,
    ApprovalPolicy,
    ApprovalEvent,
    assess_risk,
)
from pyclaw.constants import DEFAULT_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)


# Event handler type
ApprovalEventHandler = Callable[[ApprovalEvent], Awaitable[None]]


class ApprovalManager:
    """
    Manages execution approvals.
    
    Features:
    - Request creation and tracking
    - Policy-based auto-approval
    - Async decision waiting
    - SQLite persistence
    """
    
    def __init__(
        self,
        store_path: Optional[Path] = None,
        default_timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        db_path: Optional[str] = None,  # Alias parameter
    ):
        """
        Initialize the approval manager.
        
        Args:
            store_path: Path to SQLite database
            default_timeout_seconds: Default request timeout
            db_path: Alias for store_path (string)
        """
        # Support db_path alias
        if db_path is not None:
            store_path = Path(db_path)
        elif store_path is None:
            from pyclaw.config.paths import get_paths as _get_paths
            store_path = _get_paths().approvals_db
        elif isinstance(store_path, str):
            store_path = Path(store_path)
        
        self._store_path = store_path
        self._default_timeout = default_timeout_seconds
        self._db: Optional[aiosqlite.Connection] = None
        
        # In-memory state
        self._pending: Dict[str, ApprovalRequest] = {}
        self._policies: List[ApprovalPolicy] = []
        
        # Waiting for decisions
        self._waiters: Dict[str, asyncio.Event] = {}
        
        # Event handlers
        self._event_handlers: List[ApprovalEventHandler] = []
        
        # Lock
        self._lock = asyncio.Lock()
    
    async def initialize(self) -> None:
        """Initialize database connection and load approval policies."""
        self._store_path.parent.mkdir(parents=True, exist_ok=True)
        
        self._db = await aiosqlite.connect(str(self._store_path))
        
        await self._db.executescript("""
            CREATE TABLE IF NOT EXISTS requests (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                command TEXT,
                args TEXT,
                session_id TEXT,
                user_id TEXT,
                status TEXT NOT NULL,
                decision TEXT,
                risk_level TEXT,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                decided_at TEXT,
                metadata TEXT
            );
            
            CREATE INDEX IF NOT EXISTS idx_requests_status ON requests(status);
            CREATE INDEX IF NOT EXISTS idx_requests_session ON requests(session_id);
            
            CREATE TABLE IF NOT EXISTS policies (
                id TEXT PRIMARY KEY,
                pattern TEXT NOT NULL,
                type TEXT,
                decision TEXT NOT NULL,
                session_id TEXT,
                user_id TEXT,
                enabled INTEGER DEFAULT 1,
                created_at TEXT NOT NULL,
                expires_at TEXT,
                description TEXT
            );
        """)
        
        await self._db.commit()
        
        # Load policies
        await self._load_policies()
        
        logger.info("ApprovalManager initialized")
    
    async def _load_policies(self) -> None:
        """Load policies from database."""
        if not self._db:
            return
        
        self._policies.clear()
        
        async with self._db.execute(
            "SELECT * FROM policies WHERE enabled = 1"
        ) as cursor:
            async for row in cursor:
                policy = ApprovalPolicy(
                    id=row[0],
                    pattern=row[1],
                    type=ApprovalType(row[2]) if row[2] else None,
                    decision=ApprovalDecision(row[3]),
                    session_id=row[4],
                    user_id=row[5],
                    enabled=bool(row[6]),
                    created_at=datetime.fromisoformat(row[7]),
                    expires_at=datetime.fromisoformat(row[8]) if row[8] else None,
                    description=row[9],
                )
                
                if policy.is_valid():
                    self._policies.append(policy)
        
        logger.debug("Loaded %s approval policies", len(self._policies))
    
    async def request_approval(
        self,
        type: ApprovalType,
        command: Optional[str] = None,
        args: Optional[Dict[str, Any]] = None,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
        timeout_seconds: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ApprovalRequest:
        """
        Create an approval request.
        
        Args:
            type: Type of operation
            command: Command to execute (for exec type)
            args: Operation arguments
            session_id: Related session
            user_id: Requesting user
            timeout_seconds: Request timeout
            metadata: Additional metadata
            
        Returns:
            The ApprovalRequest
        """
        args = args or {}
        timeout = timeout_seconds or self._default_timeout
        
        # Assess risk
        risk_level, risk_factors = assess_risk(type, command, args)
        
        request = ApprovalRequest(
            type=type,
            command=command,
            args=args,
            session_id=session_id,
            user_id=user_id,
            risk_level=risk_level,
            risk_factors=risk_factors,
            expires_at=datetime.now() + timedelta(seconds=timeout),
            metadata=metadata or {},
        )
        
        async with self._lock:
            # Check for matching policy
            for policy in self._policies:
                if policy.matches(request):
                    # Auto-apply policy
                    if policy.decision in (ApprovalDecision.ALLOW_ONCE, ApprovalDecision.ALLOW_ALWAYS):
                        request.status = ApprovalStatus.APPROVED
                        request.decision = policy.decision
                        request.decision_reason = f"Auto-approved by policy: {policy.description or policy.id}"
                        request.decided_at = datetime.now()
                        logger.info("Auto-approved request %s by policy %s", request.id, policy.id)
                    else:
                        request.status = ApprovalStatus.DENIED
                        request.decision = policy.decision
                        request.decision_reason = f"Auto-denied by policy: {policy.description or policy.id}"
                        request.decided_at = datetime.now()
                        logger.info("Auto-denied request %s by policy %s", request.id, policy.id)
                    
                    await self._persist_request(request)
                    await self._emit_event(ApprovalEvent(
                        request_id=request.id,
                        event_type="auto_decided",
                        decision=request.decision,
                    ))
                    return request
            
            # No matching policy - add to pending
            self._pending[request.id] = request
            self._waiters[request.id] = asyncio.Event()
            
            await self._persist_request(request)
            await self._emit_event(ApprovalEvent(
                request_id=request.id,
                event_type="created",
            ))
        
        logger.info("Created approval request %s: %s", request.id, type.value)
        return request
    
    async def wait_for_decision(
        self,
        request_id: str,
        timeout: Optional[float] = None,
    ) -> ApprovalRequest:
        """Wait for approval request decision.

        Args:
            request_id: Request ID
            timeout: Maximum wait time (None = use request expiration time)

        Returns:
            Request object with decision

        Raises:
            asyncio.TimeoutError: Timeout exception
            KeyError: Request not found
        """
        if request_id not in self._pending:
            # Check if already decided
            request = await self.get_request(request_id)
            if request:
                return request
            raise KeyError(f"Request not found: {request_id}")
        
        request = self._pending[request_id]
        waiter = self._waiters.get(request_id)
        
        if not waiter:
            return request
        
        # Calculate timeout
        if timeout is None:
            remaining = (request.expires_at - datetime.now()).total_seconds()
            timeout = max(1, remaining)
        
        try:
            await asyncio.wait_for(waiter.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            # Mark as expired
            request.status = ApprovalStatus.EXPIRED
            request.decision = ApprovalDecision.EXPIRED
            await self._persist_request(request)
            await self._cleanup_request(request_id)
            raise
        
        return self._pending.get(request_id) or await self.get_request(request_id)
    
    async def resolve(
        self,
        request_id: str,
        decision: ApprovalDecision,
        reason: Optional[str] = None,
        decided_by: Optional[str] = None,
    ) -> Optional[ApprovalRequest]:
        """Make a decision on an approval request.

        Args:
            request_id: Request ID
            decision: Decision result
            reason: Decision reason
            decided_by: Decision maker

        Returns:
            Updated request object, None if not found
        """
        async with self._lock:
            request = self._pending.get(request_id)
            if not request:
                logger.warning("Request not found for resolution: %s", request_id)
                return None
            
            # Update request
            if decision in (ApprovalDecision.ALLOW_ONCE, ApprovalDecision.ALLOW_ALWAYS):
                request.status = ApprovalStatus.APPROVED
            else:
                request.status = ApprovalStatus.DENIED
            
            request.decision = decision
            request.decision_reason = reason
            request.decided_by = decided_by
            request.decided_at = datetime.now()
            
            await self._persist_request(request)
            
            # If allow_always or deny_always, create policy
            if decision in (ApprovalDecision.ALLOW_ALWAYS, ApprovalDecision.DENY_ALWAYS):
                policy = ApprovalPolicy(
                    pattern=request.get_pattern(),
                    type=request.type,
                    decision=decision,
                    session_id=request.session_id,
                    created_by=decided_by,
                    description=f"Created from request {request_id}",
                )
                await self.add_policy(policy)
            
            # Notify waiters
            waiter = self._waiters.get(request_id)
            if waiter:
                waiter.set()
            
            await self._emit_event(ApprovalEvent(
                request_id=request_id,
                event_type="resolved",
                decision=decision,
            ))
        
        logger.info("Resolved request %s: %s", request_id, decision.value)
        return request
    
    async def add_policy(self, policy: ApprovalPolicy) -> None:
        """Add an approval policy.

        Args:
            policy: Policy object to add
        """
        self._policies.append(policy)
        
        if self._db:
            await self._db.execute(
                """
                INSERT OR REPLACE INTO policies 
                (id, pattern, type, decision, session_id, user_id, enabled, created_at, expires_at, description)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    policy.id, policy.pattern,
                    policy.type.value if policy.type else None,
                    policy.decision.value,
                    policy.session_id, policy.user_id,
                    1 if policy.enabled else 0,
                    policy.created_at.isoformat(),
                    policy.expires_at.isoformat() if policy.expires_at else None,
                    policy.description,
                )
            )
            await self._db.commit()
        
        logger.info("Added approval policy: %s", policy.id)
    
    async def remove_policy(self, policy_id: str) -> bool:
        """Delete an approval policy.

        Args:
            policy_id: Policy ID

        Returns:
            True if deleted successfully, False otherwise
        """
        for i, policy in enumerate(self._policies):
            if policy.id == policy_id:
                self._policies.pop(i)
                break
        else:
            return False
        
        if self._db:
            await self._db.execute(
                "DELETE FROM policies WHERE id = ?", (policy_id,)
            )
            await self._db.commit()
        
        return True
    
    async def get_request(self, request_id: str) -> Optional[ApprovalRequest]:
        """Get an approval request.

        Args:
            request_id: Request ID

        Returns:
            Request object, None if not found
        """
        if request_id in self._pending:
            return self._pending[request_id]
        
        if not self._db:
            return None
        
        async with self._db.execute(
            "SELECT * FROM requests WHERE id = ?", (request_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return self._row_to_request(row)
        
        return None
    
    def _row_to_request(self, row: tuple) -> ApprovalRequest:
        """Convert a database row to ApprovalRequest."""
        return ApprovalRequest(
            id=row[0],
            type=ApprovalType(row[1]),
            command=row[2],
            args=json.loads(row[3]) if row[3] else {},
            session_id=row[4],
            user_id=row[5],
            status=ApprovalStatus(row[6]),
            decision=ApprovalDecision(row[7]) if row[7] else None,
            risk_level=row[8] or "medium",
            created_at=datetime.fromisoformat(row[9]),
            expires_at=datetime.fromisoformat(row[10]),
            decided_at=datetime.fromisoformat(row[11]) if row[11] else None,
            metadata=json.loads(row[12]) if row[12] else {},
        )
    
    async def _persist_request(self, request: ApprovalRequest) -> None:
        """Persist a request to database."""
        if not self._db:
            return
        
        await self._db.execute(
            """
            INSERT OR REPLACE INTO requests 
            (id, type, command, args, session_id, user_id, status, decision, 
             risk_level, created_at, expires_at, decided_at, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                request.id, request.type.value, request.command,
                json.dumps(request.args), request.session_id, request.user_id,
                request.status.value,
                request.decision.value if request.decision else None,
                request.risk_level,
                request.created_at.isoformat(), request.expires_at.isoformat(),
                request.decided_at.isoformat() if request.decided_at else None,
                json.dumps(request.metadata),
            )
        )
        await self._db.commit()
    
    async def _cleanup_request(self, request_id: str) -> None:
        """Clean up a resolved request."""
        if request_id in self._pending:
            del self._pending[request_id]
        if request_id in self._waiters:
            del self._waiters[request_id]
    
    async def _emit_event(self, event: ApprovalEvent) -> None:
        """Emit an event to all handlers."""
        for handler in self._event_handlers:
            try:
                await handler(event)
            except Exception as e:
                logger.error("Event handler error: %s", e, exc_info=True)
    
    def add_event_handler(self, handler: ApprovalEventHandler) -> None:
        """Add an approval event handler.

        Args:
            handler: Event handler callback
        """
        self._event_handlers.append(handler)
    
    def remove_event_handler(self, handler: ApprovalEventHandler) -> None:
        """Remove an approval event handler.

        Args:
            handler: Event handler to remove
        """
        if handler in self._event_handlers:
            self._event_handlers.remove(handler)
    
    async def get_pending(
        self,
        session_id: Optional[str] = None,
    ) -> List[ApprovalRequest]:
        """Get all pending approval requests.

        Args:
            session_id: Optional, filter by session

        Returns:
            List of pending requests
        """
        requests = list(self._pending.values())
        
        if session_id:
            requests = [r for r in requests if r.session_id == session_id]
        
        return requests
    
    def list_policies(self) -> List[ApprovalPolicy]:
        """List all valid approval policies.

        Returns:
            List of policies
        """
        return [p for p in self._policies if p.is_valid()]
    
    async def cleanup_expired(self) -> int:
        """Clean up expired approval requests.

        Returns:
            Number of cleaned requests
        """
        count = 0
        now = datetime.now()
        
        async with self._lock:
            expired_ids = [
                rid for rid, req in self._pending.items()
                if req.expires_at <= now
            ]
            
            for request_id in expired_ids:
                request = self._pending[request_id]
                request.status = ApprovalStatus.EXPIRED
                request.decision = ApprovalDecision.EXPIRED
                await self._persist_request(request)
                await self._cleanup_request(request_id)
                count += 1
        
        if count > 0:
            logger.info("Cleaned up %s expired approval requests", count)
        
        return count
    
    async def close(self) -> None:
        """Close the approval manager and release resources."""
        if self._db:
            await self._db.close()
            self._db = None


# Global instance
_manager_instance: Optional[ApprovalManager] = None


async def get_approval_manager() -> ApprovalManager:
    """Get the global approval manager instance.

    Returns:
        Approval manager instance
    """
    global _manager_instance
    if _manager_instance is None:
        _manager_instance = ApprovalManager()
        await _manager_instance.initialize()
    return _manager_instance


# Convenience functions for common approval checks

async def require_approval(
    type: ApprovalType,
    command: Optional[str] = None,
    args: Optional[Dict[str, Any]] = None,
    session_id: Optional[str] = None,
    timeout: float = 300,
) -> bool:
    """Request approval and wait for decision.

    Args:
        type: Operation type
        command: Command to execute
        args: Operation parameters
        session_id: Related session
        timeout: Timeout in seconds

    Returns:
        True if approved, False otherwise
    """
    manager = await get_approval_manager()
    
    request = await manager.request_approval(
        type=type,
        command=command,
        args=args,
        session_id=session_id,
        timeout_seconds=int(timeout),
    )
    
    # If already decided (by policy)
    if request.status != ApprovalStatus.PENDING:
        return request.status == ApprovalStatus.APPROVED
    
    try:
        request = await manager.wait_for_decision(request.id, timeout=timeout)
        return request.status == ApprovalStatus.APPROVED
    except asyncio.TimeoutError:
        return False
