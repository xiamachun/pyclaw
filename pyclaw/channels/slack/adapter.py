"""
Slack channel adapter

Implements real-time message push using Slack Socket Mode.
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


class SlackConfig(BaseModel):
    """Slack configuration"""

    bot_token: SecretStr = Field(..., description="Bot Token (xoxb-...)")
    app_token: SecretStr = Field(..., description="App-Level Token (xapp-...)")
    signing_secret: str = Field(..., description="Signing Secret")
    gateway_url: str = Field(
        default=DEFAULT_GATEWAY_URL,
        description="Gateway URL"
    )
    gateway_token: Optional[str] = Field(None, description="Gateway authentication token")


class SlackAdapter(ChannelAdapter):
    """Slack channel adapter

    Receives messages using Slack Socket Mode, sends messages via Slack Web API.
    """

    _channel_id = "slack"
    _WSS_URL = "wss://wss-primary.slack.com/socket-mode/"
    _SEND_MESSAGE_URL = "https://slack.com/api/chat.postMessage"
    _REPLY_MESSAGE_URL = "https://slack.com/api/chat.postMessage"

    def __init__(self, config: SlackConfig) -> None:
        self.config = config
        self._message_handler: Optional[MessageHandler] = None
        self._connected = False
        self._http_client: Optional[httpx.AsyncClient] = None
        self._websocket: Optional[Any] = None
        self._sessions: dict[str, dict] = {}
        self._socket_task: Optional[asyncio.Task] = None

    @property
    def channel_id(self) -> str:
        return self._channel_id

    async def connect(self) -> None:
        """Connect to Slack Socket Mode"""
        self._http_client = httpx.AsyncClient(timeout=30.0)
        await self._connect_socket()
        self._connected = True
        logger.info("Slack Socket Mode connected")

    async def disconnect(self) -> None:
        """Disconnect Slack Socket Mode"""
        self._connected = False

        if self._socket_task:
            self._socket_task.cancel()
            try:
                await self._socket_task
            except asyncio.CancelledError:
                pass
            self._socket_task = None

        if self._websocket:
            await self._websocket.close()
            self._websocket = None

        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None

        logger.info("Slack Socket Mode disconnected")

    async def send_message(self, target: str, message: OutboundMessage) -> None:
        """Send message

        Args:
            target: Target channel ID
            message: Outbound message
        """
        if not self._connected:
            raise RuntimeError("Slack channel not connected")

        headers = {
            "Authorization": f"Bearer {self.config.bot_token.get_secret_value()}",
            "Content-Type": "application/json"
        }

        payload = {
            "channel": target,
            "text": message.content
        }

        # If it's a reply message
        if message.reply_to:
            payload["thread_ts"] = message.reply_to

        response = await self._http_client.post(self._SEND_MESSAGE_URL, json=payload, headers=headers)
        response.raise_for_status()
        result = response.json()

        if not result.get("ok"):
            raise RuntimeError(f"Failed to send Slack message: {result.get('error')}")
        logger.debug(f"Slack message sent successfully: {target}")

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
        return self._connected and self._websocket is not None

    async def _connect_socket(self) -> None:
        """Connect to Slack WebSocket"""
        import websockets

        # First get WSS URL
        headers = {
            "Authorization": f"Bearer {self.config.app_token.get_secret_value()}",
            "Content-Type": "application/json"
        }

        payload = {"app_token": self.config.app_token.get_secret_value()}

        response = await self._http_client.post(
            "https://slack.com/api/apps.connections.open",
            headers=headers,
            json=payload
        )
        response.raise_for_status()
        result = response.json()

        if not result.get("ok"):
            raise RuntimeError(f"Failed to open Slack Socket Mode: {result.get('error')}")

        wss_url = result.get("url")
        logger.info(f"Connecting to Slack WebSocket: {wss_url}")

        # Connect WebSocket
        self._websocket = await websockets.connect(wss_url)

        # Start message processing task
        self._socket_task = asyncio.create_task(self._socket_loop())

    async def _socket_loop(self) -> None:
        """WebSocket message loop"""
        try:
            async for message in self._websocket:
                data = json.loads(message)
                await self._handle_socket_message(data)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Slack WebSocket error: {e}", exc_info=True)
            self._connected = False

    async def _handle_socket_message(self, data: dict[str, Any]) -> None:
        """Handle WebSocket message"""
        msg_type = data.get("type")

        # Handle event
        if msg_type == "events_api":
            envelope = data.get("envelope", {})
            payload = envelope.get("payload", {})
            event = payload.get("event", {})

            if event.get("type") == "message":
                await self._handle_message_event(event)

        # Acknowledge message
        if data.get("envelope_id"):
            await self._send_ack(data.get("envelope_id"))

    async def _send_ack(self, envelope_id: str) -> None:
        """Send message acknowledgment"""
        ack_msg = {
            "envelope_id": envelope_id,
            "payload": {
                "type": "ack"
            }
        }
        await self._websocket.send(json.dumps(ack_msg))

    async def _handle_message_event(self, event: dict[str, Any]) -> None:
        """Handle message event"""
        if not self._message_handler:
            return

        # Ignore bot's own messages
        if event.get("subtype") == "bot_message":
            return

        # Ignore messages without text
        text = event.get("text", "").strip()
        if not text:
            return

        # Extract user information
        user_id = event.get("user", "")
        channel_id = event.get("channel", "")
        ts = event.get("ts", "")

        # Build inbound message
        inbound = InboundMessage(
            channel_id=self._channel_id,
            peer_id=user_id,
            peer_name=user_id,
            content=text,
            raw_data=event
        )

        # Add thread information
        if event.get("thread_ts"):
            inbound.raw_data["thread_ts"] = event.get("thread_ts")

        await self._message_handler(inbound)