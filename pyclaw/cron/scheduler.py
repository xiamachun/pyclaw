"""APScheduler-based cron scheduler with persistence and CRUD support."""

import logging
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.date import DateTrigger
from pydantic import BaseModel, Field

from pyclaw.constants import CRON_EXECUTOR_MAX_WORKERS, CRON_MISFIRE_GRACE_TIME_SECONDS

logger = logging.getLogger(__name__)


# ==================== Models ====================

class CronJobCreate(BaseModel):
    """Request model for creating scheduled tasks"""
    name: str = Field(..., description="Task name")
    description: str = Field(default="", description="Task description")
    trigger_type: str = Field(..., description="Trigger type: cron, interval, date")
    trigger_args: Dict[str, Any] = Field(..., description="Trigger parameters")
    action_type: str = Field(..., description="Action type: shell, http, message")
    action_args: Dict[str, Any] = Field(..., description="Action parameters")
    enabled: bool = Field(default=True, description="Whether enabled")


class CronJobUpdate(BaseModel):
    """Request model for updating scheduled tasks"""
    name: Optional[str] = None
    description: Optional[str] = None
    trigger_type: Optional[str] = None
    trigger_args: Optional[Dict[str, Any]] = None
    action_type: Optional[str] = None
    action_args: Optional[Dict[str, Any]] = None
    enabled: Optional[bool] = None


class CronJobInfo(BaseModel):
    """Response model for scheduled task information"""
    id: str
    name: str
    description: str
    trigger_type: str
    trigger_args: Dict[str, Any]
    action_type: str
    action_args: Dict[str, Any]
    enabled: bool
    next_run_time: Optional[str] = None
    created_at: Optional[str] = None


# ==================== Job Actions ====================

async def shell_action(command: str, timeout: int = 60, **kwargs) -> str:
    """Execute shell command"""
    import asyncio
    import subprocess
    
    logger.info("[CronJob] Executing shell command: %s...", command[:100])
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        result = f"Exit: {proc.returncode}\nStdout: {stdout.decode()}\nStderr: {stderr.decode()}"
        logger.info("[CronJob] Shell result: %s", result[:200])
        return result
    except asyncio.TimeoutError:
        logger.error("[CronJob] Shell command timed out after %ss", timeout)
        return f"Error: Command timed out after {timeout}s"
    except Exception as e:
        logger.error("[CronJob] Shell error: %s", e, exc_info=True)
        return f"Error: {str(e)}"


async def http_action(url: str, method: str = "GET", headers: dict = None, body: str = None, **kwargs) -> str:
    """Send HTTP request"""
    import httpx
    
    logger.info("[CronJob] HTTP %s %s", method, url)
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.request(method, url, headers=headers, content=body)
            result = f"Status: {resp.status_code}\nBody: {resp.text[:500]}"
            logger.info("[CronJob] HTTP result: %s", result[:200])
            return result
    except Exception as e:
        logger.error("[CronJob] HTTP error: %s", e, exc_info=True)
        return f"Error: {str(e)}"


