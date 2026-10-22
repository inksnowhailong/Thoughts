"""主动环（proactive ring）：身份感知 / 触发 / 生成 / 边界的共享配置与工具。

本包承载管家 v2 主动化升级（议会定稿 feat/core-add-proactive-butler）的公共设施，
视觉链路地基批先落配置与隐私降级器。
"""

from guanjia.proactive.config import ProactiveConfig
from guanjia.proactive.privacy import PrivacyDownscaler

__all__ = ['ProactiveConfig', 'PrivacyDownscaler']
