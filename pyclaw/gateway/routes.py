"""API routes."""

from pathlib import Path

from fastapi import APIRouter, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from pyclaw.config.schema import PyClawConfig
from pyclaw.constants import (
    SKILL_LIST_DEFAULT_LIMIT,
    SKILL_SEARCH_DEFAULT_LIMIT,
    SKILL_SEARCH_MAX_LIMIT,
)
from pyclaw.logging.redact import redact_sensitive_data
from pyclaw.sessions.manager import SessionManager
from pyclaw.skills.marketplace import SkillMarketplaceClient, SkillMarketplaceConfig
from pyclaw.skills.installer import SkillInstaller
from pyclaw.skills.skillhub import (
    SkillhubClient,
    is_skillhub_available,
    skillhub_search,
    skillhub_install,
)


def create_health_router() -> APIRouter:
    """Create health check router.

    Returns:
        Router
    """
    router = APIRouter(prefix="/api/health", tags=["health"])

    @router.get("/")
    async def health_check():
        """Health check endpoint."""
        return {"status": "healthy"}

    return router


def create_config_router(config: PyClawConfig, app: FastAPI) -> APIRouter:
    """Create config router.

    Args:
        config: Configuration object
        app: FastAPI application instance

    Returns:
        Router
    """
    router = APIRouter(prefix="/api/config", tags=["config"])

    @router.get("/")
    async def get_config():
        """Get configuration (redacted)."""
        config_dict = config.model_dump()
        return redact_sensitive_data(config_dict)

    @router.post("/")
    async def update_config(new_config: dict):
        """Update configuration.

        Args:
            new_config: New configuration

        Returns:
            Updated configuration
        """
        # Configuration update logic should be implemented here
        # Simplified example: return current configuration
        config_dict = config.model_dump()
        return redact_sensitive_data(config_dict)

    @router.get("/reload")
    async def reload_config():
        """Manually trigger configuration reload.

        Returns:
            Reloaded configuration (redacted)
        """
        if not hasattr(app.state, 'config_watcher'):
            raise HTTPException(status_code=503, detail="Config watcher not available")

        watcher = app.state.config_watcher
        new_config = await watcher.reload_now()

        if new_config is None:
            raise HTTPException(status_code=500, detail="Configuration reload failed")

        config_dict = new_config.model_dump()
        return redact_sensitive_data(config_dict)

    @router.get("/providers")
    async def get_providers():
        """Get available LLM providers and models."""
        providers = []
        models_list = []

        for provider_name, provider_entry in config.llm.providers.items():
            providers.append({"value": provider_name, "label": provider_name.upper()})
            for model_entry in provider_entry.models:
                models_list.append({
                    "id": model_entry.id,
                    "name": model_entry.id,
                    "provider": provider_name,
                })

        if not providers:
            providers.append({"value": "local", "label": "LOCAL"})

        return {"providers": providers, "models": models_list}

    return router


def create_sessions_router(session_manager: SessionManager) -> APIRouter:
    """Create sessions router.

    Args:
        session_manager: Session manager

    Returns:
        Router
    """
    router = APIRouter(prefix="/api/sessions", tags=["sessions"])

    @router.get("/")
    async def list_sessions():
        """List all active sessions."""
        sessions = await session_manager.list_active_sessions()
        return {"sessions": [s.model_dump() for s in sessions]}

    @router.get("/{session_id}")
    async def get_session(session_id: str):
        """Get specific session.

        Args:
            session_id: Session ID

        Returns:
            Session information
        """
        session = await session_manager.get_session(session_id)
        return session.model_dump()

    @router.delete("/{session_id}")
    async def delete_session(session_id: str):
        """Delete session.

        Args:
            session_id: Session ID

        Returns:
            Deletion result
        """
        await session_manager.close_session(session_id)
        return {"message": "Session deleted"}

    @router.get("/{session_id}/export/markdown")
    async def export_session_markdown(session_id: str):
        """Export session as Markdown format.

        Args:
            session_id: Session ID

        Returns:
            Markdown formatted session content
        """
        from pyclaw.sessions.export import export_session_markdown

        try:
            markdown_content = await export_session_markdown(
                session_id, session_manager.store
            )
            return {
                "session_id": session_id,
                "format": "markdown",
                "content": markdown_content,
            }
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @router.get("/{session_id}/export/json")
    async def export_session_json(session_id: str):
        """Export session as JSON format.

        Args:
            session_id: Session ID

        Returns:
            JSON formatted session data
        """
        from pyclaw.sessions.export import export_session_json

        try:
            json_content = await export_session_json(
                session_id, session_manager.store
            )
            return {
                "session_id": session_id,
                "format": "json",
                "data": json_content,
            }
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    return router


