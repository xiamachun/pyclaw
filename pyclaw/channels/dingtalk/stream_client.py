#!/usr/bin/env python3
"""
DingTalk Stream Client

Connects to DingTalk servers using the official dingtalk-stream SDK,
receives messages and processes them asynchronously via message queue.

Architecture design (inspired by claude-code):
- Message reception: Immediately enqueue and return ACK
- Message processing: Background async, supports concurrency
- Message queue: Three priority levels (now > next > later)
"""

import asyncio
import json
import logging
import os
import ssl
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

# Add project root to Python path
_project_root = Path(__file__).parent.parent.parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

import certifi

from pyclaw.constants import DEFAULT_GATEWAY_URL
import httpx

# Try to import DingTalk Stream SDK
try:
    from dingtalk_stream import (
        AckMessage,
        CallbackHandler,
        CallbackMessage,
        ChatbotHandler,
        ChatbotMessage,
        Credential,
        DingTalkStreamClient,
    )
except ImportError:
    logger.error("Please install dingtalk-stream first: pip install dingtalk-stream")
    sys.exit(1)

# Import message queue
from pyclaw.infra.message_queue import (
    AsyncMessageProcessor,
    MessageQueue,
    Priority,
    QueuedMessage,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s"
)
logger = logging.getLogger("dingtalk_client")


