"""
Channel registry

Manages all available channel adapters.
"""

from typing import Any

from pydantic import BaseModel, Field

from pyclaw.channels.base import ChannelAdapter


class ChannelMeta(BaseModel):
    """Channel metadata"""

    channel_id: str = Field(..., description="Channel ID")
    label: str = Field(..., description="Channel label")
    description: str = Field(..., description="Channel description")
    docs_url: str | None = Field(None, description="Documentation URL")


class ChannelRegistry:
    """Channel registry"""

    def __init__(self) -> None:
        self._adapters: dict[str, type[ChannelAdapter]] = {}

    def register(self, channel_id: str, adapter_class: type[ChannelAdapter]) -> None:
        """Register channel adapter

        Args:
            channel_id: Channel ID
            adapter_class: Adapter class
        """
        self._adapters[channel_id] = adapter_class

    def get(self, channel_id: str) -> ChannelAdapter | None:
        """Get channel adapter instance

        Args:
            channel_id: Channel ID

        Returns:
            Channel adapter instance or None
        """
        adapter_class = self._adapters.get(channel_id)
        if adapter_class is None:
            return None

        return adapter_class()

    def list_registered(self) -> list[ChannelMeta]:
        """List all registered channels

        Returns:
            List of channel metadata
        """
        result = []
        for channel_id, adapter_class in self._adapters.items():
            label = channel_id
            description = getattr(adapter_class, "__doc__", None) or ""
            docs_url = getattr(adapter_class, "_docs_url", None)
            result.append(
                ChannelMeta(
                    channel_id=channel_id,
                    label=label,
                    description=description,
                    docs_url=docs_url,
                )
            )
        return result
