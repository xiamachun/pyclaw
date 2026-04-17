"""
DingTalk channel adapter
"""

from pyclaw.channels.dingtalk.adapter import DingTalkAdapter, DingTalkChannelConfig
from pyclaw.channels.dingtalk.stream_adapter import DingTalkStreamAdapter, DingTalkStreamConfig

__all__ = [
    "DingTalkAdapter",
    "DingTalkChannelConfig",
    "DingTalkStreamAdapter",
    "DingTalkStreamConfig",
]