async def message_action(message: str, channel: str = "log", **kwargs) -> str:
    """Send message (support multiple channels)
    
    Args:
        message: Message content
        channel: Channel (log, dingtalk)
    """
    logger.info("[CronJob] Message to %s: %s", channel, message)
    
    # Always print to log
    logger.info("\n%s", "="*50)
    logger.info("⏰ Scheduled task message [%s]", datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    logger.info("📢 %s", message)
    logger.info("%s\n", "="*50)
    
    # If DingTalk channel is specified, send to DingTalk
    if channel == "dingtalk" or kwargs.get("send_to_dingtalk", False):
        result = await _send_to_dingtalk(message)
        if result:
            return f"Message sent to DingTalk: {message}"
        else:
            return f"Message logged (DingTalk send failed): {message}"
    
    return f"Message logged: {message}"


async def dingtalk_action(message: str, **kwargs) -> str:
    """Send message to DingTalk"""
    logger.info("[CronJob] DingTalk message: %s", message)
    result = await _send_to_dingtalk(message)
    if result:
        return f"DingTalk message sent: {message}"
    else:
        return f"ERROR: DingTalk send failed: {message}"


async def chat_action(
    message: str,
    send_to_dingtalk: bool = True,
    model: str = "default",
    **kwargs,
) -> str:
    """Send message to Gateway AI and forward the response to DingTalk.

    Unlike ``dingtalk_action`` which sends a static text, this action
    triggers a full AI conversation through the Gateway API, allowing
    the LLM to call skills (stock analysis, web search, etc.) and
    produce a dynamic response.

    Args:
        message: The user prompt to send to the AI.
        send_to_dingtalk: Whether to forward the AI response to DingTalk.
        model: Model name to use for the AI conversation.
    """
    import httpx
    from pyclaw.config.loader import load_config
    from pyclaw.constants import DEFAULT_HOST, DEFAULT_PORT

    logger.info("[CronJob] Chat action: %s", message[:100])

    config = load_config()
    token = config.gateway.auth.token.get_secret_value()
    gateway_url = f"http://{DEFAULT_HOST}:{DEFAULT_PORT}/v1/chat/completions"

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": message}],
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=300) as client:
            resp = await client.post(gateway_url, json=payload, headers=headers)

        if resp.status_code != 200:
            error_msg = f"Gateway returned {resp.status_code}: {resp.text[:300]}"
            logger.error("[CronJob] Chat action failed: %s", error_msg)
            return f"ERROR: {error_msg}"

        result = resp.json()
        ai_content = ""
        choices = result.get("choices", [])
        if choices:
            ai_content = choices[0].get("message", {}).get("content", "")

        if not ai_content:
            logger.warning("[CronJob] Chat action returned empty content")
            return "ERROR: AI returned empty response"

        logger.info("[CronJob] Chat action got response: %s chars", len(ai_content))

        if send_to_dingtalk:
            sent = await _send_to_dingtalk(ai_content)
            if sent:
                return f"Chat response sent to DingTalk ({len(ai_content)} chars)"
            else:
                logger.warning("[CronJob] Chat response ready but DingTalk send failed")
                return f"Chat response ready but DingTalk send failed: {ai_content[:200]}"

        return ai_content

    except httpx.TimeoutException:
        logger.error("[CronJob] Chat action timed out (300s)")
        return "ERROR: Chat action timed out after 300s"
    except Exception as e:
        logger.error("[CronJob] Chat action error: %s", e, exc_info=True)
        return f"ERROR: {str(e)}"


async def _send_to_dingtalk(message: str) -> bool:
    """Send message to DingTalk via cached webhooks, with OpenAPI fallback.

    Tries the fast webhook path first.  When a webhook returns
    ``session 不存在`` (errcode 300001) — meaning the session has
    expired — automatically falls back to the DingTalk OpenAPI
    ``oToMessages/batchSend`` endpoint which does not depend on
    session liveness.

    Args:
        message: The message content to send.

    Returns:
        True if at least one user received the message successfully.
    """
    import asyncio
    import httpx
    import json
    from pyclaw.config.paths import get_paths as _get_paths

    webhooks_file = _get_paths().dingtalk_webhooks_file
    CHUNK_LIMIT = 3800  # stay safely below the 4000-char webhook limit

    try:
        if not webhooks_file.exists():
            logger.warning("[CronJob] No webhooks file found")
            return False

        webhooks = json.loads(webhooks_file.read_text())
        if not webhooks:
            logger.warning("[CronJob] No cached webhooks")
            return False

        # Convert single newlines to DingTalk markdown line breaks.
        message = _adapt_markdown_for_dingtalk(message)

        # Split long messages into chunks at paragraph boundaries
        chunks = _split_message(message, CHUNK_LIMIT)
        logger.info(
            "[CronJob] Sending %d chunk(s) to %d webhook(s), total %d chars",
            len(chunks), len(webhooks), len(message),
        )

        success_count = 0
        failed_user_ids: list[str] = []

        for user_id, webhook_url in webhooks.items():
            try:
                user_ok = True
                for idx, chunk in enumerate(chunks):
                    payload = {
                        "msgtype": "markdown",
                        "markdown": {
                            "title": "PyClaw",
                            "text": chunk,
                        },
                    }
                    async with httpx.AsyncClient(timeout=15) as client:
                        resp = await client.post(webhook_url, json=payload)
                        if resp.status_code == 200:
                            result = resp.json()
                            if result.get("errcode") != 0:
                                logger.warning(
                                    "[CronJob] DingTalk webhook error for %s chunk %d: %s",
                                    user_id, idx, result,
                                )
                                user_ok = False
                                break
                        else:
                            logger.warning(
                                "[CronJob] DingTalk HTTP %d for %s chunk %d",
                                resp.status_code, user_id, idx,
                            )
                            user_ok = False
                            break
                    if len(chunks) > 1 and idx < len(chunks) - 1:
                        await asyncio.sleep(1)
                if user_ok:
                    success_count += 1
                    logger.info("[CronJob] Sent to user %s via webhook", user_id)
                else:
                    failed_user_ids.append(user_id)
            except Exception as e:
                logger.error("[CronJob] Failed to send to %s: %s", user_id, e, exc_info=True)
                failed_user_ids.append(user_id)

        # Fallback: retry failed users via OpenAPI
        if failed_user_ids:
            logger.info(
                "[CronJob] Webhook failed for %d user(s), trying OpenAPI fallback",
                len(failed_user_ids),
            )
            for user_id in failed_user_ids:
                openapi_ok = await _send_via_openapi(user_id, chunks)
                if openapi_ok:
                    success_count += 1

        return success_count > 0

    except Exception as e:
        logger.error("[CronJob] DingTalk send error: %s", e, exc_info=True)
        return False


