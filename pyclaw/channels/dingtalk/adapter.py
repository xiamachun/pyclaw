"""
DingTalk channel adapter

Supports DingTalk bot and message callbacks.
"""

import hashlib
import hmac
import json
import time
from typing import Any

import httpx
from pydantic import BaseModel

from pyclaw.channels.base import ChannelAdapter, InboundMessage, MessageHandler, OutboundMessage


class DingTalkChannelConfig(BaseModel):
    """DingTalk channel configuration"""

    app_key: str
    app_secret: str
    webhook_url: str | None = None
    callback_url: str | None = None


class DingTalkAdapter(ChannelAdapter):
    """DingTalk channel adapter"""

    channel_id = "dingtalk"
    _ACCESS_TOKEN_URL = "https://oapi.dingtalk.com/gettoken"
    _SEND_MESSAGE_URL = "https://oapi.dingtalk.com/topapi/message/corpconversation/asyncsend_v2"

    def __init__(self, config: DingTalkChannelConfig) -> None:
        self.config = config
        self._access_token: str | None = None
        self._message_handler: MessageHandler | None = None
        self._connected = False
        self._token_expires_at: float = 0
        self._http_client: httpx.AsyncClient | None = None

    @property
    def channel_id(self) -> str:
        return "dingtalk"

    async def connect(self) -> None:
        """Connect to DingTalk API"""
        self._http_client = httpx.AsyncClient(timeout=30.0)
        await self._get_access_token()
        self._connected = True

    async def disconnect(self) -> None:
        """Disconnect"""
        self._access_token = None
        self._connected = False
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None

    async def send_message(self, target: str, message: OutboundMessage) -> None:
        """Send DingTalk message

        Args:
            target: Target user or group ID
            message: Outbound message
        """
        if not self._connected:
            raise RuntimeError("DingTalk adapter is not connected")

        if self._http_client is None:
            raise RuntimeError("HTTP client not initialized")

        # Ensure access token is valid
        if self._access_token is None or time.time() >= self._token_expires_at:
            await self._get_access_token()

        # Build message body
        msg_content = {
            "msg": {
                "msgtype": "text",
                "text": {"content": message.content},
            }
        }

        if message.media_urls:
            msg_content["msg"]["msgtype"] = "markdown"
            msg_content["msg"]["markdown"] = {
                "title": "Message",
                "text": message.content + "\n\n" + "\n".join(message.media_urls),
            }

        # Send request
        params = {"access_token": self._access_token}
        payload = {
            "agent_id": self.config.app_key,
            "userid_list": target,
            "msg": msg_content["msg"],
        }

        try:
            response = await self._http_client.post(
                self._SEND_MESSAGE_URL,
                params=params,
                json=payload,
            )
            response.raise_for_status()
            result = response.json()

            if result.get("errcode") != 0:
                raise RuntimeError(f"DingTalk API error: {result.get('errmsg')}")
        except httpx.HTTPError as e:
            raise RuntimeError(f"Failed to send DingTalk message: {e}")

    async def on_message(self, handler: MessageHandler) -> None:
        """Register message handler

        Args:
            handler: Message handler callback function
        """
        self._message_handler = handler

    async def health_check(self) -> bool:
        """Health check

        Returns:
            Whether the channel is healthy
        """
        if not self._connected or self._access_token is None or self._http_client is None:
            return False

        # Check if token is expired
        if time.time() >= self._token_expires_at:
            try:
                await self._get_access_token()
            except Exception:
                return False

        return True

    async def _get_access_token(self) -> str:
        """Get DingTalk access token

        Returns:
            Access token
        """
        if self._http_client is None:
            raise RuntimeError("HTTP client not initialized")

        params = {
            "appkey": self.config.app_key,
            "appsecret": self.config.app_secret,
        }

        try:
            response = await self._http_client.get(
                self._ACCESS_TOKEN_URL,
                params=params,
            )
            response.raise_for_status()
            result = response.json()

            if result.get("errcode") != 0:
                raise RuntimeError(f"Failed to get access token: {result.get('errmsg')}")

            self._access_token = result.get("access_token")
            # access_token valid for 7200 seconds, refresh 300 seconds early
            self._token_expires_at = time.time() + 7200 - 300

            return self._access_token
        except httpx.HTTPError as e:
            raise RuntimeError(f"Failed to get DingTalk access token: {e}")

    async def _handle_callback(self, data: dict[str, Any]) -> None:
        """Handle DingTalk callback

        Args:
            data: Callback data
        """
        if self._message_handler is None:
            return

        # Parse callback data
        try:
            content = data.get("text", {}).get("content", "")
            sender_id = data.get("senderId", "")
            sender_name = data.get("senderNick", "")

            inbound_message = InboundMessage(
                channel_id=self.channel_id,
                peer_id=sender_id,
                peer_name=sender_name,
                content=content,
                raw_data=data,
            )

            await self._message_handler(inbound_message)
        except Exception as e:
            raise RuntimeError(f"Failed to handle DingTalk callback: {e}")

    def verify_callback_signature(self, timestamp: str, nonce: str, sign: str) -> bool:
        """Verify callback signature

        Args:
            timestamp: Timestamp
            nonce: Random number
            sign: Signature

        Returns:
            Whether signature is valid
        """
        string_to_sign = f"{timestamp}\n{nonce}\n{self.config.app_secret}"
        hmac_obj = hmac.new(
            self.config.app_secret.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        )
        expected_sign = hmac_obj.digest().hex()

        return sign == expected_sign