def create_channels_router() -> APIRouter:
    """Create channels router.

    Returns:
        Router
    """
    import json as _json

    router = APIRouter(prefix="/api/channels", tags=["channels"])

    @router.get("/")
    @router.get("/status")
    async def list_channels():
        """Get connection status of all channels."""
        from pyclaw.config.paths import get_paths as _get_paths

        _paths = _get_paths()
        channels = []

        # Check DingTalk
        dingtalk_has_sessions = _paths.dingtalk_sessions_file.exists()
        webhooks_exist = _paths.dingtalk_webhooks_file.exists()

        webhook_count = 0
        if webhooks_exist:
            try:
                webhooks = _json.loads(_paths.dingtalk_webhooks_file.read_text())
                webhook_count = len(webhooks)
            except Exception:
                pass

        dingtalk_connected = dingtalk_has_sessions and webhook_count > 0
        channels.append({
            "id": "dingtalk",
            "name": "DingTalk",
            "icon": "🔔",
            "connected": dingtalk_connected,
            "user_count": webhook_count,
            "status": "connected" if dingtalk_connected else "disconnected",
        })

        # Check WeChat Personal
        wechat_enabled = False
        try:
            from pyclaw.config.loader import load_config as _load_wechat_cfg
            wechat_enabled = _load_wechat_cfg().channels.wechat_personal_connector.enabled
        except Exception:
            pass

        if not wechat_enabled:
            channels.append({
                "id": "wechat_personal",
                "name": "WeChat",
                "icon": "💚",
                "connected": False,
                "user_count": 0,
                "status": "disabled",
                "enabled": False,
            })
        else:
            try:
                from pyclaw.channels.wechat_personal.client import WeChatPersonalClient
                wechat_client = WeChatPersonalClient.get_instance()
                wechat_status = wechat_client.get_status()
                wechat_connected = wechat_status.get("logged_in", False)
                channels.append({
                    "id": "wechat_personal",
                    "name": "WeChat",
                    "icon": "💚",
                    "connected": wechat_connected,
                    "user_count": wechat_status.get("session_count", 0),
                    "status": "connected" if wechat_connected else "ready",
                    "enabled": True,
                    "nickname": wechat_status.get("nickname", ""),
                    "has_qrcode": wechat_status.get("has_qrcode", False),
                })
            except ImportError:
                channels.append({
                    "id": "wechat_personal",
                    "name": "WeChat",
                    "icon": "💚",
                    "connected": False,
                    "user_count": 0,
                    "status": "not_installed",
                    "enabled": True,
                })

        # Reserve other channels
        for channel_id, name, icon in [
            ("feishu", "Feishu", "🐦"),
            ("slack", "Slack", "💜"),
            ("telegram", "Telegram", "✈️"),
        ]:
            channels.append({
                "id": channel_id,
                "name": name,
                "icon": icon,
                "connected": False,
                "user_count": 0,
                "status": "not_configured",
            })

        return {"channels": channels}

    @router.get("/dingtalk/sessions")
    async def get_dingtalk_sessions():
        """Get DingTalk message history."""
        from pyclaw.config.paths import get_paths as _get_paths

        sessions_file = _get_paths().dingtalk_sessions_file
        if not sessions_file.exists():
            return {"sessions": [], "status": "no_data"}

        try:
            raw = _json.loads(sessions_file.read_text())
            result = []
            for user_id, messages in raw.items():
                result.append({
                    "user_id": user_id,
                    "messages": messages[-50:],
                    "message_count": len(messages),
                })
            return {"sessions": result, "status": "ok"}
        except Exception as exc:
            return {"sessions": [], "status": "error", "error": str(exc)}

    # ── WeChat Personal APIs ──────────────────────────────────────────

    @router.post("/wechat_personal/login")
    async def wechat_personal_login():
        """Start WeChat login process, return QR code base64"""
        try:
            from pyclaw.config.loader import load_config as _load_cfg
            _cfg = _load_cfg()
            if not _cfg.channels.wechat_personal_connector.enabled:
                return {
                    "status": "disabled",
                    "qrcode": None,
                    "error": "WeChat Personal channel is disabled. Enable it in pyclaw.json first.",
                }

            from pyclaw.channels.wechat_personal.client import WeChatPersonalClient
            client = WeChatPersonalClient.get_instance()

            if client.is_logged_in:
                return {
                    "status": "already_logged_in",
                    "nickname": client.nickname,
                    "qrcode": None,
                }

            qr_base64 = client.start_login()
            if qr_base64:
                return {
                    "status": "qrcode_ready",
                    "qrcode": qr_base64,
                    "nickname": None,
                }
            # Login thread may have failed, check thread status
            login_thread = getattr(client, "_login_thread", None)
            if login_thread and not login_thread.is_alive() and not client.is_logged_in:
                return {
                    "status": "error",
                    "qrcode": None,
                    "nickname": None,
                    "error": "WeChat login failed. Check network/SSL or try: pip install certifi",
                }
            return {
                "status": "waiting",
                "qrcode": None,
                "nickname": None,
            }
        except ImportError:
            return {
                "status": "error",
                "qrcode": None,
                "error": "itchat-uos not installed. Run: pip install itchat-uos",
            }
        except Exception as exc:
            return {
                "status": "error",
                "qrcode": None,
                "error": str(exc),
            }

    @router.get("/wechat_personal/status")
    async def wechat_personal_status():
        """Get WeChat login status"""
        try:
            from pyclaw.channels.wechat_personal.client import WeChatPersonalClient
            client = WeChatPersonalClient.get_instance()
            status = client.get_status()
            return {"status": "ok", **status}
        except ImportError:
            return {"status": "not_installed", "logged_in": False}
        except Exception as exc:
            return {"status": "error", "logged_in": False, "error": str(exc)}

    @router.post("/wechat_personal/logout")
    async def wechat_personal_logout():
        """WeChat logout"""
        try:
            from pyclaw.channels.wechat_personal.client import WeChatPersonalClient
            client = WeChatPersonalClient.get_instance()
            client.logout()
            return {"status": "ok"}
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @router.get("/wechat_personal/sessions")
    async def wechat_personal_sessions():
        """Get WeChat session list"""
        try:
            from pyclaw.channels.wechat_personal.client import WeChatPersonalClient
            client = WeChatPersonalClient.get_instance()
            sessions = client.get_sessions_summary()
            return {"sessions": sessions, "status": "ok"}
        except ImportError:
            return {"sessions": [], "status": "not_installed"}
        except Exception as exc:
            return {"sessions": [], "status": "error", "error": str(exc)}

    return router