# ── DingTalk OpenAPI helpers ─────────────────────────────────────────────

# Cached access token and its expiry (epoch seconds)
_dingtalk_access_token: str = ""
_dingtalk_token_expires_at: float = 0.0


async def _get_dingtalk_access_token() -> str:
    """Obtain a DingTalk access token, using a cached value when possible.

    The token is fetched from the DingTalk OAuth2 endpoint using the
    ``clientId`` / ``clientSecret`` from ``pyclaw.json``.  It is cached
    in-memory and refreshed 5 minutes before expiry.

    Returns:
        A valid access token string, or empty string on failure.
    """
    import time
    import httpx
    from pyclaw.config.loader import load_config as _load_config

    global _dingtalk_access_token, _dingtalk_token_expires_at

    # Return cached token if still valid (with 5-min safety margin)
    if _dingtalk_access_token and time.time() < _dingtalk_token_expires_at - 300:
        return _dingtalk_access_token

    try:
        config = _load_config()
        dt_config = config.channels.dingtalk_connector
        client_id = dt_config.client_id
        client_secret = dt_config.client_secret.get_secret_value()

        if not client_id or not client_secret:
            logger.warning("[CronJob] DingTalk clientId/clientSecret not configured")
            return ""

        # Force IPv4 — DingTalk IP whitelist does not support IPv6
        transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0")
        async with httpx.AsyncClient(timeout=10, transport=transport) as client:
            resp = await client.post(
                "https://api.dingtalk.com/v1.0/oauth2/accessToken",
                json={"appKey": client_id, "appSecret": client_secret},
            )
            if resp.status_code == 200:
                data = resp.json()
                _dingtalk_access_token = data.get("accessToken", "")
                expire_in = data.get("expireIn", 7200)
                _dingtalk_token_expires_at = time.time() + expire_in
                logger.info("[CronJob] DingTalk access token refreshed, expires in %ds", expire_in)
                return _dingtalk_access_token
            else:
                logger.error("[CronJob] Failed to get DingTalk token: HTTP %d %s", resp.status_code, resp.text)
                return ""
    except Exception as e:
        logger.error("[CronJob] DingTalk token error: %s", e, exc_info=True)
        return ""


