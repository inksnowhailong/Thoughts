"""ConfigHub：统一配置层（发起人需求 2）——收编散落配置为单一来源。

优先级（高→低）：环境变量 > data/config.json > 各模块默认值
键引用：keys.* 只存 SecretRef 路径，绝不落明文（隐私红线：key 不进仓库/配置文件）。

现状迁移策略（渐进式）：
- 旧配置文件（voice-settings.json / proactive.json / openclaw.json 网关侧）保持兼容；
- 新代码一律从 ConfigHub 读；旧模块后续逐个迁移（见各文件 TODO 标记）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

_REPO_ROOT = Path(os.environ.get('GUANJIA_ROOT', r'F:\coding\guanjia-v2-development'))
_DEFAULT_PATH = _REPO_ROOT / 'data' / 'config.json'

# keys 的 SecretRef 白名单：值是 ~/.openclaw/secrets/<name>/value 的 <name>，
# 绝不接受明文 key 写进 config.json（隐私红线）
_KNOWN_SECRET_REFS = {
    'zhipu': 'zhipu_key_file',
    'gateway': None,  # 网关 token 由 brain.load_gateway_token 管（openclaw.json），此处仅登记来源
}


class ConfigHub:
    """单一配置来源。读多写少，进程内缓存 + 热重读。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or _DEFAULT_PATH
        self._cache: dict | None = None

    # ---------- 读取 ----------

    def reload(self) -> dict:
        """热重读 config.json（改文件即生效语义与 proactive.json 一致）。"""
        try:
            self._cache = json.loads(self.path.read_text(encoding='utf-8'))
        except Exception:
            self._cache = self._cache or {}
        return self._cache

    @property
    def data(self) -> dict:
        if self._cache is None:
            self.reload()
        return self._cache if isinstance(self._cache, dict) else {}

    def get(self, dotted: str, default: object = None) -> object:
        """点路径读取：get('limits.speak_window_sec', 21600)。

        环境变量覆盖：GUANJIA_<大写下划线>（如 GUANJIA_SPEAK_WINDOW_SEC）
        ——运维兜底手段，与 supervisor 的 tmp/voice.env 机制天然衔接。
        """
        env_key = 'GUANJIA_' + dotted.upper().replace('.', '_')
        env_val = os.environ.get(env_key)
        if env_val is not None:
            return self._coerce(env_val, default)
        node: object = self.data
        for part in dotted.split('.'):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return default
        return node

    def num(self, dotted: str, default: float) -> float:
        try:
            return float(self.get(dotted, default))
        except (TypeError, ValueError):
            return default

    def section(self, name: str) -> dict:
        value = self.get(name, {})
        return value if isinstance(value, dict) else {}

    # ---------- key 引用（只解析 SecretRef，不落明文） ----------

    def key(self, name: str) -> str | None:
        """按名字解析密钥：优先 SecretRef 文件，其次环境变量；永不写明文进 config。"""
        env_key = f'GUANJIA_{name.upper()}_KEY'
        if os.environ.get(env_key):
            return os.environ[env_key]
        secret_ref = _KNOWN_SECRET_REFS.get(name)
        if secret_ref:
            path = Path.home() / '.openclaw' / 'secrets' / secret_ref / 'value'
            if path.exists():
                return path.read_text(encoding='utf-8').strip()
        # 兼容 openclaw.json 的 SecretRef 结构（与 camera-tool 同源逻辑）
        try:
            oc = json.loads((Path.home() / '.openclaw' / 'openclaw.json').read_text(encoding='utf-8-sig'))
            value = oc['models']['providers'][name].get('apiKey', '')
            if isinstance(value, dict):
                path = Path.home() / '.openclaw' / 'secrets' / secret_ref / 'value'
                value = path.read_text(encoding='utf-8').strip() if path.exists() else ''
            return value or None
        except Exception:
            return None

    # ---------- 内部 ----------

    @staticmethod
    def _coerce(env_val: str, default: object) -> object:
        """环境变量按默认值类型转换（bool/int/float/str）。"""
        if isinstance(default, bool) or default is None and env_val.lower() in ('true', 'false', '1', '0'):
            return env_val.lower() in ('true', '1')
        try:
            if isinstance(default, int) and not isinstance(default, bool):
                return int(env_val)
            if isinstance(default, float):
                return float(env_val)
        except ValueError:
            pass
        return env_val


__all__ = ['ConfigHub']
