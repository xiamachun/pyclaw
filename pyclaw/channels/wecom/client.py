#!/usr/bin/env python3
"""
WeCom (Enterprise WeChat) Channel Client

Standalone process that:
1. Starts a local HTTP server to receive WeCom callback messages
2. Decrypts incoming messages using WeCom's AES encryption
3. Forwards messages to PyClaw Gateway via resolve_and_chat()
4. Sends AI replies back to users via WeCom message API

Requires a public URL (e.g. via ngrok) pointed at the callback port.

Usage:
    python -m pyclaw.channels.wecom.client
"""

import asyncio
import json
import logging
import re
import shutil
import signal
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Optional

# Add project root to Python path
_project_root = Path(__file__).parent.parent.parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

import httpx
from aiohttp import web

from pyclaw.channels.gateway_client import resolve_and_chat
from pyclaw.channels.wecom.crypto import WeComCrypto
from pyclaw.constants import DEFAULT_GATEWAY_URL

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("wecom_client")

# WeCom API base URL
WECOM_API_BASE = "https://qyapi.weixin.qq.com/cgi-bin"

# Access token refresh buffer (5 minutes before expiry)
TOKEN_REFRESH_BUFFER_SECONDS = 300

# Session timeout default (30 minutes)
DEFAULT_SESSION_TIMEOUT_MS = 1800000

# Cloudflared URL detection timeout
TUNNEL_STARTUP_TIMEOUT_SECONDS = 30

# Regex to extract public URL from cloudflared output
_CLOUDFLARED_URL_PATTERN = re.compile(r"https://[a-zA-Z0-9\-]+\.trycloudflare\.com")


