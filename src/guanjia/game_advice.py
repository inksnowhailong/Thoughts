"""游戏攻略后台 worker：flash 全程看图分析（发起人 2026-10-25 定稿）。

流程：
1. 主程序本地识别攻略意图，立即说"我看一下"，不调主脑。
2. worker 静默截屏，截图+问题一次发给 glm-5.3-flash（原生支持看图），
   单次调用直接返回攻略文本——不搞多阶段、不做模型切换。
3. 结果回到语音直接播报队列；usage 照常进 UsageGuard 记账。
"""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import logging
from pathlib import Path
import threading
from typing import Callable

import httpx
from PIL import Image, ImageGrab

from guanjia.config_hub import ConfigHub
from guanjia.usage_guard import UsageGuard

log = logging.getLogger(__name__)

_ROOT = Path(r'F:\coding\guanjia-v2-development')
_SCREEN_DIR = _ROOT / 'data' / 'vision' / 'screenshots'
_MODEL = 'glm-5.3-flash'

_ADVICE_PROMPT = """你是游戏实时教练。用户正在玩电脑游戏，向你提问，以下是当前屏幕截图。

用户问题：{question}

请先认出这是什么游戏，再基于截图实际可见内容给实用建议：
- 优先回答：当前该做什么、下一步目标、操作或资源上的关键提醒
- 只基于截图可见信息和该游戏的可靠知识，不编造看不到的内容
- 关键信息看不清时，先给能确定的建议，再提示用户补充
- 中文口语，2到4句，每句尽量短
"""


class GameAdviceWorker:
    """单 worker 串行处理攻略请求，避免连续截屏和重复调用。"""

    def __init__(self, usage_guard: UsageGuard, config: ConfigHub | None = None) -> None:
        self._usage_guard = usage_guard
        self._config = config or ConfigHub()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='game-advice')
        self._busy = threading.Lock()

    def submit(self, question: str, callback: Callable[[str], None]) -> bool:
        """提交后台任务；正在处理上一题时返回 False。"""
        if not self._busy.acquire(blocking=False):
            return False
        self._pool.submit(self._run, question, callback)
        return True

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _run(self, question: str, callback: Callable[[str], None]) -> None:
        try:
            screenshot = self._capture()
            advice, usage = self._analyze(screenshot, question)
            self._usage_guard.record(usage, model=_MODEL)
            callback(advice)
        except Exception:  # noqa: BLE001
            log.exception('game advice worker failed')
            callback('这次没看成，你再喊我一次。')
        finally:
            self._busy.release()

    def _capture(self) -> Path:
        """静默截取主屏幕。限制到 1600px 宽，兼顾 UI 小字与图片 token。"""
        image = ImageGrab.grab(all_screens=False).convert('RGB')
        if image.width > 1600:
            height = round(image.height * 1600 / image.width)
            image = image.resize((1600, height), Image.Resampling.LANCZOS)
        _SCREEN_DIR.mkdir(parents=True, exist_ok=True)
        name = f'game_{datetime.now():%Y%m%d_%H%M%S}.jpg'
        path = _SCREEN_DIR / name
        image.save(path, format='JPEG', quality=88)
        return path

    def _analyze(self, screenshot: Path, question: str) -> tuple[str, dict]:
        """一次多模态调用：flash 看图+分析+给攻略。

        必须流式（stream: true）：保险丝对非流式请求只有 25 秒首字窗口，
        看图分析可能超窗被误杀；流式首字节来得快，不会被熔断。
        thinking / reasoning_effort 不自带，由保险丝统一缺省注入（flash 强制开思考，默认 low）。
        """
        image_b64 = base64.b64encode(screenshot.read_bytes()).decode('ascii')
        body = {
            'model': _MODEL,
            'messages': [{
                'role': 'user',
                'content': [
                    {'type': 'text', 'text': _ADVICE_PROMPT.format(question=question)},
                    {'type': 'image_url', 'image_url': {
                        'url': f'data:image/jpeg;base64,{image_b64}',
                    }},
                ],
            }],
            'stream': True,
        }
        key = self._config.key('zhipu')
        if not key:
            raise RuntimeError('zhipu key unavailable')
        base = str(self._config.get('models.brain.fuse', 'http://127.0.0.1:18793')).rstrip('/')
        text_parts: list[str] = []
        usage: dict = {}
        with httpx.Client(timeout=90.0, trust_env=False) as client:
            with client.stream(
                'POST',
                base + '/api/paas/v4/chat/completions',
                headers={
                    'Authorization': f'Bearer {key}',
                    'Content-Type': 'application/json',
                    'Accept': 'text/event-stream',
                },
                json=body,
            ) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if not line.startswith('data:'):
                        continue
                    data = line[5:].strip()
                    if data == '[DONE]':
                        break
                    chunk = json.loads(data or '{}')
                    usage = chunk.get('usage') or usage
                    for choice in chunk.get('choices') or []:
                        delta = choice.get('delta') or {}
                        text_parts.append(delta.get('content') or '')
        text = ''.join(text_parts).strip()
        return text or '截图看到了，但这次没整理出建议。', self._usage(usage)

    @staticmethod
    def _usage(payload: dict) -> dict:
        usage = payload or {}
        return {
            'input': int(usage.get('prompt_tokens') or usage.get('input') or 0),
            'output': int(usage.get('completion_tokens') or usage.get('output') or 0),
            'cacheRead': int(
                (usage.get('prompt_tokens_details') or {}).get('cached_tokens')
                or usage.get('cacheRead')
                or 0
            ),
        }


__all__ = ['GameAdviceWorker']