"""Passive input edge adapters with no production-chain dependencies."""

from .contracts import InputShadowSink, ShadowInputRecord
from .environment import EnvironmentReaders, EnvironmentShadowAdapter, EnvironmentShadowAsyncAdapter
from .voice import AcceptedInputIssuer, AcceptedVoiceSegment, VoiceShadowAdapter
from .wechat import WeChatInboundMessage, WeChatShadowAdapter

__all__ = [
    'AcceptedInputIssuer',
    'AcceptedVoiceSegment',
    'EnvironmentReaders',
    'EnvironmentShadowAdapter',
    'EnvironmentShadowAsyncAdapter',
    'InputShadowSink',
    'ShadowInputRecord',
    'VoiceShadowAdapter',
    'WeChatInboundMessage',
    'WeChatShadowAdapter',
]