class CloudflaredTunnel:
    """Manages a cloudflared Quick Tunnel as an independent long-lived process.

    The tunnel process is decoupled from the WeComClient lifecycle:
    - On start, checks if an existing tunnel is already running and reusable.
    - If reusable (process alive + URL reachable), skips restart entirely.
    - URL and PID are persisted to ~/.pyclaw/ so restarts of the wecom
      client do NOT restart cloudflared (avoiding URL changes).

    Args:
        local_port: Local port to tunnel.
        state_dir: Directory for persisting PID and URL files.
    """

    def __init__(self, local_port: int, state_dir: Optional[Path] = None):
        self._local_port = local_port
        self._process: Optional[subprocess.Popen] = None
        self._public_url: Optional[str] = None
        self._state_dir = state_dir or Path.home() / ".pyclaw"
        self._pid_file = self._state_dir / "cloudflared.pid"
        self._url_file = self._state_dir / "cloudflared_url.txt"

    @property
    def public_url(self) -> Optional[str]:
        """Return the detected public URL, or None if not yet available."""
        return self._public_url

    def _read_saved_state(self) -> tuple[Optional[int], Optional[str]]:
        """Read saved PID and URL from state files.

        Returns:
            Tuple of (pid, url), either may be None if file is missing.
        """
        saved_pid: Optional[int] = None
        saved_url: Optional[str] = None

        if self._pid_file.exists():
            try:
                saved_pid = int(self._pid_file.read_text().strip())
            except (ValueError, OSError):
                pass

        if self._url_file.exists():
            try:
                saved_url = self._url_file.read_text().strip()
            except OSError:
                pass

        return saved_pid, saved_url

    def _save_state(self, pid: int, url: str) -> None:
        """Persist PID and URL to state files.

        Args:
            pid: Process ID of the cloudflared process.
            url: Public HTTPS URL assigned by Cloudflare.
        """
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._pid_file.write_text(str(pid))
        self._url_file.write_text(url)

    @staticmethod
    def _is_process_alive(pid: int) -> bool:
        """Check if a process with the given PID is still running.

        Args:
            pid: Process ID to check.

        Returns:
            True if the process is alive.
        """
        try:
            import os
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False

    @staticmethod
    def _is_url_reachable(url: str) -> bool:
        """Check if a cloudflared URL is still reachable via HTTP.

        Args:
            url: Public HTTPS URL to check.

        Returns:
            True if the URL responds (any status code).
        """
        try:
            result = subprocess.run(
                ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                 "--max-time", "5", url],
                capture_output=True, text=True, timeout=10,
            )
            return result.returncode == 0 and result.stdout.strip() != "000"
        except Exception:
            return False

    @staticmethod
    def kill_all_processes() -> None:
        """Kill all existing cloudflared tunnel processes.

        Prevents accumulation of orphaned cloudflared processes.
        """
        try:
            result = subprocess.run(
                ["pkill", "-f", "cloudflared tunnel"],
                capture_output=True, timeout=5,
            )
            if result.returncode == 0:
                logger.info("Killed existing cloudflared processes")
                time.sleep(1)
        except Exception:
            pass

    async def try_reuse_existing(self) -> Optional[str]:
        """Try to reuse an existing cloudflared tunnel.

        Checks if a previously started tunnel is still alive and its URL
        is reachable. If so, returns the URL without starting a new process.

        Returns:
            The reusable public URL, or None if no reusable tunnel found.
        """
        saved_pid, saved_url = self._read_saved_state()

        if saved_pid and saved_url:
            if self._is_process_alive(saved_pid):
                logger.info(
                    "Found existing cloudflared (PID %d), checking URL...",
                    saved_pid,
                )
                if self._is_url_reachable(saved_url):
                    self._public_url = saved_url
                    logger.info(
                        "Reusing existing cloudflared tunnel: %s (PID %d)",
                        saved_url, saved_pid,
                    )
                    return saved_url
                else:
                    logger.warning(
                        "Existing cloudflared (PID %d) URL is unreachable, "
                        "will restart tunnel",
                        saved_pid,
                    )
            else:
                logger.info(
                    "Saved cloudflared PID %d is no longer running",
                    saved_pid,
                )

        return None

    async def start(self, max_retries: int = 10) -> str:
        """Start cloudflared, reusing an existing tunnel if possible.

        First checks for a running tunnel that can be reused. If not,
        kills any stale processes and starts a new one with retries.

        Args:
            max_retries: Maximum number of start attempts.

        Returns:
            The public HTTPS URL assigned by Cloudflare.

        Raises:
            RuntimeError: If cloudflared is not installed or all retries
                are exhausted.
        """
        # Try to reuse existing tunnel first
        reused_url = await self.try_reuse_existing()
        if reused_url:
            return reused_url

        cloudflared_bin = shutil.which("cloudflared")
        if not cloudflared_bin:
            raise RuntimeError(
                "cloudflared is not installed. "
                "Install it with: brew install cloudflared (macOS) or "
                "see https://developers.cloudflare.com/cloudflare-one/"
                "connections/connect-networks/downloads/"
            )

        # Kill any stale cloudflared processes before starting fresh
        self.kill_all_processes()

        last_error: Optional[Exception] = None
        for attempt in range(1, max_retries + 1):
            logger.info(
                "Starting cloudflared tunnel for localhost:%d (attempt %d/%d) ...",
                self._local_port, attempt, max_retries,
            )

            self._process = subprocess.Popen(
                [cloudflared_bin, "tunnel", "--url",
                 "http://localhost:%d" % self._local_port],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            try:
                url = await self._wait_for_url()
                self._public_url = url
                # Persist state so future restarts can reuse this tunnel
                self._save_state(self._process.pid, url)
                logger.info("Cloudflared tunnel established: %s", url)
                return url
            except RuntimeError as exc:
                last_error = exc
                logger.warning(
                    "Cloudflared attempt %d/%d failed: %s",
                    attempt, max_retries, exc,
                )
                # Clean up failed process
                if self._process and self._process.poll() is None:
                    self._process.kill()
                    self._process.wait(timeout=3)
                self._process = None

                if attempt < max_retries:
                    wait_seconds = min(attempt * 2, 10)
                    logger.info(
                        "Retrying in %d seconds...", wait_seconds
                    )
                    await asyncio.sleep(wait_seconds)

        raise RuntimeError(
            "cloudflared failed after %d attempts. Last error: %s"
            % (max_retries, last_error)
        )

    async def _wait_for_url(self) -> str:
        """Read cloudflared stderr until the public URL appears.

        Returns:
            Detected public URL.

        Raises:
            RuntimeError: If URL is not detected within timeout or process
                exits unexpectedly.
        """
        loop = asyncio.get_event_loop()
        deadline = time.time() + TUNNEL_STARTUP_TIMEOUT_SECONDS
        collected_output: list[str] = []

        while time.time() < deadline:
            if self._process is None or self._process.poll() is not None:
                exit_code = (
                    self._process.returncode if self._process else -1
                )
                raise RuntimeError(
                    "cloudflared exited unexpectedly (code %d). Output: %s"
                    % (exit_code, "".join(collected_output))
                )

            line = await loop.run_in_executor(
                None, self._read_stderr_line
            )
            if line:
                collected_output.append(line)
                logger.debug("cloudflared: %s", line.rstrip())
                match = _CLOUDFLARED_URL_PATTERN.search(line)
                if match:
                    return match.group(0)

            await asyncio.sleep(0.1)

        raise RuntimeError(
            "Timed out waiting for cloudflared URL (%ds). Output: %s"
            % (TUNNEL_STARTUP_TIMEOUT_SECONDS, "".join(collected_output))
        )

    def _read_stderr_line(self) -> str:
        """Read one line from cloudflared stderr (blocking).

        Returns:
            Decoded line string, or empty string if nothing available.
        """
        if self._process and self._process.stderr:
            line = self._process.stderr.readline()
            if line:
                return line.decode("utf-8", errors="replace")
        return ""

    def stop(self) -> None:
        """Stop the cloudflared subprocess."""
        if self._process and self._process.poll() is None:
            logger.info("Stopping cloudflared tunnel...")
            self._process.send_signal(signal.SIGTERM)
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
            logger.info("Cloudflared tunnel stopped")


class WeComTokenManager:
    """Manages WeCom access_token with automatic refresh.

    Args:
        corp_id: Enterprise Corp ID.
        secret: App secret for the self-built application.
    """

    def __init__(self, corp_id: str, secret: str):
        self._corp_id = corp_id
        self._secret = secret
        self._access_token: Optional[str] = None
        self._expire_time: float = 0

    async def get_token(self, http_client: httpx.AsyncClient) -> str:
        """Get a valid access_token, refreshing if needed.

        Args:
            http_client: Async HTTP client for API calls.

        Returns:
            Valid access_token string.

        Raises:
            RuntimeError: If token fetch fails.
        """
        now = time.time()
        if self._access_token and now < self._expire_time - TOKEN_REFRESH_BUFFER_SECONDS:
            return self._access_token

        url = "%s/gettoken" % WECOM_API_BASE
        params = {"corpid": self._corp_id, "corpsecret": self._secret}
        response = await http_client.get(url, params=params)
        data = response.json()

        if data.get("errcode", 0) != 0:
            raise RuntimeError(
                "Failed to get WeCom access_token: errcode=%s, errmsg=%s"
                % (data.get("errcode"), data.get("errmsg"))
            )

        self._access_token = data["access_token"]
        self._expire_time = now + data.get("expires_in", 7200)
        logger.info(
            "WeCom access_token refreshed, expires in %ds",
            data.get("expires_in", 7200),
        )
        return self._access_token


class WeComClient:
    """WeCom channel client.

    Runs a local HTTP server for receiving WeCom callback messages,
    processes them through PyClaw Gateway, and sends replies back.

    Args:
        corp_id: Enterprise Corp ID.
        agent_id: Self-built app Agent ID.
        secret: App secret.
        token: Callback verification token.
        encoding_aes_key: 43-char AES key for message encryption.
        callback_port: Local port for the callback HTTP server.
        gateway_url: PyClaw Gateway URL.
        gateway_token: Gateway authentication token.
        session_timeout_ms: Session timeout in milliseconds.
        callback_url: Fixed public callback URL. If set, automatic tunnel
            is skipped.
        tunnel_enabled: Whether to auto-start a cloudflared tunnel when
            no callback_url is provided.
    """

    def __init__(
        self,
        corp_id: str,
        agent_id: int,
        secret: str,
        token: str,
        encoding_aes_key: str,
        callback_port: int = 18790,
        gateway_url: str = DEFAULT_GATEWAY_URL,
        gateway_token: Optional[str] = None,
        session_timeout_ms: int = DEFAULT_SESSION_TIMEOUT_MS,
        callback_url: Optional[str] = None,
        tunnel_enabled: bool = True,
        request_timeout_seconds: int = 960,
    ):
        self.corp_id = corp_id
        self.agent_id = agent_id
        self.callback_port = callback_port
        self.gateway_url = gateway_url
        self.gateway_token = gateway_token or ""
        self.session_timeout_ms = session_timeout_ms
        self.callback_url = callback_url
        self.tunnel_enabled = tunnel_enabled
        self.request_timeout_seconds = request_timeout_seconds

        self._encoding_aes_key = encoding_aes_key
        self._crypto = WeComCrypto(token, encoding_aes_key, corp_id)
        self._token_manager = WeComTokenManager(corp_id, secret)
        self._http_client: Optional[httpx.AsyncClient] = None
        self._tunnel: Optional[CloudflaredTunnel] = None
        self._sessions: dict[str, list] = {}
        self._state_dir = Path.home() / ".pyclaw"
        self._sessions_file = self._state_dir / "wecom_sessions.json"

        self._load_sessions()

    def _load_sessions(self) -> None:
        """Load session history from disk."""
        try:
            if self._sessions_file.exists():
                self._sessions = json.loads(self._sessions_file.read_text())
                logger.info(
                    "Loaded %d WeCom sessions from %s",
                    len(self._sessions),
                    self._sessions_file,
                )
        except Exception as exc:
            logger.warning("Failed to load WeCom sessions: %s", exc, exc_info=True)

    def _save_sessions(self) -> None:
        """Save session history to disk."""
        try:
            self._state_dir.mkdir(parents=True, exist_ok=True)
            self._sessions_file.write_text(
                json.dumps(self._sessions, ensure_ascii=False, indent=2)
            )
        except Exception as exc:
            logger.warning("Failed to save WeCom sessions: %s", exc, exc_info=True)

    async def _send_text_message(self, user_id: str, content: str) -> bool:
        """Send a text message to a WeCom user.

        Args:
            user_id: Target user's WeCom UserID.
            content: Message text content.

        Returns:
            True if message was sent successfully.
        """
        if not self._http_client:
            logger.error("HTTP client not initialized")
            return False

        try:
            access_token = await self._token_manager.get_token(self._http_client)
            url = "%s/message/send?access_token=%s" % (WECOM_API_BASE, access_token)
            payload = {
                "touser": user_id,
                "msgtype": "text",
                "agentid": self.agent_id,
                "text": {"content": content},
                "safe": 0,
            }
            response = await self._http_client.post(url, json=payload)
            result = response.json()

            if result.get("errcode", 0) != 0:
                logger.error(
                    "Failed to send WeCom message to %s: errcode=%s, errmsg=%s",
                    user_id,
                    result.get("errcode"),
                    result.get("errmsg"),
                )
                return False

            logger.info("Sent WeCom message to user %s (%d chars)", user_id, len(content))
            return True
        except Exception as exc:
            logger.error("Error sending WeCom message: %s", exc, exc_info=True)
            return False

    async def _handle_user_message(self, user_id: str, content: str) -> None:
        """Process a user message through PyClaw Gateway and reply.

        Args:
            user_id: Sender's WeCom UserID.
            content: Message text content.
        """
        logger.info("Processing message from user %s: %s", user_id, content[:100])

        try:
            reply = await resolve_and_chat(
                channel="wecom",
                peer_id=user_id,
                peer_kind="direct",
                message=content,
                gateway_url=self.gateway_url,
                gateway_token=self.gateway_token,
                http_client=self._http_client,
                sessions=self._sessions,
            )

            if reply:
                await self._send_text_message(user_id, reply)
                self._save_sessions()
            else:
                logger.warning("No reply from Gateway for user %s", user_id)
                await self._send_text_message(
                    user_id, "Sorry, I could not process your message. Please try again."
                )
        except Exception as exc:
            logger.error(
                "Error processing message from user %s: %s", user_id, exc, exc_info=True
            )
            await self._send_text_message(
                user_id, "An error occurred while processing your message."
            )

    async def _handle_callback_get(self, request: web.Request) -> web.Response:
        """Handle WeCom URL verification (GET request).

        Args:
            request: aiohttp request object.

        Returns:
            Decrypted echostr as plain text response.
        """
        msg_signature = request.query.get("msg_signature", "")
        timestamp = request.query.get("timestamp", "")
        nonce = request.query.get("nonce", "")
        echostr = request.query.get("echostr", "")

        logger.info(
            "WeCom URL verification request: msg_signature=%s, timestamp=%s, "
            "nonce=%s, echostr=%s",
            msg_signature, timestamp, nonce, echostr[:30] if echostr else "",
        )

        if not msg_signature or not echostr:
            logger.warning("WeCom URL verification: missing required params")
            return web.Response(status=403, text="Missing required parameters")

        if not self._crypto.verify_signature(msg_signature, timestamp, nonce, echostr):
            logger.warning(
                "WeCom URL verification failed: signature mismatch. "
                "Check that token and encodingAesKey match your WeCom app config."
            )
            return web.Response(status=403, text="Signature verification failed")

        try:
            decrypted = self._crypto.decrypt_callback_echostr(echostr)
            logger.info("WeCom URL verification succeeded")
            return web.Response(text=decrypted)
        except Exception as exc:
            logger.error("WeCom echostr decryption failed: %s", exc, exc_info=True)
            return web.Response(status=500, text="Decryption failed")

    async def _handle_callback_post(self, request: web.Request) -> web.Response:
        """Handle WeCom message callback (POST request).

        Args:
            request: aiohttp request object.

        Returns:
            Success response (WeCom expects "success" or empty body).
        """
        msg_signature = request.query.get("msg_signature", "")
        timestamp = request.query.get("timestamp", "")
        nonce = request.query.get("nonce", "")

        try:
            xml_body = await request.text()
            decrypted_xml = self._crypto.decrypt_message(
                xml_body, msg_signature, timestamp, nonce
            )

            root = ET.fromstring(decrypted_xml)
            msg_type = root.findtext("MsgType", "")
            from_user = root.findtext("FromUserName", "")
            content = root.findtext("Content", "")

            logger.info(
                "WeCom callback: MsgType=%s, FromUser=%s, Content=%s",
                msg_type,
                from_user,
                (content or "")[:50],
            )

            if msg_type == "text" and from_user and content:
                # Process asynchronously so we can return 200 immediately
                asyncio.create_task(self._handle_user_message(from_user, content.strip()))

            return web.Response(text="success")
        except Exception as exc:
            logger.error("Error handling WeCom callback: %s", exc, exc_info=True)
            return web.Response(status=200, text="success")

    def _print_callback_url_notice(
        self, public_url: str, is_reused: bool
    ) -> None:
        """Print the callback URL, prominently if it changed.

        When the tunnel is reused (URL unchanged), prints a brief info line.
        When the URL is new, prints a prominent banner so the user knows
        to update the WeCom admin console.

        Args:
            public_url: The public HTTPS base URL.
            is_reused: True if the URL was reused from an existing tunnel.
        """
        callback_url = public_url + "/wecom/callback"
        if is_reused:
            logger.info("Callback URL (unchanged): %s", callback_url)
        else:
            logger.info("=" * 60)
            logger.info("  NEW CALLBACK URL — update WeCom admin console!")
            logger.info("  %s", callback_url)
            logger.info("=" * 60)
            logger.info(
                "Go to: WeCom Admin -> Apps -> Your App -> "
                "API Receive -> set the URL above"
            )

    async def start(self) -> None:
        """Start the WeCom client (HTTP callback server + tunnel)."""
        self._state_dir.mkdir(parents=True, exist_ok=True)

        self._http_client = httpx.AsyncClient(
            timeout=self.request_timeout_seconds, verify=False
        )

        # Try to pre-fetch access token
        try:
            await self._token_manager.get_token(self._http_client)
            logger.info("WeCom credentials validated, access_token obtained")
        except Exception as exc:
            logger.warning(
                "Could not pre-fetch WeCom access_token: %s. "
                "Callback server will still start. "
                "Token will be fetched when first message arrives.",
                exc,
            )

        # Set up aiohttp callback server
        app = web.Application()
        app.router.add_get("/wecom/callback", self._handle_callback_get)
        app.router.add_post("/wecom/callback", self._handle_callback_post)
        app.router.add_get("/health", self._handle_health)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self.callback_port)
        await site.start()

        logger.info(
            "WeCom callback server started on port %d", self.callback_port
        )

        # Determine public URL
        public_url = self.callback_url
        tunnel_reused = False
        if not public_url and self.tunnel_enabled:
            # Start or reuse cloudflared tunnel (independent process)
            self._tunnel = CloudflaredTunnel(
                self.callback_port, state_dir=self._state_dir
            )
            try:
                # Check if we can reuse an existing tunnel
                reused_url = await self._tunnel.try_reuse_existing()
                if reused_url:
                    public_url = reused_url
                    tunnel_reused = True
                else:
                    public_url = await self._tunnel.start()
            except RuntimeError as exc:
                self._tunnel = None
                logger.warning("=" * 60)
                logger.warning("CLOUDFLARED TUNNEL UNAVAILABLE")
                logger.warning("=" * 60)
                logger.warning("%s", exc)
                logger.warning(
                    "The WeCom callback server is running locally on "
                    "port %d, but it is NOT reachable from the internet.",
                    self.callback_port,
                )
                logger.warning(
                    "To fix this, either:\n"
                    "  1. Install cloudflared: "
                    "brew install cloudflared (macOS)\n"
                    "  2. Or set a fixed public URL in pyclaw.json: "
                    '"callbackUrl": "https://your-domain.com"'
                )
                logger.warning("=" * 60)

        if public_url:
            self._print_callback_url_notice(public_url, is_reused=tunnel_reused)
        else:
            logger.warning(
                "No public URL available. WeCom will not be able to "
                "send messages to this server until a public URL is "
                "configured. The server will keep running and waiting."
            )

        # Keep running — tunnel is NOT stopped on exit (independent process)
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            pass
        finally:
            await runner.cleanup()
            if self._http_client:
                await self._http_client.aclose()

    async def _handle_health(self, request: web.Request) -> web.Response:
        """Health check endpoint.

        Args:
            request: aiohttp request object.

        Returns:
            JSON health status.
        """
        return web.json_response({
            "status": "ok",
            "channel": "wecom",
            "corp_id": self.corp_id,
            "agent_id": self.agent_id,
            "sessions": len(self._sessions),
        })


