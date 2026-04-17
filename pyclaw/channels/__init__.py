"""
PyClaw channel system

Provides unified message channel interface, supporting multiple instant messaging platforms.
"""

from pyclaw.channels.base import (
    ChannelAdapter,
    InboundMessage,
    MessageHandler,
    OutboundMessage,
)
from pyclaw.channels.manager import ChannelManager
from pyclaw.channels.registry import ChannelRegistry

__all__ = [
    "ChannelAdapter",
    "ChannelManager",
    "ChannelRegistry",
    "InboundMessage",
    "OutboundMessage",
]
