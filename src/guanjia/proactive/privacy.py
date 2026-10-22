"""裸体图像降级·组合路（议会定稿）：采集源头生效的 downscale 管线。

两级语义（发起人裁决 14 原意）：
  基础级 = 人框区域无条件轻度降像素（"倾向把人做模糊"）
  升级级 = 状态规则（待命/夜间/离开沿后 N 分钟）触发深度降（躯干深降、头肩保轻）

用词定义（SX-14）：降级 = downscale（缩放后放回原尺寸，保留轮廓/主色等低频特征），
不是 blur；身形、衣着主色、发长等性别判断信号保留（QC-23 区域策略）。

采集源头生效（YH-11）：调用方在保存/外发之前调用本管线，
原图只在内存短暂存在，落盘/入日志/外发的都应是返回值。
"""

from __future__ import annotations

from PIL import Image

# 头肩/躯干分界：人框自上而下 45% 处，上半保留轻度（性别信号），下半深度降
_TORSO_SPLIT_RATIO = 0.45


class PrivacyDownscaler:
    """人框区域降像素器；纯本地像素操作，零模型、零网络。"""

    def __init__(self, light_scale: float = 0.5, deep_scale: float = 0.125) -> None:
        self._light_scale = float(light_scale)
        self._deep_scale = float(deep_scale)

    @property
    def light_scale(self) -> float:
        return self._light_scale

    @property
    def deep_scale(self) -> float:
        return self._deep_scale

    def apply(
        self,
        image: Image.Image,
        box: tuple[int, int, int, int] | None,
        *,
        deep: bool = False,
    ) -> Image.Image:
        """对图像做人框降级并返回新图；box 为像素坐标 (left, top, right, bottom)。

        - box=None 且无人：整帧轻度降一级（"所有送出帧默认降一级"的保底）。
        - box 给定：人框轻度降；deep=True 时躯干（下半）用深度比例、头肩保轻度。
        """
        result = image.copy()
        if box is None:
            return self._downscale(result, (0, 0, result.width, result.height), self._light_scale)
        left, top, right, bottom = self._clamp(box, result.width, result.height)
        if right - left < 8 or bottom - top < 8:
            return result
        region = result.crop((left, top, right, bottom))
        if not deep:
            light = self._downscale(region, (0, 0, region.width, region.height), self._light_scale)
            result.paste(light, (left, top))
            return result
        # 深度层：头肩轻度 + 躯干深度（保性别判断线，抹躯干细节）
        split = int(region.height * _TORSO_SPLIT_RATIO)
        upper = region.crop((0, 0, region.width, split))
        lower = region.crop((0, split, region.width, region.height))
        upper = self._downscale(upper, (0, 0, upper.width, upper.height), self._light_scale)
        lower = self._downscale(lower, (0, 0, lower.width, lower.height), self._deep_scale)
        merged = Image.new('RGB', region.size)
        merged.paste(upper, (0, 0))
        merged.paste(lower, (0, split))
        result.paste(merged, (left, top))
        return result

    def _downscale(self, image: Image.Image, box: tuple[int, int, int, int], scale: float) -> Image.Image:
        """downscale 核心操作：缩到 scale 再放回原尺寸——保留轮廓/主色，抹掉细节纹理。"""
        left, top, right, bottom = box
        region = image.crop(box)
        small_w = max(1, int(region.width * scale))
        small_h = max(1, int(region.height * scale))
        small = region.resize((small_w, small_h), Image.Resampling.BILINEAR)
        restored = small.resize((region.width, region.height), Image.Resampling.BILINEAR)
        out = image.copy()
        out.paste(restored, (left, top))
        return out

    @staticmethod
    def _clamp(box: tuple[int, int, int, int], width: int, height: int) -> tuple[int, int, int, int]:
        left, top, right, bottom = box
        left = max(0, min(width - 1, int(left)))
        top = max(0, min(height - 1, int(top)))
        right = max(left + 1, min(width, int(right)))
        bottom = max(top + 1, min(height, int(bottom)))
        return left, top, right, bottom


__all__ = ['PrivacyDownscaler']
