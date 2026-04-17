"""
Extended API routes - New feature modules.

Includes:
- Task flow orchestration
- Execution approval
- Cost tracking
- Hook system
"""

from fastapi import APIRouter, HTTPException, Query


def create_tasks_router() -> APIRouter:
    """Create task flow orchestration router."""
    from pyclaw.tasks.registry import get_task_registry
    from pyclaw.tasks.models import TaskCreate, TaskUpdate, TaskQuery, TaskStatus

    router = APIRouter(prefix="/api/tasks", tags=["tasks"])

    @router.get("/")
    async def list_tasks(
        status: str = Query(default=None, description="Status filter"),
        limit: int = Query(default=50, ge=1, le=200),
    ):
        """List tasks."""
        registry = await get_task_registry()
        query = TaskQuery(limit=limit)
        if status:
            query.status = [TaskStatus(status)]
        tasks = await registry.list_tasks(query)
        return {"tasks": [t.model_dump() for t in tasks]}

    @router.get("/{task_id}")
    async def get_task(task_id: str):
        """Get task details."""
        registry = await get_task_registry()
        task = await registry.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        return task.model_dump()

    @router.post("/")
    async def create_task(task_data: TaskCreate):
        """Create task."""
        registry = await get_task_registry()
        task = await registry.create_task(task_data)
        return task.model_dump()

    @router.patch("/{task_id}")
    async def update_task(task_id: str, update: TaskUpdate):
        """Update task."""
        registry = await get_task_registry()
        task = await registry.update_task(task_id, update)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        return task.model_dump()

    @router.delete("/{task_id}")
    async def delete_task(task_id: str):
        """Delete task."""
        registry = await get_task_registry()
        if not await registry.delete_task(task_id):
            raise HTTPException(status_code=404, detail="Task not found")
        return {"status": "deleted", "task_id": task_id}

    @router.get("/{task_id}/events")
    async def get_task_events(task_id: str, limit: int = 100):
        """Get task events."""
        registry = await get_task_registry()
        events = await registry.get_events(task_id, limit=limit)
        return {"events": [e.model_dump() for e in events]}

    @router.get("/stats/summary")
    async def get_task_stats():
        """Get task statistics."""
        registry = await get_task_registry()
        return await registry.get_stats()

    return router


def create_approvals_router() -> APIRouter:
    """Create execution approval router."""
    from pyclaw.infra.approvals import get_approval_manager
    from pyclaw.infra.approval_types import ApprovalDecision, ApprovalPolicy

    router = APIRouter(prefix="/api/approvals", tags=["approvals"])

    @router.get("/pending")
    async def list_pending(
        session_id: str = Query(default=None, description="Session ID filter"),
    ):
        """Get pending approval requests."""
        manager = await get_approval_manager()
        pending = await manager.get_pending(session_id=session_id)
        return {"pending": [r.model_dump() for r in pending]}

    @router.get("/{request_id}")
    async def get_request(request_id: str):
        """Get approval request details."""
        manager = await get_approval_manager()
        request = await manager.get_request(request_id)
        if not request:
            raise HTTPException(status_code=404, detail="Request not found")
        return request.model_dump()

    @router.post("/{request_id}/resolve")
    async def resolve_request(
        request_id: str,
        decision: str = Query(..., description="Decision: allow_once, allow_always, deny, deny_always"),
        reason: str = Query(default=None, description="Decision reason"),
    ):
        """Resolve approval request."""
        manager = await get_approval_manager()
        try:
            decision_enum = ApprovalDecision(decision)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid decision: {decision}")
        
        request = await manager.resolve(request_id, decision_enum, reason=reason)
        if not request:
            raise HTTPException(status_code=404, detail="Request not found")
        return request.model_dump()

    @router.get("/policies/list")
    async def list_policies():
        """Get all approval policies."""
        manager = await get_approval_manager()
        policies = manager.list_policies()
        return {"policies": [p.model_dump() for p in policies]}

    @router.post("/policies")
    async def add_policy(
        pattern: str = Query(..., description="Match pattern"),
        decision: str = Query(..., description="Decision: allow_always, deny_always"),
        description: str = Query(default=None),
    ):
        """Add approval policy."""
        manager = await get_approval_manager()
        try:
            decision_enum = ApprovalDecision(decision)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid decision: {decision}")
        
        policy = ApprovalPolicy(
            pattern=pattern,
            decision=decision_enum,
            description=description,
        )
        await manager.add_policy(policy)
        return policy.model_dump()

    @router.delete("/policies/{policy_id}")
    async def remove_policy(policy_id: str):
        """Remove approval policy."""
        manager = await get_approval_manager()
        if not await manager.remove_policy(policy_id):
            raise HTTPException(status_code=404, detail="Policy not found")
        return {"status": "deleted", "policy_id": policy_id}

    return router


