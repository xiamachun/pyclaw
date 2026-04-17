"""
Feishu/Lark channel adapter

Implements message receiving and sending using Feishu event subscription mode.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import time
from typing import Any, Optional

import httpx
from pydantic import BaseModel, Field, SecretStr

from pyclaw.channels.base import ChannelAdapter, InboundMessage, MessageHandler, OutboundMessage
from pyclaw.constants import DEFAULT_GATEWAY_URL

logger = logging.getLogger(__name__)


class FeishuConfig(BaseModel):
    """Feishu configuration"""

    app_id: str = Field(..., description="Feishu app ID")
    app_secret: SecretStr = Field(..., description="Feishu app secret")
    verification_token: str = Field(..., description="Event subscription verification token")
    encrypt_key: str = Field("", description="Event encryption key (optional)")
    gateway_url: str = Field(
        default=DEFAULT_GATEWAY_URL,
        description="Gateway URL"
    )
    gateway_token: Optional[str] = Field(None, description="Gateway authentication token")


class FeishuAdapter(ChannelAdapter):
    """Feishu channel adapter

    Receives messages via Feishu event subscription, sends messages via Feishu Open API.
    """

    _channel_id = "feishu"
    _TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    _SEND_MESSAGE_URL = "https://open.feishu.cn/open-apis/im/v1/messages"
    _REPLY_MESSAGE_URL = "https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reply"

    def __init__(self, config: FeishuConfig) -> None:
        self.config = config
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0
        self._message_handler: Optional[MessageHandler] = None
        self._connected = False
        self._http_client: Optional[httpx.AsyncClient] = None
        self._sessions: dict[str, dict] = {}

    @property
    def channel_id(self) -> str:
        return self._channel_id

    async def connect(self) -> None:
        """Connect to Feishu channel"""
        self._http_client = httpx.AsyncClient(timeout=30.0)
        await self._ensure_token()
        self._connected = True
        logger.info("Feishu channel connected")

    async def disconnect(self) -> None:
        """Disconnect Feishu channel"""
        self._connected = False
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None
        logger.info("Feishu channel disconnected")

    async def send_message(self, target: str, message: OutboundMessage) -> None:
        """Send message

        Args:
            target: Target user or group ID
            message: Outbound message
        """
        if not self._connected:
            raise RuntimeError("Feishu channel not connected")

        await self._ensure_token()

        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json"
        }

        # If it's a reply message
        if message.reply_to:
            url = self._REPLY_MESSAGE_URL.format(message_id=message.reply_to)
            payload = {
                "msg_type": "text",
                "content": json.dumps({"text": message.content})
            }
        else:
            url = self._SEND_MESSAGE_URL
            payload = {
                "receive_id": target,
                "msg_type": "text",
                "content": json.dumps({"text": message.content})
            }

        response = await self._http_client.post(url, json=payload, headers=headers)
        response.raise_for_status()
        logger.debug(f"Feishu message sent successfully: {target}")

    async def on_message(self, handler: MessageHandler) -> None:
        """Register message handler

        Args:
            handler: Message processing callback function
        """
        self._message_handler = handler

    async def health_check(self) -> bool:
        """Health check

        Returns:
            Whether the channel is healthy
        """
        return self._connected and self._http_client is not None

    async def _ensure_token(self) -> None:
        """Ensure access_token is valid"""
        if self._access_token and time.time() < self._token_expires_at:
            return

        await self._refresh_access_token()

    async def _refresh_access_token(self) -> None:
        """Refresh access_token"""
        payload = {
            "app_id": self.config.app_id,
            "app_secret": self.config.app_secret.get_secret_value()
        }

        response = await self._http_client.post(self._TOKEN_URL, json=payload)
        response.raise_for_status()
        data = response.json()

        if data.get("code") != 0:
            raise RuntimeError(f"Failed to get Feishu token: {data.get('msg')}")

        self._access_token = data.get("tenant_access_token")
        self._token_expires_at = time.time() + data.get("expire", 7200) - 60
        logger.debug("Feishu access_token refreshed")

    async def handle_event(self, event_data: dict[str, Any]) -> Optional[str]:
        """Handle Feishu event

        Args:
            event_data: Event data

        Returns:
            Processing result, used for url_verification response
        """
        event_type = event_data.get("type")

        # URL verification challenge
        if event_type == "url_verification":
            return event_data.get("challenge")

        # Message event
        if event_type == "event_callback":
            event = event_data.get("event", {})
            if event.get("type") == "message":
                await self._handle_message_event(event)

        return None

    async def _handle_message_event(self, event: dict[str, Any]) -> None:
        """Handle message event"""
        if not self._message_handler:
            return

        # Only process text messages
        if event.get("msg_type") != "text":
            return

        # Ignore bot's own messages
        sender = event.get("sender", {})
        if sender.get("type") == "app":
            return

        # Extract message content
        content = json.loads(event.get("content", "{}"))
        text = content.get("text", "").strip()

        if not text:
            return

        # Build inbound message
        inbound = InboundMessage(
            channel_id=self._channel_id,
            peer_id=sender.get("sender_id", {}).get("open_id", ""),
            peer_name=sender.get("sender_id", {}).get("open_id", ""),
            content=text,
            timestamp=event.get("create_time", 0),
            raw_data=event
        )

        await self._message_handler(inbound)

    def verify_signature(self, timestamp: str, nonce: str, body: str, signature: str) -> bool:
        """Verify event signature

        Args:
            timestamp: Timestamp
            nonce: Random number
            body: Request body
            signature: Signature

        Returns:
            Whether the signature is valid
        """
        if not self.config.encrypt_key:
            return True

        sign_str = f"{timestamp}{nonce}{body}"
        hmac_obj = hmac.new(
            self.config.encrypt_key.encode(),
            sign_str.encode(),
            hashlib.sha256
        )
        calculated = hmac_obj.hexdigest()
        return calculated == signature