async def _send_via_openapi(user_id: str, chunks: list[str]) -> bool:
    """Send markdown message to a single user via DingTalk OpenAPI.

    Uses the ``oToMessages/batchSend`` endpoint which does not depend
    on session webhooks and therefore works even when the user has not
    interacted with the bot recently.

    The message format (markdown title + text) is identical to the
    webhook path, so rendering is unchanged.

    Args:
        user_id: The DingTalk ``staffId`` of the recipient.
        chunks: Pre-split message chunks (already adapted for DingTalk markdown).

    Returns:
        True if all chunks were sent successfully.
    """
    import asyncio
    import json
    import httpx
    from pyclaw.config.loader import load_config as _load_config

    access_token = await _get_dingtalk_access_token()
    if not access_token:
        logger.warning("[CronJob] OpenAPI fallback skipped: no access token")
        return False

    try:
        config = _load_config()
        robot_code = config.channels.dingtalk_connector.client_id
        if not robot_code:
            logger.warning("[CronJob] OpenAPI fallback skipped: no robotCode (clientId)")
            return False

        all_ok = True
        for idx, chunk in enumerate(chunks):
            msg_param = json.dumps({"title": "PyClaw", "text": chunk}, ensure_ascii=False)
            payload = {
                "robotCode": robot_code,
                "userIds": [user_id],
                "msgKey": "sampleMarkdown",
                "msgParam": msg_param,
            }
            # Force IPv4 — DingTalk IP whitelist does not support IPv6
            transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0")
            async with httpx.AsyncClient(timeout=15, transport=transport) as client:
                resp = await client.post(
                    "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend",
                    headers={"x-acs-dingtalk-access-token": access_token},
                    json=payload,
                )
                if resp.status_code == 200:
                    result = resp.json()
                    if "processQueryKey" in result:
                        logger.info("[CronJob] OpenAPI sent to %s chunk %d OK", user_id, idx)
                    else:
                        logger.warning("[CronJob] OpenAPI unexpected response for %s: %s", user_id, result)
                        all_ok = False
                        break
                else:
                    logger.warning(
                        "[CronJob] OpenAPI HTTP %d for %s chunk %d: %s",
                        resp.status_code, user_id, idx, resp.text[:200],
                    )
                    all_ok = False
                    break

            if len(chunks) > 1 and idx < len(chunks) - 1:
                await asyncio.sleep(1)

        if all_ok:
            logger.info("[CronJob] Sent to user %s via OpenAPI", user_id)
        return all_ok

    except Exception as e:
        logger.error("[CronJob] OpenAPI send error for %s: %s", user_id, e, exc_info=True)
        return False


def _adapt_markdown_for_dingtalk(text: str) -> str:
    """Adapt standard Markdown to DingTalk webhook markdown rendering.

    DingTalk markdown ignores single ``\\n`` between lines.  To force a
    visible line break you need either a blank line (paragraph break) or
    two trailing spaces before the newline (hard break).

    This function adds trailing double-spaces to every content line that
    is not already a heading, table row, blank line, or horizontal rule
    so that the rendered output preserves the intended line structure.

    Args:
        text: Standard Markdown text.

    Returns:
        Text adapted for DingTalk markdown rendering.
    """
    output_lines: list[str] = []
    for line in text.split("\n"):
        stripped = line.strip()
        # Skip lines that already handle their own spacing
        is_blank = not stripped
        is_heading = stripped.startswith("#")
        is_table = stripped.startswith("|")
        is_hr = stripped in ("---", "***", "___")
        already_has_break = line.endswith("  ")

        if is_blank or is_heading or is_table or is_hr or already_has_break:
            output_lines.append(line)
        else:
            # Add two trailing spaces for a hard line break
            output_lines.append(line + "  ")
    return "\n".join(output_lines)


def _split_message(message: str, limit: int) -> list[str]:
    """Split a long message into chunks that fit within *limit* chars.

    Tries to split at paragraph boundaries (double newline) first,
    then at single newlines, and finally hard-cuts as a last resort.

    Args:
        message: The full message text.
        limit: Maximum characters per chunk.

    Returns:
        A list of message chunks.
    """
    if len(message) <= limit:
        return [message]

    chunks: list[str] = []
    remaining = message

    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break

        # Try to find a paragraph break within the limit
        cut_pos = remaining.rfind("\n\n", 0, limit)
        if cut_pos <= 0:
            # Fall back to single newline
            cut_pos = remaining.rfind("\n", 0, limit)
        if cut_pos <= 0:
            # Hard cut at limit
            cut_pos = limit

        chunks.append(remaining[:cut_pos].rstrip())
        remaining = remaining[cut_pos:].lstrip("\n")

    return chunks


# Action dispatcher
ACTION_HANDLERS: Dict[str, Callable] = {
    "shell": shell_action,
    "http": http_action,
    "message": message_action,
    "dingtalk": dingtalk_action,  # Send directly to DingTalk
    "chat": chat_action,  # AI conversation via Gateway + forward to DingTalk
}


# ==================== Job Execution ====================

# Global reference to scheduler for job execution
_scheduler_instance: Optional["CronScheduler"] = None

# Job execution history (in-memory, last N records per job)
_job_execution_history: Dict[str, list] = {}
MAX_HISTORY_PER_JOB = 10


