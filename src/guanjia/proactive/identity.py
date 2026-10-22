"""A 身份感知（议会定稿）：沿触发的性别视觉判断 + 在场身份缓存 + 双通道修正。

设计要点（对应 issue）：
- 沿触发判断（SX-5/JG-2）：人进入沿才调 GLM 视觉，同人在场期间缓存复用，不逐帧烧 token
- 缓存 TTL（QC-30）：N 分钟未再确认即降置信并停止作为开口依据——防盲区人离开沿缺失、
  对着空房间按旧缓存开口
- 置信度门控（QC-5）：低于阈值开口降级为不带称呼或不开口
- 冷启动降级（JG-3）：两口人性别相异，仅靠性别视觉即可给出候选身份；
  位置/时间上下文推断待 E 习惯库置信度达标后接入（启用阈值在 proactive.json）
- 错标双通道修正（JG-4/QC-18）：声纹开口回写 + 对话中被纠正即回写

视觉调用走网关（127.0.0.1:18789 → 智谱 glm-5.3-flash），独立会话 agent:main:identity，
不污染主会话与微信；输入用降级后的帧（隐私组合路，采集源头已生效）。
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import json
import re
import threading
import time
from pathlib import Path
from typing import Callable

import httpx

from guanjia.brain import load_gateway_token
from guanjia.proactive.config import ProactiveConfig

_IDENTITY_SESSION = 'agent:main:identity'
_GATEWAY_URL = 'http://127.0.0.1:18789/v1/chat/completions'
# 两口人映射（#F15 一男一女）：性别视觉 → 候选身份；声纹/对话纠正可覆盖
_GENDER_TO_WHO = {'男': '主人', '女': '女主人'}

_PROMPT = (
    '看这张家庭监控画面。画面里的人是男性还是女性？'
    '只输出一个 JSON 对象，格式 {"gender": "男" 或 "女", "confidence": 0到1, '
    '"appearance": "一句衣着外观描述"}；看不清或判断没把握就把 confidence 调低。不要 markdown。'
)


class IdentityTracker:
    """在场身份状态机：视觉判断只发生在沿上，缓存在内存，快照落 identity.json。"""

    def __init__(
        self,
        config: ProactiveConfig | None = None,
        snapshot_path: Path | None = None,
        client_factory: Callable[[], httpx.Client] | None = None,
    ) -> None:
        self._config = config or ProactiveConfig()
        # 快照与 proactive.json 同目录（data/vision/），从配置路径推导，跟随 GUANJIA_VISION_ROOT
        self._snapshot_path = snapshot_path or (
            Path(str(self._config.path)).parent / 'identity.json'
        )
        self._token = load_gateway_token()
        self._client_factory = client_factory or (lambda: httpx.Client(timeout=20.0, trust_env=False))
        self._lock = threading.Lock()
        self._presence: dict[str, object] | None = None   # 当前在场身份缓存
        self._last_seen_monotonic: float = 0.0
        self._judging = False                              # 视觉判断去重（同一沿只判一次）

    # ---------- 沿回调（由 vision_loop 的 on_edge 钩子调用） ----------

    def on_edge(self, record: dict[str, object], edge: str) -> None:
        """沿事件入口：entered → 异步视觉判断；left → 清缓存。绝不在视觉线程里同步调模型。"""
        if edge == 'left':
            self._clear_presence('person_left')
            return
        if edge != 'entered' or self._judging:
            return
        if not record.get('send_to_model', True):
            return  # 待命/夜间帧不送层：不做视觉判断
        if self._presence is not None:
            # 已有在场身份：只刷新 last_seen（TTL 续期），不重判
            self._refresh_last_seen()
            return
        self._judge_async(record)

    # ---------- 修正双通道（QC-18） ----------

    def confirm_by_voice(self, speaker: str) -> None:
        """声纹通道：说话人标签回写在场身份（声纹比视觉可靠，开口即修正）。"""
        if not speaker or speaker in ('未知', ''):
            return
        with self._lock:
            if self._presence is None:
                return
            previous = str(self._presence.get('who') or '')
            self._presence['who'] = speaker
            self._presence['confidence'] = 1.0
            self._presence['corrected_by'] = 'voice'
            self._presence['corrected_at'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
            self._presence['previous_who'] = previous or None
            self._last_seen_monotonic = time.monotonic()
            self._write_snapshot()

    def correct_by_chat(self, who: str) -> None:
        """对话纠正通道：用户在对话中表明身份即回写。"""
        self.confirm_by_voice(who)

    # ---------- 查询 ----------

    def presence(self, refresh_seen: bool = False) -> dict[str, object] | None:
        """当前在场身份（TTL 检查后）；超时降置信，stop_as_basis=True 时不得作为开口依据。"""
        with self._lock:
            if self._presence is None:
                return None
            stale_min = (time.monotonic() - self._last_seen_monotonic) / 60.0
            ttl_min = self._config.num('identity_ttl_min', 10)
            result = dict(self._presence)
            if stale_min >= ttl_min:
                result['stale'] = True
                result['stop_as_basis'] = True  # QC-30：过期停止作为开口依据
                result['stale_min'] = round(stale_min, 1)
            if refresh_seen:
                self._last_seen_monotonic = time.monotonic()
            return result

    def should_address_by_name(self) -> bool:
        """置信度门控（QC-5）：身份置信达标才允许带称呼开口。"""
        current = self.presence()
        if not current or current.get('stop_as_basis'):
            return False
        threshold = self._config.num('identity_confidence_threshold', 0.55)
        return float(current.get('confidence') or 0.0) >= threshold

    # ---------- 内部 ----------

    def _judge_async(self, record: dict[str, object]) -> None:
        self._judging = True

        def runner() -> None:
            try:
                self._judge_sync(record)
            finally:
                self._judging = False

        threading.Thread(target=runner, name='identity-judge', daemon=True).start()

    def _judge_sync(self, record: dict[str, object]) -> None:
        image_path = Path(str(record.get('image_path') or ''))
        if not image_path.exists():
            return
        try:
            image_b64 = base64.b64encode(image_path.read_bytes()).decode('ascii')
        except Exception:
            return
        content = [
            {'type': 'text', 'text': _PROMPT},
            {'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{image_b64}'}},
        ]
        reply = self._ask_gateway(content)
        if reply is None:
            return
        parsed = self._parse(reply)
        if parsed is None:
            return
        gender = str(parsed.get('gender') or '').strip()
        confidence = float(parsed.get('confidence') or 0.0)
        appearance = str(parsed.get('appearance') or '').strip()[:60]
        who = _GENDER_TO_WHO.get(gender)
        if who is None:
            return
        with self._lock:
            self._presence = {
                'who': who,
                'gender': gender,
                'confidence': round(min(1.0, confidence), 3),
                'appearance': appearance,
                'source': 'vision',
                'since': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                'frame_id': record.get('frame_id'),
            }
            self._last_seen_monotonic = time.monotonic()
            self._write_snapshot()

    def _ask_gateway(self, content: list[dict[str, object]]) -> str | None:
        body = {
            'model': 'openclaw',
            'messages': [{'role': 'user', 'content': content}],
            'stream': False,
        }
        headers = {
            'Authorization': f'Bearer {self._token}',
            'Content-Type': 'application/json',
            'x-openclaw-session-key': _IDENTITY_SESSION,
        }
        try:
            with self._client_factory() as client:
                response = client.post(_GATEWAY_URL, headers=headers, json=body)
                response.raise_for_status()
                return str(response.json()['choices'][0]['message']['content'])
        except Exception:
            return None

    @staticmethod
    def _parse(reply: str) -> dict[str, object] | None:
        match = re.search(r'\{.*\}', reply, re.S)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None

    def _refresh_last_seen(self) -> None:
        with self._lock:
            if self._presence is not None:
                self._last_seen_monotonic = time.monotonic()
                self._write_snapshot()

    def _clear_presence(self, reason: str) -> None:
        with self._lock:
            if self._presence is not None:
                self._presence['left_at'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
                self._presence['left_reason'] = reason
                self._write_snapshot(left=True)
            self._presence = None

    def _write_snapshot(self, left: bool = False) -> None:
        """身份快照落 data/vision/identity.json：面板可见 + E 组件习惯提取的输入。"""
        try:
            payload = dict(self._presence or {})
            payload['present'] = self._presence is not None
            payload['updated_at'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
            self._snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._snapshot_path.with_suffix('.tmp')
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
            temporary.replace(self._snapshot_path)
        except Exception:
            pass


__all__ = ['IdentityTracker']
