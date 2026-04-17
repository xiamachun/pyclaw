"""
Channel system base module

Defines message models and channel adapter abstract base class.
"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Awaitable, Callable

from pydantic import BaseModel, Field


class InboundMessage(BaseModel):
    """Inbound message model"""

    channel_id: str = Field(..., description="Channel ID")
    peer_id: str = Field(..., description="Sender ID")
    peer_name: str = Field(..., description="Sender name")
    content: str = Field(..., description="Message content")
    media_urls: list[str] = Field(default_factory=list, description="Media URL list")
    timestamp: datetime = Field(default_factory=datetime.utcnow, description="Message timestamp")
    raw_data: dict[str, Any] = Field(default_factory=dict, description="Raw data")
    thread_id: str | None = Field(None, description="Thread ID, for threaded conversations in group chats")


class OutboundMessage(BaseModel):
    """Outbound message model"""

    content: str = Field(..., description="Message content")
    media_urls: list[str] = Field(default_factory=list, description="Media URL list")
    reply_to: str | None = Field(None, description="Message ID to reply to")


MessageHandler = Callable[[InboundMessage], Awaitable[None]]


class ChannelAdapter(ABC):
    """Channel adapter abstract base class"""

    @property
    @abstractmethod
    def channel_id(self) -> str:
        """Channel unique identifier"""
        pass

    @abstractmethod
    async def connect(self) -> None:
        """Connect channel"""
        pass

    @abstractmethod
    async def disconnect(self) -> None:
        """Disconnect channel"""
        pass

    @abstractmethod
    async def send_message(self, target: str, message: OutboundMessage) -> None:
        """Send message

        Args:
            target: Target user or group ID
            message: Outbound message
        """
        pass

    @abstractmethod
    async def on_message(self, handler: MessageHandler) -> None:
        """Register message handler

        Args:
            handler: Message handler callback function
        """
        pass

    @abstractmethod
    async def health_check(self) -> bool:
        """Health check

        Returns:
            Whether the channel is healthy
        """
        pass