# ---------------------------------------------------------------------------
# Skills Marketplace Routes
# ---------------------------------------------------------------------------

class SkillSearchResponse(BaseModel):
    """Skill search response"""
    score: float
    slug: str
    display_name: str
    summary: str | None = None
    version: str | None = None
    updated_at: int | None = None


class SkillDetailResponse(BaseModel):
    """Skill detail response"""
    slug: str
    display_name: str
    summary: str | None = None
    tags: dict[str, str] = Field(default_factory=dict)
    created_at: int
    updated_at: int
    latest_version: str | None = None
    owner: dict | None = None
    metadata: dict | None = None


class SkillInstallRequest(BaseModel):
    """Skill install request"""
    slug: str = Field(..., description="Skill identifier")
    version: str | None = Field(default=None, description="Specify version, default latest")
    force: bool = Field(default=False, description="Whether to force reinstall")


class SkillInstallResponse(BaseModel):
    """Skill install response"""
    ok: bool
    slug: str | None = None
    version: str | None = None
    target_dir: str | None = None
    message: str = ""
    error: str | None = None


class SkillUpdateRequest(BaseModel):
    """Skill update request"""
    slug: str | None = Field(default=None, description="Specify skill, empty means update all")
    all_skills: bool = Field(default=False, description="Whether to update all installed skills")