def create_costs_router() -> APIRouter:
    """Create cost tracking router."""
    from pyclaw.sessions.cost_tracker import get_cost_tracker

    router = APIRouter(prefix="/api/costs", tags=["costs"])

    @router.get("/sessions/{session_id}")
    async def get_session_cost(session_id: str):
        """Get session cost."""
        tracker = await get_cost_tracker()
        cost = await tracker.get_session_cost(session_id)
        if not cost:
            return {"session_id": session_id, "total_cost_usd": 0}
        return cost.model_dump()

    @router.get("/daily")
    async def get_daily_cost(date: str = Query(default=None, description="Date YYYY-MM-DD")):
        """Get daily cost."""
        from datetime import date as date_type
        tracker = await get_cost_tracker()
        
        target_date = None
        if date:
            target_date = date_type.fromisoformat(date)
        
        stats = await tracker.get_daily_stats(target_date)
        return stats

    @router.get("/history")
    async def get_cost_history(days: int = Query(default=30, ge=1, le=365)):
        """Get cost history."""
        tracker = await get_cost_tracker()
        history = await tracker.get_cost_history(days=days)
        return {"history": history}

    @router.get("/top-sessions")
    async def get_top_sessions(
        limit: int = Query(default=10, ge=1, le=100),
        days: int = Query(default=7, ge=1, le=30),
    ):
        """Get highest cost sessions."""
        tracker = await get_cost_tracker()
        sessions = await tracker.get_top_sessions(limit=limit, days=days)
        return {"sessions": sessions}

    return router


def create_hooks_router() -> APIRouter:
    """Create hook system router."""
    from pyclaw.hooks.handlers import get_hook_registry
    from pyclaw.hooks.events import HookEvent

    router = APIRouter(prefix="/api/hooks", tags=["hooks"])

    @router.get("/handlers")
    async def list_handlers(
        event: str = Query(default=None, description="Event filter"),
    ):
        """List registered hook handlers."""
        registry = get_hook_registry()
        
        event_filter = None
        if event:
            try:
                event_filter = HookEvent(event)
            except ValueError:
                pass
        
        handlers = registry.list_handlers(event=event_filter)
        return {"handlers": handlers}

    @router.get("/stats")
    async def get_hooks_stats():
        """Get hook statistics."""
        registry = get_hook_registry()
        return registry.get_stats()

    @router.post("/handlers/{name}/enable")
    async def enable_handler(name: str):
        """Enable hook handler."""
        registry = get_hook_registry()
        if not registry.enable(name):
            raise HTTPException(status_code=404, detail="Handler not found")
        return {"status": "enabled", "name": name}

    @router.post("/handlers/{name}/disable")
    async def disable_handler(name: str):
        """Disable hook handler."""
        registry = get_hook_registry()
        if not registry.disable(name):
            raise HTTPException(status_code=404, detail="Handler not found")
        return {"status": "disabled", "name": name}

    @router.get("/events")
    async def list_events():
        """List all available events."""
        return {"events": [e.value for e in HookEvent]}

    return router


def create_context_cache_router() -> APIRouter:
    """Create context cache router."""
    from pyclaw.agents.context_cache import get_context_cache

    router = APIRouter(prefix="/api/cache", tags=["cache"])

    @router.get("/stats")
    async def get_cache_stats():
        """Get cache statistics."""
        cache = get_context_cache()
        return cache.get_stats()

    @router.post("/clear")
    async def clear_cache():
        """Clear cache."""
        cache = get_context_cache()
        count = cache.clear()
        return {"cleared": count}

    @router.post("/cleanup")
    async def cleanup_expired():
        """Clean up expired cache."""
        cache = get_context_cache()
        count = cache.cleanup_expired()
        return {"cleaned": count}

    @router.delete("/sessions/{session_id}")
    async def invalidate_session(session_id: str):
        """Invalidate session cache."""
        cache = get_context_cache()
        count = cache.invalidate_session(session_id)
        return {"invalidated": count, "session_id": session_id}

    return router


def create_failover_router() -> APIRouter:
    """Create failover router."""
    from pyclaw.agents.auth_profiles import get_auth_profile_manager

    router = APIRouter(prefix="/api/failover", tags=["failover"])

    @router.get("/profiles")
    async def list_profiles(provider: str = Query(default=None)):
        """List authentication profiles."""
        manager = get_auth_profile_manager()
        profiles = manager.list_profiles(provider=provider)
        # Hide API key
        return {
            "profiles": [
                {
                    "id": p.id,
                    "provider": p.provider,
                    "name": p.name,
                    "priority": p.priority,
                    "is_available": p.is_available(),
                    "failure_count": p.failure_count,
                    "total_requests": p.total_requests,
                }
                for p in profiles
            ]
        }

    @router.get("/status")
    async def get_cooldown_status():
        """Get cooldown status."""
        manager = get_auth_profile_manager()
        return manager.get_cooldown_status()

    @router.post("/reset")
    async def reset_cooldowns():
        """Reset all cooldowns."""
        manager = get_auth_profile_manager()
        manager.reset_all_cooldowns()
        return {"status": "reset"}

    return router
