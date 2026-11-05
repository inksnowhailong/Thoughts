"""能力分级表：levels.json 分级映射的 Python 侧真相。

来源与一致性要求：
- openclaw/plugins/guanjia-guard/defaults/levels.json 的 tools 表（迁移）；
- agent-runtime/src/tools/capabilities.ts（TS Registry）必须与本表一致；
- 未登记的能力一律按角色矩阵拒绝（owner 矩阵只放行这里列出的键）。
"""

from __future__ import annotations

# 与 TS 侧 capabilities.ts 完全一致的 Wave1-B 能力分级
CAPABILITY_LEVELS: dict[str, str] = {
    'launch_app': 'L1',
    'get_weather': 'L0',
    'get_location': 'L0',
    'speak': 'L1',
    'web_fetch': 'L0',
    'screen_capture': 'L0',
    'screen_understand': 'L0',
}

# 以下照搬 levels.json tools 表：新工具链后续接入时分级先到位，
# exec 端点没有实现会单独报 tool_not_implemented，不会因此放开执行。
CAPABILITY_LEVELS.update({
    'read': 'L0',
    'ls': 'L0',
    'memory_search': 'L0',
    'memory_get': 'L0',
    'web_search': 'L0',
    'x_search': 'L0',
    'view_image': 'L0',
    'session_status': 'L0',
    'sessions_list': 'L0',
    'sessions_history': 'L0',
    'agents_list': 'L0',
    'get_goal': 'L0',
    'tool_search': 'L0',
    'tool_describe': 'L0',
    'ask_user': 'L0',
    'progress_card': 'L1',
    'tts': 'L1',
    'task_start': 'L1',
    'task_status': 'L1',
    'task_cancel': 'L1',
    'task_report': 'L1',
    'browser': 'L1',
    'cron': 'L1',
    'automations': 'L1',
    'image_generate': 'L1',
    'write': 'L1',
    'edit': 'L1',
    'apply_patch': 'L1',
    'exec': 'L1',
    'process': 'L1',
    'message': 'L1',
    'sessions_send': 'L1',
    'subagents': 'L1',
    'code_execution': 'L1',
    'nodes': 'L1',
    'camera_ptz': 'L1',
    'gateway': 'L2',
    'plugins': 'L2',
    'secrets': 'L2',
    'terminal': 'L2',
    'get_desktop_status': 'L0',
    'environment_snapshot': 'L0',
    'camera_look': 'L0',
    'camera_photo': 'L0',
})

DEFAULT_LEVEL = 'L1'


def level_for(capability_id: str) -> str:
    """分级查询；未登记能力不会走到这里放行（角色矩阵先拒）。"""
    return CAPABILITY_LEVELS.get(capability_id, DEFAULT_LEVEL)


def l2_capabilities() -> frozenset[str]:
    return frozenset(cap for cap, level in CAPABILITY_LEVELS.items() if level == 'L2')


def owner_allowed_capabilities() -> frozenset[str]:
    """owner 角色允许调用的能力全集（分级门在其后再拦 L2/L3）。"""
    return frozenset(CAPABILITY_LEVELS)