class SkillUninstallRequest(BaseModel):
    """Skill uninstall request"""
    slug: str = Field(..., description="Skill identifier")


class InstalledSkillInfo(BaseModel):
    """Installed skill info"""
    slug: str
    version: str
    installed_at: int
    registry: str | None = None
    name: str | None = None
    description: str | None = None


class SkillsStatusResponse(BaseModel):
    """Skill status response"""
    installed: list[InstalledSkillInfo]
    marketplace_url: str


def _get_workspace_dir() -> Path:
    """Get workspace directory"""
    from pyclaw.config.paths import get_paths as _get_paths
    return _get_paths().workspace_dir


def create_skills_router(config: PyClawConfig) -> APIRouter:
    """Create skills marketplace router.

    Args:
        config: Configuration object

    Returns:
        Router
    """
    router = APIRouter(prefix="/api/skills", tags=["skills"])

    # Read marketplace URL from config or use default
    marketplace_url = "https://clawhub.ai"

    # Prefer Skillhub CLI (domestic acceleration)
    _use_skillhub = is_skillhub_available()

    @router.get("/marketplace/search", response_model=list[SkillSearchResponse])
    async def search_skills(
        query: str = Query(default="*", description="Search keyword"),
        limit: int = Query(default=SKILL_SEARCH_DEFAULT_LIMIT, ge=1, le=SKILL_SEARCH_MAX_LIMIT, description="Result count limit"),
    ):
        """Search skills in marketplace. Prefer Skillhub (domestic acceleration)."""
        # Try Skillhub CLI first
        if _use_skillhub:
            try:
                results = await skillhub_search(query)
                return [
                    SkillSearchResponse(
                        score=0.0,
                        slug=r.slug,
                        display_name=r.display_name,
                        summary=r.summary,
                        version=r.version,
                        updated_at=None,
                    )
                    for r in results[:limit]
                ]
            except Exception:
                pass  # Skillhub failed, fallback to ClawHub

        # Fallback to ClawHub API
        marketplace_config = SkillMarketplaceConfig(base_url=marketplace_url)

        try:
            async with SkillMarketplaceClient(marketplace_config) as client:
                results = await client.search_skills(query=query, limit=limit)
        except Exception as e:
            raise HTTPException(status_code=503, detail=str(e))

        return [
            SkillSearchResponse(
                score=r.score,
                slug=r.slug,
                display_name=r.display_name,
                summary=r.summary,
                version=r.version,
                updated_at=r.updated_at,
            )
            for r in results
        ]

    @router.get("/marketplace/list", response_model=list[SkillSearchResponse])
    async def list_marketplace_skills(
        limit: int = Query(default=SKILL_LIST_DEFAULT_LIMIT, ge=1, le=SKILL_SEARCH_MAX_LIMIT, description="Result count limit"),
    ):
        """List available skills in marketplace. Prefer Skillhub."""
        # Try Skillhub CLI first
        if _use_skillhub:
            try:
                results = await skillhub_search("*")
                return [
                    SkillSearchResponse(
                        score=0.0,
                        slug=r.slug,
                        display_name=r.display_name,
                        summary=r.summary,
                        version=r.version,
                        updated_at=None,
                    )
                    for r in results[:limit]
                ]
            except Exception:
                pass  # Skillhub failed, fallback to ClawHub

        # Fallback to ClawHub API
        marketplace_config = SkillMarketplaceConfig(base_url=marketplace_url)

        try:
            async with SkillMarketplaceClient(marketplace_config) as client:
                results = await client.list_skills(limit=limit)
        except Exception as e:
            raise HTTPException(status_code=503, detail=str(e))

        return [
            SkillSearchResponse(
                score=r.score,
                slug=r.slug,
                display_name=r.display_name,
                summary=r.summary,
                version=r.version,
                updated_at=r.updated_at,
            )
            for r in results
        ]

    @router.get("/marketplace/detail/{slug}", response_model=SkillDetailResponse)
    async def get_skill_detail(slug: str):
        """Get skill details."""
        marketplace_config = SkillMarketplaceConfig(base_url=marketplace_url)

        try:
            async with SkillMarketplaceClient(marketplace_config) as client:
                detail = await client.get_skill_detail(slug)
        except Exception as e:
            raise HTTPException(status_code=503, detail=str(e))

        if not detail:
            raise HTTPException(status_code=404, detail=f"Skill '{slug}' not found")

        return SkillDetailResponse(
            slug=detail.slug,
            display_name=detail.display_name,
            summary=detail.summary,
            tags=detail.tags,
            created_at=detail.created_at,
            updated_at=detail.updated_at,
            latest_version=detail.latest_version,
            owner=detail.owner,
            metadata=detail.metadata,
        )

    @router.post("/install", response_model=SkillInstallResponse)
    async def install_skill(request: SkillInstallRequest):
        """Install skill. Prefer Skillhub (domestic acceleration)."""
        workspace_dir = _get_workspace_dir()

        # Try Skillhub CLI first
        if _use_skillhub:
            try:
                result = await skillhub_install(request.slug, workspace_dir)
                if result.ok:
                    return SkillInstallResponse(
                        ok=True,
                        slug=request.slug,
                        version=None,
                        target_dir=str(workspace_dir / "skills" / request.slug),
                        message=result.message,
                        error=None,
                    )
                # Skillhub install failed, fallback to ClawHub
            except Exception:
                pass

        # Fallback to ClawHub API
        marketplace_config = SkillMarketplaceConfig(base_url=marketplace_url)

        async with SkillInstaller(workspace_dir, marketplace_config) as installer:
            result = await installer.install(
                slug=request.slug,
                version=request.version,
                force=request.force,
            )

        return SkillInstallResponse(
            ok=result.ok,
            slug=result.slug,
            version=result.version,
            target_dir=str(result.target_dir) if result.target_dir else None,
            message=result.message,
            error=result.error,
        )

    @router.post("/update", response_model=list[SkillInstallResponse])
    async def update_skills(request: SkillUpdateRequest):
        """Update skill."""
        workspace_dir = _get_workspace_dir()
        marketplace_config = SkillMarketplaceConfig(base_url=marketplace_url)

        async with SkillInstaller(workspace_dir, marketplace_config) as installer:
            results = await installer.update(
                slug=request.slug,
                all_skills=request.all_skills,
            )

        return [
            SkillInstallResponse(
                ok=r.ok,
                slug=r.slug,
                version=r.version,
                target_dir=str(r.target_dir) if r.target_dir else None,
                message=r.message,
                error=r.error,
            )
            for r in results
        ]

    @router.post("/uninstall", response_model=SkillInstallResponse)
    async def uninstall_skill(request: SkillUninstallRequest):
        """Uninstall skill."""
        workspace_dir = _get_workspace_dir()
        marketplace_config = SkillMarketplaceConfig(base_url=marketplace_url)

        async with SkillInstaller(workspace_dir, marketplace_config) as installer:
            result = await installer.uninstall(slug=request.slug)

        return SkillInstallResponse(
            ok=result.ok,
            slug=result.slug,
            version=result.version,
            target_dir=str(result.target_dir) if result.target_dir else None,
            message=result.message,
            error=result.error,
        )

    @router.get("/status", response_model=SkillsStatusResponse)
    async def get_skills_status():
        """Get installed skill status."""
        workspace_dir = _get_workspace_dir()
        marketplace_config = SkillMarketplaceConfig(base_url=marketplace_url)

        async with SkillInstaller(workspace_dir, marketplace_config) as installer:
            installed = installer.list_installed()

        # Show current marketplace in use
        active_marketplace = "skillhub://local" if _use_skillhub else marketplace_url

        return SkillsStatusResponse(
            installed=[
                InstalledSkillInfo(
                    slug=s["slug"],
                    version=s["version"],
                    installed_at=s["installed_at"],
                    registry=s.get("registry"),
                    name=s.get("name"),
                    description=s.get("description"),
                )
                for s in installed
            ],
            marketplace_url=active_marketplace,
        )

    return router