def _record_execution(job_id: str, status: str, result: str, duration_ms: int) -> None:
    """Record job execution result."""
    from datetime import datetime
    if job_id not in _job_execution_history:
        _job_execution_history[job_id] = []
    
    record = {
        "timestamp": datetime.now().isoformat(),
        "status": status,  # "success" or "failed"
        "result": result[:500] if result else "",  # Truncate long results
        "duration_ms": duration_ms,
    }
    _job_execution_history[job_id].append(record)
    # Keep only last N records
    if len(_job_execution_history[job_id]) > MAX_HISTORY_PER_JOB:
        _job_execution_history[job_id] = _job_execution_history[job_id][-MAX_HISTORY_PER_JOB:]


def get_job_history(job_id: str) -> list:
    """Get execution history for a job."""
    return _job_execution_history.get(job_id, [])


def _execute_scheduled_job(
    job_id: str,
    action_type: str,
    action_args: Dict[str, Any],
    scheduled_run_time: Optional[datetime] = None,
) -> None:
    """Execute a scheduled job (called by APScheduler).

    This is a module-level function to avoid serialization issues.
    If the job fires after its scheduled time (e.g. after macOS
    sleep/wake), a warning is logged with the delay.

    Args:
        job_id: Unique job identifier.
        action_type: Action handler key (shell, http, message, etc.).
        action_args: Keyword arguments forwarded to the handler.
        scheduled_run_time: The originally scheduled fire time
            (injected automatically by APScheduler when the job is
            configured with ``misfire_grace_time``).
    """
    import asyncio
    import time

    # Detect delayed execution (e.g. after macOS sleep/wake)
    if scheduled_run_time is not None:
        now = datetime.now(scheduled_run_time.tzinfo)
        delay = (now - scheduled_run_time).total_seconds()
        if delay > 5:
            logger.warning(
                "Job %s firing %ds late (scheduled %s, now %s) — "
                "likely recovered from system sleep",
                job_id,
                int(delay),
                scheduled_run_time.strftime("%H:%M:%S"),
                now.strftime("%H:%M:%S"),
            )

    handler = ACTION_HANDLERS.get(action_type)
    if handler is None:
        logger.error("Unknown action type: %s", action_type)
        _record_execution(job_id, "failed", f"Unknown action type: {action_type}", 0)
        return

    start_time = time.time()
    try:
        # Create a new event loop for this thread (APScheduler runs in ThreadPoolExecutor)
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(handler(**action_args))
            duration_ms = int((time.time() - start_time) * 1000)
            logger.info("Job %s executed successfully in %sms", job_id, duration_ms)
            _record_execution(job_id, "success", str(result) if result else "OK", duration_ms)
        finally:
            loop.close()
    except Exception as e:
        duration_ms = int((time.time() - start_time) * 1000)
        logger.error("Job %s execution failed: %s", job_id, e, exc_info=True)
        _record_execution(job_id, "failed", str(e), duration_ms)


# ==================== Scheduler ====================

