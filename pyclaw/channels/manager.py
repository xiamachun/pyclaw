"""
Channel manager

Manages lifecycle and message routing of all channels.
"""

from typing import Any

from pyclaw.channels.base import ChannelAdapter, MessageHandler, OutboundMessage
from pyclaw.channels.registry import ChannelRegistry


class ChannelManager:
    """Channel manager"""

    def __init__(self, config: dict[str, Any], registry: ChannelRegistry) -> None:
        self.config = config
        self.registry = registry
        self._active_channels: dict[str, ChannelAdapter] = {}
        self._message_handler: MessageHandler | None = None

    async def start_channels(self) -> None:
        """Start all enabled channels"""
        enabled_channels = self.config.get("enabled_channels", [])

        for channel_id in enabled_channels:
            adapter_class = self.registry._adapters.get(channel_id)
            if adapter_class is None:
                continue

            channel_config = self.config.get("channels", {}).get(channel_id, {})
            adapter = adapter_class(channel_config)
            await adapter.connect()

            if self._message_handler:
                await adapter.on_message(self._message_handler)

            self._active_channels[channel_id] = adapter

    async def stop_channels(self) -> None:
        """Stop all channels"""
        for channel_id, adapter in self._active_channels.items():
            await adapter.disconnect()

        self._active_channels.clear()

    async def get_channel_status(self) -> dict[str, Any]:
        """Get status of all channels

        Returns:
            Channel status dictionary
        """
        status = {}

        for channel_id, adapter in self._active_channels.items():
            is_healthy = await adapter.health_check()
            status[channel_id] = {
                "connected": True,
                "healthy": is_healthy,
            }

        return status

    async def send(self, channel_id: str, target: str, message: OutboundMessage) -> None:
        """Send message to specified channel

        Args:
            channel_id: Channel ID
            target: Target user or group ID
            message: Outbound message
        """
        adapter = self._active_channels.get(channel_id)
        if adapter is None:
            raise ValueError(f"Channel {channel_id} not found or not active")

        await adapter.send_message(target, message)

    def set_message_handler(self, handler: MessageHandler) -> None:
        """Set global message handler

        Args:
            handler: Message handler callback function
        """
        self._message_handler = handler