def create_cron_router() -> APIRouter:
    """Create scheduled task router.

    Returns:
        Router
    """
    from pyclaw.cron.scheduler import (
        CronScheduler,
        CronJobCreate,
        CronJobUpdate,
        CronJobInfo,
        get_scheduler,
    )

    router = APIRouter(prefix="/api/cron", tags=["cron"])

    @router.get("/jobs", response_model=list[CronJobInfo])
    async def list_jobs():
        """Get all scheduled tasks."""
        scheduler = get_scheduler()
        return scheduler.list_jobs()

    @router.get("/jobs/{job_id}", response_model=CronJobInfo)
    async def get_job(job_id: str):
        """Get single scheduled task."""
        scheduler = get_scheduler()
        job = scheduler.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        return job

    @router.post("/jobs", response_model=CronJobInfo)
    async def create_job(job_data: CronJobCreate):
        """Create scheduled task.
        
        trigger_type: cron | interval | date
        
        cron examples:
        - Daily 9:00: {"hour": 9, "minute": 0}
        - Weekly Monday 10:30: {"day_of_week": "mon", "hour": 10, "minute": 30}
        - Hourly: {"minute": 0}
        
        interval examples:
        - Every 30 minutes: {"minutes": 30}
        - Every 2 hours: {"hours": 2}
        
        action_type: shell | http | message
        
        shell example: {"command": "echo hello", "timeout": 60}
        http example: {"url": "https://api.example.com", "method": "POST"}
        message example: {"message": "Tired, take a break"}
        """
        try:
            scheduler = get_scheduler()
            return scheduler.add_job(job_data)
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))

    @router.put("/jobs/{job_id}", response_model=CronJobInfo)
    async def update_job(job_id: str, job_data: CronJobUpdate):
        """Update scheduled task."""
        scheduler = get_scheduler()
        job = scheduler.update_job(job_id, job_data)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        return job

    @router.delete("/jobs/{job_id}")
    async def delete_job(job_id: str):
        """Delete scheduled task."""
        scheduler = get_scheduler()
        if not scheduler.delete_job(job_id):
            raise HTTPException(status_code=404, detail="Job not found")
        return {"status": "deleted", "job_id": job_id}

    @router.post("/jobs/{job_id}/pause")
    async def pause_job(job_id: str):
        """Pause scheduled task."""
        scheduler = get_scheduler()
        if not scheduler.pause_job(job_id):
            raise HTTPException(status_code=404, detail="Job not found")
        return {"status": "paused", "job_id": job_id}

    @router.post("/jobs/{job_id}/resume")
    async def resume_job(job_id: str):
        """Resume scheduled task."""
        scheduler = get_scheduler()
        if not scheduler.resume_job(job_id):
            raise HTTPException(status_code=404, detail="Job not found")
        return {"status": "resumed", "job_id": job_id}

    @router.post("/jobs/{job_id}/run")
    async def run_job_now(job_id: str):
        """Execute scheduled task immediately."""
        scheduler = get_scheduler()
        # run_job_now is an async method
        if not await scheduler.run_job_now(job_id):
            raise HTTPException(status_code=404, detail="Job not found")
        return {"status": "triggered", "job_id": job_id}

    @router.get("/jobs/{job_id}/history")
    async def get_job_history(job_id: str):
        """Get scheduled task execution history."""
        from pyclaw.cron.scheduler import get_job_history
        history = get_job_history(job_id)
        return {"job_id": job_id, "history": history}

    return router