@dataclass
class DingTalkMessage:
    """DingTalk message data"""
    sender_id: str
    sender_nick: str
    content: str
    session_webhook: str
    conversation_id: str = ""
    received_at: float = 0.0
    
    def to_dict(self) -> dict:
        return {
            "sender_id": self.sender_id,
            "sender_nick": self.sender_nick,
            "content": self.content,
            "session_webhook": self.session_webhook,
            "conversation_id": self.conversation_id,
            "received_at": self.received_at,
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "DingTalkMessage":
        return cls(**data)

class PyClawChatbotHandler(CallbackHandler):
    """Handler for processing DingTalk bot messages
    
    Architecture design (inspired by claude-code):
    - process() method immediately enqueues messages and returns ACK
    - Message queue buffers all messages, no message loss
    - Background processor handles queue messages concurrently
    - Session history persisted to disk, survives restarts
    """
    
    from pyclaw.config.paths import get_paths as _get_paths
    _paths = _get_paths()
    # Session history persistence file path
    SESSIONS_FILE = _paths.dingtalk_sessions_file
    # Webhook cache file path (for scheduled task sending)
    WEBHOOKS_FILE = _paths.dingtalk_webhooks_file
    
    # Global instance reference (for scheduled task sending)
    _instance: Optional["PyClawChatbotHandler"] = None
    
    def __init__(
        self,
        gateway_url: str = DEFAULT_GATEWAY_URL,
        gateway_token: Optional[str] = None,
        max_concurrency: int = 5,
        request_timeout_seconds: int = 960,
    ):
        super().__init__()
        self.gateway_url = gateway_url
        self.gateway_token = gateway_token
        self.max_concurrency = max_concurrency
        self.request_timeout_seconds = request_timeout_seconds
        
        # Load session history from disk (survives restarts)
        self._sessions: dict[str, list] = self._load_sessions()
        
        # Load webhook cache (for scheduled task sending)
        self._webhooks: dict[str, str] = self._load_webhooks()
        
        # Create message queue
        self._queue = MessageQueue()
        self._processor: Optional[AsyncMessageProcessor] = None
        self._processor_started = False
        
        # Statistics
        self._stats = {
            "received": 0,
            "processed": 0,
            "failed": 0,
        }
        
        # Message deduplication
        self._processed_msg_ids: set = set()
        
        # Set global instance reference
        PyClawChatbotHandler._instance = self
        
        logger.info("Loaded %d user sessions, %d webhook caches", len(self._sessions), len(self._webhooks))
    
    def _load_sessions(self) -> dict[str, list]:
        """Load session history from disk"""
        try:
            if self.SESSIONS_FILE.exists():
                data = json.loads(self.SESSIONS_FILE.read_text())
                logger.info("Loaded session history from %s", self.SESSIONS_FILE)
                return data
        except Exception as e:
            logger.warning("Failed to load session history: %s", e, exc_info=True)
        return {}
    
    def _save_sessions(self) -> None:
        """Save session history to disk"""
        try:
            self.SESSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
            self.SESSIONS_FILE.write_text(
                json.dumps(self._sessions, ensure_ascii=False, indent=2)
            )
        except Exception as e:
            logger.warning("Failed to save session history: %s", e, exc_info=True)
    
    def _load_webhooks(self) -> dict[str, str]:
        """Load webhook cache from disk"""
        try:
            if self.WEBHOOKS_FILE.exists():
                data = json.loads(self.WEBHOOKS_FILE.read_text())
                return data
        except Exception as e:
            logger.warning("Failed to load webhook cache: %s", e, exc_info=True)
        return {}
    
    def _save_webhooks(self) -> None:
        """Save webhook cache to disk"""
        try:
            self.WEBHOOKS_FILE.parent.mkdir(parents=True, exist_ok=True)
            self.WEBHOOKS_FILE.write_text(
                json.dumps(self._webhooks, ensure_ascii=False, indent=2)
            )
        except Exception as e:
            logger.warning("Failed to save webhook cache: %s", e, exc_info=True)
    
    def _update_webhook(self, user_id: str, webhook_url: str) -> None:
        """Update user's webhook cache"""
        if webhook_url:
            self._webhooks[user_id] = webhook_url
            self._save_webhooks()
    
    async def _ensure_processor_started(self) -> None:
        """Ensure message processor is started"""
        if self._processor_started:
            return
        
        self._processor = AsyncMessageProcessor(
            queue=self._queue,
            handler=self._handle_queued_message,
            max_concurrency=self.max_concurrency,
        )
        await self._processor.start()
        self._processor_started = True
        logger.info("Message processor started (max_concurrency=%d)", self.max_concurrency)
    
    async def process(self, message: CallbackMessage) -> AckMessage:
        """Process received callback message
        
        Immediately enqueue message and return ACK without any processing
        This ensures no message loss
        """
        try:
            # Ensure processor is started
            await self._ensure_processor_started()
            
            # Extract information from raw data
            data = message.data if hasattr(message, 'data') else {}
            
            # Message deduplication: DingTalk Stream may resend messages
            msg_key = data.get("msgId") or data.get("chatbotCorpId", "") + str(data.get("createAt", ""))
            if not hasattr(self, "_processed_msg_ids"):
                self._processed_msg_ids = set()
            if msg_key and msg_key in self._processed_msg_ids:
                logger.info("Skipping duplicate message: %s", msg_key)
                return AckMessage.STATUS_OK, None
            if msg_key:
                self._processed_msg_ids.add(msg_key)
                # Limit set size to avoid memory leak
                if len(self._processed_msg_ids) > 1000:
                    self._processed_msg_ids = set(list(self._processed_msg_ids)[-500:])
            if not data and hasattr(message, 'to_dict'):
                data = message.to_dict()
            if not data and hasattr(message, '__dict__'):
                data = message.__dict__
            
            # Extract text content
            content = ""
            text_data = data.get('text', {})
            if isinstance(text_data, dict):
                content = text_data.get('content', '').strip()
            elif isinstance(text_data, str):
                content = text_data.strip()
            
            # Extract sender information
            sender_id = data.get('senderStaffId') or data.get('senderId') or "unknown"
            sender_nick = data.get('senderNick') or data.get('sender_nick') or "User"
            session_webhook = data.get('sessionWebhook') or data.get('session_webhook') or ""
            conversation_id = data.get('conversationId') or data.get('conversation_id') or ""
            
            # Create message object
            dt_msg = DingTalkMessage(
                sender_id=sender_id,
                sender_nick=sender_nick,
                content=content,
                session_webhook=session_webhook,
                conversation_id=conversation_id,
                received_at=time.time(),
            )
            
            # Immediately enqueue without any processing
            msg_id = self._queue.enqueue(
                content=dt_msg.to_dict(),
                priority=Priority.NEXT,
                metadata={"sender_nick": sender_nick},
            )
            
            self._stats["received"] += 1
            logger.info("Message enqueued [%s] from %s: %s (queue=%d)", msg_id, sender_nick, content[:30] if content else '(empty)', self._queue.size)
            
            # Immediately return ACK
            return AckMessage.STATUS_OK, None
                
        except Exception as e:
            logger.error("Failed to enqueue message: %s", e, exc_info=True)
            return AckMessage.STATUS_OK, None
    
    async def _handle_queued_message(self, queued_msg: QueuedMessage) -> None:
        """Process queued message (called by AsyncMessageProcessor)"""
        try:
            # Parse message
            dt_msg = DingTalkMessage.from_dict(queued_msg.content)
            
            # Cache webhook (for scheduled task sending)
            if dt_msg.session_webhook and dt_msg.sender_id:
                self._update_webhook(dt_msg.sender_id, dt_msg.session_webhook)
            
            # Empty message handling
            if not dt_msg.content:
                if dt_msg.session_webhook:
                    await self._reply_via_webhook(dt_msg.session_webhook, "Please send text messages")
                return

            # Send "processing" notification
            if dt_msg.session_webhook:
                await self._reply_via_webhook(dt_msg.session_webhook, "✨ Got it, processing...")

            # Call Gateway
            reply = await self._call_gateway(dt_msg.sender_id, dt_msg.content)
            
            # Send reply
            if reply and dt_msg.session_webhook:
                logger.info("Reply to %s: %s...", dt_msg.sender_nick, reply[:50])
                await self._reply_via_webhook(dt_msg.session_webhook, reply)
                self._stats["processed"] += 1
            elif dt_msg.session_webhook:
                await self._reply_via_webhook(dt_msg.session_webhook, "Sorry, I cannot reply at the moment")
                
        except Exception as e:
            self._stats["failed"] += 1
            logger.error("Failed to process message [%s]: %s", queued_msg.id, e, exc_info=True)
            try:
                dt_msg = DingTalkMessage.from_dict(queued_msg.content)
                if dt_msg.session_webhook:
                    await self._reply_via_webhook(dt_msg.session_webhook, f"❌ Processing failed: {str(e)[:50]}")
            except Exception:
                logger.debug("Failed to send error reply for message [%s]", queued_msg.id)
    
    async def _reply_via_webhook(self, webhook_url: str, content: str) -> bool:
        """Reply message via session webhook
        
        Args:
            webhook_url: Webhook URL provided by DingTalk
            content: Reply content
            
        Returns:
            Whether successful
        """
        try:
            payload = {
                "msgtype": "text",
                "text": {
                    "content": content
                }
            }
            
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(webhook_url, json=payload)
                
                if response.status_code == 200:
                    result = response.json()
                    if result.get('errcode') == 0:
                        logger.info("Reply message sent successfully")
                        return True
                    else:
                        logger.error("Reply failed: %s", result, exc_info=True)
                else:
                    logger.error("Webhook request failed: %d", response.status_code, exc_info=True)
                    
        except Exception as e:
            logger.error("Send reply exception: %s", e, exc_info=True)
        
        return False
    
    async def _call_gateway(self, user_id: str, message: str) -> Optional[str]:
        """Call PyClaw Gateway chatCompletions API
        
        Args:
            user_id: User ID
            message: User message
            
        Returns:
            AI reply
        """
        url = f"{self.gateway_url}/v1/chat/completions"
        
        # Build request headers
        headers = {"Content-Type": "application/json"}
        if self.gateway_token:
            headers["Authorization"] = f"Bearer {self.gateway_token}"
        
        # Get or create session history
        if user_id not in self._sessions:
            self._sessions[user_id] = []
        
        history = self._sessions[user_id]
        
        # Add user message
        history.append({"role": "user", "content": message})
        
        # Limit history length
        if len(history) > 20:
            history = history[-20:]
            self._sessions[user_id] = history
        
        # Save session history to disk (not lost on restart)
        self._save_sessions()
        
        payload = {
            "model": "default",
            "messages": history,
            "stream": False,
            "user": user_id,
        }
        
        try:
            async with httpx.AsyncClient(timeout=self.request_timeout_seconds) as client:
                response = await client.post(url, headers=headers, json=payload)
                response.raise_for_status()
                result = response.json()
                
                reply = result.get("choices", [{}])[0].get("message", {}).get("content", "")
                
                if reply:
                    # Add assistant reply to history
                    history.append({"role": "assistant", "content": reply})
                    # Save session history to disk
                    self._save_sessions()
                
                return reply
                
        except httpx.TimeoutException:
            logger.error("Gateway call timeout")
            return "⏳ Task processing timeout, please retry later or simplify your request"
        except httpx.HTTPStatusError as e:
            logger.error("Gateway returned error: %d - %s", e.response.status_code, e.response.text, exc_info=True)
            return f"⚠️ Service temporarily unavailable (HTTP {e.response.status_code})"
        except Exception as e:
            logger.error("Gateway call failed: %s", e, exc_info=True)
            return f"❌ Failed to connect to service, please retry later"


def load_config() -> dict:
    """Load DingTalk configuration from config file"""
    from pyclaw.config.paths import get_paths as _get_paths
    config_path = str(_get_paths().config_file)
    
    if not os.path.exists(config_path):
        logger.error("Config file not found: %s", config_path)
        return {}
    
    with open(config_path, "r") as f:
        config = json.load(f)
    
    channels = config.get("channels", {})
    dingtalk = channels.get("dingtalk-connector", {})
    gateway = config.get("gateway", {})
    
    return {
        "client_id": dingtalk.get("clientId", ""),
        "client_secret": dingtalk.get("clientSecret", ""),
        "gateway_url": f"http://{gateway.get('host', '127.0.0.1')}:{gateway.get('port', 18789)}",
        "gateway_token": dingtalk.get("gatewayToken") or gateway.get("auth", {}).get("token", ""),
        "request_timeout_seconds": dingtalk.get("requestTimeout", 960000) // 1000,
    }


def main():
    """Main function"""
    # Set SSL certificates
    os.environ['SSL_CERT_FILE'] = certifi.where()
    os.environ['REQUESTS_CA_BUNDLE'] = certifi.where()
    
    # Set default SSL context
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    
    # Load configuration
    config = load_config()
    
    client_id = config.get("client_id") or os.environ.get("DINGTALK_CLIENT_ID", "")
    client_secret = config.get("client_secret") or os.environ.get("DINGTALK_CLIENT_SECRET", "")
    gateway_url = config.get("gateway_url", DEFAULT_GATEWAY_URL)
    gateway_token = config.get("gateway_token", "")
    
    if not client_id or not client_secret:
        logger.error("Please configure DingTalk clientId and clientSecret")
        logger.error("Can be configured in ~/.pyclaw/pyclaw.json under channels.dingtalk-connector")
        logger.error("Or set environment variables: DINGTALK_CLIENT_ID, DINGTALK_CLIENT_SECRET")
        sys.exit(1)
    
    logger.info("=" * 60)
    logger.info("PyClaw DingTalk Stream Client")
    logger.info("=" * 60)
    logger.info("Client ID: %s...", client_id[:10])
    logger.info("Gateway URL: %s", gateway_url)
    logger.info("=" * 60)
    
    # Create credential
    credential = Credential(client_id, client_secret)
    
    # Create message handler
    request_timeout_seconds = config.get("request_timeout_seconds", 960)
    handler = PyClawChatbotHandler(
        gateway_url=gateway_url,
        gateway_token=gateway_token,
        request_timeout_seconds=request_timeout_seconds,
    )
    
    # Create Stream client
    client = DingTalkStreamClient(credential)
    
    # Register bot message handler (using ChatbotMessage.TOPIC)
    client.register_callback_handler(ChatbotMessage.TOPIC, handler)
    
    logger.info(f"Registered callback topic: {ChatbotMessage.TOPIC}")
    logger.info("Connecting to DingTalk Stream service...")
    
    # Start client (with auto-reconnect mechanism)
    max_retries = 10
    retry_count = 0
    retry_delay = 5  # seconds
    
    while retry_count < max_retries:
        try:
            logger.info("Starting Stream client...")
            client.start_forever()
        except Exception as e:
            retry_count += 1
            logger.warning("Stream connection lost (retry %d/%d): %s", retry_count, max_retries, e, exc_info=True)
            if retry_count < max_retries:
                logger.info(f"Reconnecting in {retry_delay} seconds...")
                time.sleep(retry_delay)
                # Recreate client
                credential = Credential(client_id, client_secret)
                client = DingTalkStreamClient(credential)
                client.register_callback_handler(ChatbotMessage.TOPIC, handler)
            else:
                logger.error("Max retries reached, exiting")
                raise


async def send_cron_message(message: str) -> bool:
    """Send scheduled task message to all cached webhooks
    
    This function is called by the scheduled task dispatcher and sends messages to all recently active users.
    
    Args:
        message: Message content to send
        
    Returns:
        Whether successfully sent
    """
    import httpx
    
    # Use global instance
    handler = PyClawChatbotHandler._instance
    webhooks = {}
    
    # If global instance exists, get webhooks from instance
    if handler:
        webhooks = handler._webhooks
    else:
        # Otherwise load from file
        from pyclaw.config.paths import get_paths as _get_paths
        webhooks_file = _get_paths().dingtalk_webhooks_file
        if webhooks_file.exists():
            try:
                webhooks = json.loads(webhooks_file.read_text())
            except Exception as e:
                logger.warning("Failed to load webhook cache: %s", e, exc_info=True)
    
    if not webhooks:
        logger.warning("No cached webhooks, cannot send scheduled task message")
        return False
    
    # Send message to all cached webhooks
    success_count = 0
    for user_id, webhook_url in webhooks.items():
        try:
            payload = {
                "msgtype": "text",
                "text": {"content": message}
            }
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(webhook_url, json=payload)
                if resp.status_code == 200:
                    result = resp.json()
                    if result.get("errcode") == 0:
                        logger.info("Scheduled task message sent to user %s", user_id)
                        success_count += 1
                    else:
                        logger.warning("Send failed %s: %s", user_id, result, exc_info=True)
                else:
                    logger.warning("Webhook request failed %s: %d", user_id, resp.status_code, exc_info=True)
        except Exception as e:
            logger.warning("Failed to send message to %s: %s", user_id, e, exc_info=True)
    
    logger.info("Scheduled task message sending completed: %d/%d successful", success_count, len(webhooks))
    return success_count > 0


if __name__ == "__main__":
    while True:
        try:
            main()
        except KeyboardInterrupt:
            logger.info("Received interrupt signal, exiting...")
            break
        except Exception as e:
            logger.error("Runtime error: %s, restarting in 5 seconds...", e, exc_info=True)
            time.sleep(5)