def load_config() -> dict[str, Any]:
    """Load WeCom configuration from pyclaw.json.

    Returns:
        Configuration dictionary with WeCom settings.

    Raises:
        SystemExit: If configuration is missing or invalid.
    """
    config_paths = [
        Path.cwd() / "pyclaw.json",
        Path.home() / ".pyclaw" / "pyclaw.json",
    ]

    config_file = None
    for candidate in config_paths:
        if candidate.exists():
            config_file = candidate
            break

    if not config_file:
        logger.error("pyclaw.json not found in %s", [str(p) for p in config_paths])
        sys.exit(1)

    with open(config_file) as fh:
        config = json.load(fh)

    wecom_config = config.get("channels", {}).get("wecom-connector", {})
    if not wecom_config.get("enabled"):
        logger.error("WeCom channel is not enabled in pyclaw.json")
        sys.exit(1)

    required_fields = ["corpId", "agentId", "secret", "token", "encodingAesKey"]
    missing = [f for f in required_fields if not wecom_config.get(f)]
    if missing:
        logger.error("Missing required WeCom config fields: %s", missing)
        sys.exit(1)

    gateway_config = config.get("gateway", {})
    gateway_token = wecom_config.get("gatewayToken") or gateway_config.get("auth", {}).get("token", "")

    return {
        "corp_id": wecom_config["corpId"],
        "agent_id": wecom_config["agentId"],
        "secret": wecom_config["secret"],
        "token": wecom_config["token"],
        "encoding_aes_key": wecom_config["encodingAesKey"],
        "callback_port": wecom_config.get("callbackPort", 18790),
        "gateway_url": "http://%s:%d" % (
            gateway_config.get("host", "127.0.0.1"),
            gateway_config.get("port", 18789),
        ),
        "gateway_token": gateway_token,
        "session_timeout_ms": wecom_config.get("sessionTimeout", DEFAULT_SESSION_TIMEOUT_MS),
        "callback_url": wecom_config.get("callbackUrl"),
        "tunnel_enabled": wecom_config.get("tunnelEnabled", True),
        "request_timeout_seconds": wecom_config.get("requestTimeout", 960000) // 1000,
    }


def main() -> None:
    """Entry point for the WeCom channel client."""
    logger.info("=" * 60)
    logger.info("PyClaw WeCom Channel Client starting...")
    logger.info("=" * 60)

    config = load_config()
    logger.info(
        "WeCom config: corp_id=%s, agent_id=%s, callback_port=%d",
        config["corp_id"],
        config["agent_id"],
        config["callback_port"],
    )

    client = WeComClient(
        corp_id=config["corp_id"],
        agent_id=config["agent_id"],
        secret=config["secret"],
        token=config["token"],
        encoding_aes_key=config["encoding_aes_key"],
        callback_port=config["callback_port"],
        gateway_url=config["gateway_url"],
        gateway_token=config["gateway_token"],
        session_timeout_ms=config["session_timeout_ms"],
        callback_url=config.get("callback_url"),
        tunnel_enabled=config.get("tunnel_enabled", True),
        request_timeout_seconds=config.get("request_timeout_seconds", 960),
    )

    try:
        asyncio.run(client.start())
    except KeyboardInterrupt:
        logger.info("WeCom client stopped by user")
    except Exception as exc:
        logger.error("WeCom client fatal error: %s", exc, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
