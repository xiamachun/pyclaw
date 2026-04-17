"""
Telegram channel adapter

Implements message receiving and sending using Telegram Bot API + Long Polling.
"""

import asyncio
import json
import logging
import time
from typing import Any, Optional

import httpx
from pydantic import BaseModel, Field, SecretStr

from pyclaw.channels.base import ChannelAdapter, InboundMessage, MessageHandler, OutboundMessage
from pyclaw.constants import DEFAULT_GATEWAY_URL

logger = logging.getLogger(__name__)


class TelegramConfig(BaseModel):
    """Telegram configuration"""

    bot_token: SecretStr = Field(..., description="Telegram Bot Token")
    webhook_url: Optional[str] = Field(None, description="Webhook URL (optional)")
    gateway_url: str = Field(
        default=DEFAULT_GATEWAY_URL,
        description="Gateway URL"
    )
    gateway_token: Optional[str] = Field(None, description="Gateway authentication token")


class TelegramAdapter(ChannelAdapter):
    """Telegram channel adapter

    Receives messages using Telegram Bot API Long Polling mode, sends messages via Bot API.
    """

    _channel_id = "telegram"
    _BASE_URL = "https://api.telegram.org/bot{token}"
    _POLLING_TIMEOUT = 30

    def __init__(self, config: TelegramConfig) -> None:
        self.config = config
        self._message_handler: Optional[MessageHandler] = None
        self._connected = False
        self._http_client: Optional[httpx.AsyncClient] = None
        self._sessions: dict[str, dict] = {}
        self._polling_task: Optional[asyncio.Task] = None
        self._last_update_id: int = 0

    @property
    def channel_id(self) -> str:
        return self._channel_id

    async def connect(self) -> None:
        """Connect to Telegram Bot API"""
        self._http_client = httpx.AsyncClient(timeout=60.0)
        self._connected = True

        # Start polling task
        self._polling_task = asyncio.create_task(self._polling_loop())

        logger.info("Telegram Bot connected")

    async def disconnect(self) -> None:
        """Disconnect Telegram Bot API"""
        self._connected = False

        if self._polling_task:
            self._polling_task.cancel()
            try:
                await self._polling_task
            except asyncio.CancelledError:
                pass
            self._polling_task = None

        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None

        logger.info("Telegram Bot disconnected")

    async def send_message(self, target: str, message: OutboundMessage) -> None:
        """Send message

        Args:
            target: Target chat ID
            message: Outbound message
        """
        if not self._connected:
            raise RuntimeError("Telegram channel not connected")

        url = f"{self._BASE_URL.format(token=self.config.bot_token.get_secret_value())}/sendMessage"

        payload = {
            "chat_id": target,
            "text": message.content,
            "parse_mode": "Markdown"
        }

        # If it's a reply message
        if message.reply_to:
            payload["reply_to_message_id"] = message.reply_to

        response = await self._http_client.post(url, json=payload)
        response.raise_for_status()
        result = response.json()

        if not result.get("ok"):
            raise RuntimeError(f"Failed to send Telegram message: {result.get('description')}")
        logger.debug(f"Telegram message sent successfully: {target}")

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

    async def _polling_loop(self) -> None:
        """Long Polling message loop"""
        try:
            while self._connected:
                await self._get_updates()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Telegram polling error: {e}", exc_info=True)
            self._connected = False

    async def _get_updates(self) -> None:
        """Get updates"""
        url = f"{self._BASE_URL.format(token=self.config.bot_token.get_secret_value())}/getUpdates"

        params = {
            "timeout": self._POLLING_TIMEOUT,
            "offset": self._last_update_id + 1
        }

        response = await self._http_client.get(url, params=params)
        response.raise_for_status()
        result = response.json()

        if not result.get("ok"):
            logger.warning(f"Failed to get Telegram updates: {result.get('description')}")
            return

        updates = result.get("result", [])
        for update in updates:
            await self._handle_update(update)
            self._last_update_id = update.get("update_id", self._last_update_id)

    async def _handle_update(self, update: dict[str, Any]) -> None:
        """Handle update"""
        if not self._message_handler:
            return

        # Handle message
        if "message" in update:
            await self._handle_message(update["message"])

        # Handle callback query
        elif "callback_query" in update:
            await self._handle_callback_query(update["callback_query"])

    async def _handle_message(self, message: dict[str, Any]) -> None:
        """Handle message"""
        # Only process text messages
        if "text" not in message:
            return

        # Ignore bot's own messages
        if message.get("from", {}).get("is_bot"):
            return

        # Extract message content
        text = message.get("text", "").strip()
        if not text:
            return

        # Extract user information
        user = message.get("from", {})
        chat = message.get("chat", {})
        message_id = message.get("message_id")

        # Build inbound message
        inbound = InboundMessage(
            channel_id=self._channel_id,
            peer_id=str(user.get("id", "")),
            peer_name=user.get("username", user.get("first_name", "")),
            content=text,
            raw_data=message
        )

        # Add message ID for reply
        inbound.raw_data["message_id"] = str(message_id)

        await self._message_handler(inbound)

    async def _handle_callback_query(self, callback_query: dict[str, Any]) -> None:
        """Handle callback query"""
        # Answer callback query
        url = f"{self._BASE_URL.format(token=self.config.bot_token.get_secret_value())}/answerCallbackQuery"

        payload = {
            "callback_query_id": callback_query.get("id"),
            "text": "Received"
        }

        await self._http_client.post(url, json=payload)