class CronScheduler:
    """APScheduler-based cron scheduler with SQLite persistence."""
    
    _instance: Optional["CronScheduler"] = None
    
    def __init__(self, db_path: Optional[str] = None):
        """Initialize the scheduler.
        
        Args:
            db_path: Path to SQLite database for job persistence.
        """
        if db_path is None:
            from pyclaw.config.paths import get_paths as _get_paths
            pyclaw_dir = _get_paths().state_dir
            pyclaw_dir.mkdir(parents=True, exist_ok=True)
            db_path = str(pyclaw_dir / "cron_jobs.db")
        
        self._db_path = db_path
        self._metadata_path = str(Path(db_path).parent / "cron_metadata.json")
        self._job_metadata: Dict[str, Dict] = {}  # Store custom metadata
        
        # Configure APScheduler with SQLite persistence
        jobstores = {
            'default': SQLAlchemyJobStore(url=f'sqlite:///{db_path}')
        }
        executors = {
            'default': ThreadPoolExecutor(max_workers=CRON_EXECUTOR_MAX_WORKERS)
        }
        job_defaults = {
            'coalesce': True,  # Merge missed executions
            'max_instances': 1,  # Max 1 instance of the same task running simultaneously
            'misfire_grace_time': CRON_MISFIRE_GRACE_TIME_SECONDS,  # 1h grace for macOS sleep
        }
        
        self._scheduler = AsyncIOScheduler(
            jobstores=jobstores,
            executors=executors,
            job_defaults=job_defaults,
            timezone='Asia/Shanghai',
        )
        
        self._running = False
        logger.info("CronScheduler initialized with SQLite db: %s", db_path)
    
    @classmethod
    def get_instance(cls, db_path: Optional[str] = None) -> "CronScheduler":
        """Get singleton instance."""
        if cls._instance is None:
            cls._instance = cls(db_path)
        return cls._instance
    
    async def start(self) -> None:
        """Start the scheduler."""
        if self._running:
            logger.warning("CronScheduler is already running")
            return
        
        self._scheduler.start()
        self._running = True
        
        # Load metadata from existing jobs
        self._load_job_metadata()
        
        logger.info("CronScheduler started")
    
    async def stop(self) -> None:
        """Stop the scheduler."""
        if not self._running:
            return
        
        self._scheduler.shutdown(wait=False)
        self._running = False
        logger.info("CronScheduler stopped")
    
    def _load_job_metadata(self) -> None:
        """Load job metadata from JSON file."""
        import json
        metadata_file = Path(self._metadata_path)
        if metadata_file.exists():
            try:
                self._job_metadata = json.loads(metadata_file.read_text())
                logger.info("Loaded metadata for %d jobs", len(self._job_metadata))
            except Exception as e:
                logger.warning("Failed to load job metadata: %s", e, exc_info=True)
                self._job_metadata = {}
    
    def _save_job_metadata(self) -> None:
        """Save job metadata to JSON file."""
        import json
        metadata_file = Path(self._metadata_path)
        try:
            metadata_file.write_text(json.dumps(self._job_metadata, ensure_ascii=False, indent=2))
        except Exception as e:
            logger.warning("Failed to save job metadata: %s", e, exc_info=True)
    
    def _create_trigger(self, trigger_type: str, trigger_args: Dict[str, Any]):
        """Create APScheduler trigger from type and args."""
        if trigger_type == "cron":
            return CronTrigger(**trigger_args)
        elif trigger_type == "interval":
            return IntervalTrigger(**trigger_args)
        elif trigger_type == "date":
            return DateTrigger(**trigger_args)
        else:
            raise ValueError(f"Unknown trigger type: {trigger_type}")
    
    def add_job(self, job_data: CronJobCreate) -> CronJobInfo:
        """Add a new scheduled job.
        
        Args:
            job_data: Job creation data.
            
        Returns:
            Created job info.
        """
        job_id = str(uuid.uuid4())[:8]
        
        # Create trigger
        trigger = self._create_trigger(job_data.trigger_type, job_data.trigger_args)
        
        # Add job to scheduler using module-level function (avoids pickle issues)
        job = self._scheduler.add_job(
            _execute_scheduled_job,
            trigger=trigger,
            id=job_id,
            name=job_data.name,
            args=[job_id, job_data.action_type, job_data.action_args],
            replace_existing=True,
        )
        
        # Pause if not enabled
        if not job_data.enabled:
            job.pause()
        
        # Store metadata
        self._job_metadata[job_id] = {
            "name": job_data.name,
            "description": job_data.description,
            "trigger_type": job_data.trigger_type,
            "trigger_args": job_data.trigger_args,
            "action_type": job_data.action_type,
            "action_args": job_data.action_args,
            "enabled": job_data.enabled,
            "created_at": datetime.now().isoformat(),
        }
        self._save_job_metadata()
        
        logger.info("Added job %s: %s", job_id, job_data.name)
        
        return self._job_to_info(job_id, job)
    
    def get_job(self, job_id: str) -> Optional[CronJobInfo]:
        """Get a job by ID."""
        job = self._scheduler.get_job(job_id)
        if job is None:
            return None
        return self._job_to_info(job_id, job)
    
    def list_jobs(self) -> List[CronJobInfo]:
        """List all jobs."""
        jobs = self._scheduler.get_jobs()
        return [self._job_to_info(job.id, job) for job in jobs]
    
    def update_job(self, job_id: str, job_data: CronJobUpdate) -> Optional[CronJobInfo]:
        """Update a job.
        
        Args:
            job_id: Job ID to update.
            job_data: Update data.
            
        Returns:
            Updated job info, or None if not found.
        """
        job = self._scheduler.get_job(job_id)
        if job is None:
            return None
        
        meta = self._job_metadata.get(job_id, {})
        
        # Update metadata
        if job_data.name is not None:
            meta["name"] = job_data.name
        if job_data.description is not None:
            meta["description"] = job_data.description
        if job_data.action_type is not None:
            meta["action_type"] = job_data.action_type
        if job_data.action_args is not None:
            meta["action_args"] = job_data.action_args
        
        # Update trigger if changed
        if job_data.trigger_type is not None or job_data.trigger_args is not None:
            trigger_type = job_data.trigger_type or meta.get("trigger_type", "cron")
            trigger_args = job_data.trigger_args or meta.get("trigger_args", {})
            meta["trigger_type"] = trigger_type
            meta["trigger_args"] = trigger_args
            
            trigger = self._create_trigger(trigger_type, trigger_args)
            self._scheduler.reschedule_job(job_id, trigger=trigger)
        
        # Update job args using module-level function
        self._scheduler.modify_job(
            job_id,
            name=meta.get("name", job.name),
            args=[job_id, meta.get("action_type", "message"), meta.get("action_args", {})],
        )
        
        # Handle enabled/disabled
        if job_data.enabled is not None:
            meta["enabled"] = job_data.enabled
            if job_data.enabled:
                self._scheduler.resume_job(job_id)
            else:
                self._scheduler.pause_job(job_id)
        
        self._job_metadata[job_id] = meta
        self._save_job_metadata()
        
        logger.info("Updated job %s", job_id)
        
        return self._job_to_info(job_id, self._scheduler.get_job(job_id))
    
    def delete_job(self, job_id: str) -> bool:
        """Delete a job.
        
        Args:
            job_id: Job ID to delete.
            
        Returns:
            True if deleted, False if not found.
        """
        job = self._scheduler.get_job(job_id)
        if job is None:
            return False
        
        self._scheduler.remove_job(job_id)
        self._job_metadata.pop(job_id, None)
        self._save_job_metadata()
        
        logger.info("Deleted job %s", job_id)
        return True
    
    def pause_job(self, job_id: str) -> bool:
        """Pause a job."""
        job = self._scheduler.get_job(job_id)
        if job is None:
            return False
        self._scheduler.pause_job(job_id)
        if job_id in self._job_metadata:
            self._job_metadata[job_id]["enabled"] = False
            self._save_job_metadata()
        return True
    
    def resume_job(self, job_id: str) -> bool:
        """Resume a paused job."""
        job = self._scheduler.get_job(job_id)
        if job is None:
            return False
        self._scheduler.resume_job(job_id)
        if job_id in self._job_metadata:
            self._job_metadata[job_id]["enabled"] = True
            self._save_job_metadata()
        return True
    
    async def run_job_now(self, job_id: str) -> bool:
        """Run a job immediately (async version for API calls)."""
        import time
        
        job = self._scheduler.get_job(job_id)
        if job is None:
            return False
        
        # Get metadata
        meta = self._job_metadata.get(job_id, {})
        action_type = meta.get("action_type", "message")
        action_args = meta.get("action_args", {})
        
        handler = ACTION_HANDLERS.get(action_type)
        if handler is None:
            logger.error("Unknown action type: %s", action_type)
            _record_execution(job_id, "failed", f"Unknown action type: {action_type}", 0)
            return False
        
        start_time = time.time()
        try:
            # Directly await handler (in FastAPI event loop)
            result = await handler(**action_args)
            duration_ms = int((time.time() - start_time) * 1000)
            logger.info("Job %s executed successfully in %sms", job_id, duration_ms)
            _record_execution(job_id, "success", str(result) if result else "OK", duration_ms)
            return True
        except Exception as e:
            duration_ms = int((time.time() - start_time) * 1000)
            logger.error("Job %s execution failed: %s", job_id, e, exc_info=True)
            _record_execution(job_id, "failed", str(e), duration_ms)
            return False
    
    def _job_to_info(self, job_id: str, job) -> CronJobInfo:
        """Convert APScheduler job to CronJobInfo."""
        meta = self._job_metadata.get(job_id, {})
        
        # Get next run time
        next_run = None
        if hasattr(job, 'next_run_time') and job.next_run_time:
            next_run = job.next_run_time.isoformat()
        
        return CronJobInfo(
            id=job_id,
            name=meta.get("name", job.name or ""),
            description=meta.get("description", ""),
            trigger_type=meta.get("trigger_type", "cron"),
            trigger_args=meta.get("trigger_args", {}),
            action_type=meta.get("action_type", "message"),
            action_args=meta.get("action_args", {}),
            enabled=meta.get("enabled", True),
            next_run_time=next_run,
            created_at=meta.get("created_at"),
        )


# Singleton access
def get_scheduler() -> CronScheduler:
    """Get the global scheduler instance."""
    return CronScheduler.get_instance()
