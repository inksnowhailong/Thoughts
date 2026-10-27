"""语音直调引擎（上下文分级方案 A 链路，2026-10-27 定稿）。

说话的只驮说话的：固定底盘（SOUL/USER/MEMORY/IDENTITY/管家规则/安全红线）
+ 滚动摘要（最近 N 轮，含每轮画面一行描述）+ 当轮（用户话 + 640px 隐私帧），
直调智谱 flash（经保险丝），目标 3~4 秒开口。工具由本模块自执行，
不再走网关会话——微信/面板仍走网关，互不影响。

缓存两铁律（方案 3.2）：
1. 图放当轮消息尾部：底盘与滚动摘要字节稳定，前缀缓存逐轮命中；
2. 历史不存图：当轮看完即蒸发，只留一行文字描述进滚动摘要。
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Callable, Iterator

import httpx

from guanjia.config_hub import ConfigHub

_WS = Path.home() / '.openclaw' / 'workspace'
_ROOT = Path(os.environ.get('GUANJIA_ROOT', r'F:\coding\guanjia-v2-development'))
_HISTORY_PATH = _ROOT / 'data' / 'talk' / 'history.json'
_VISION_LATEST = _ROOT / 'data' / 'vision' / 'latest.json'

_MODEL = 'glm-5.3-flash'
_ROLLING_TURNS = 10          # 滚动摘要窗口（开放问题 3 默认：按轮数）
_MAX_TOOL_ROUNDS = 4         # 一轮最多几批工具调用（防死循环）
_IMG_MAX_W = 640             # 当轮隐私帧宽度（~500 token）


def _read(name: str) -> str:
    """读 workspace 提示词文件；失败给空串（缺文件不致命）。"""
    try:
        return (_WS / name).read_text(encoding='utf-8').strip()
    except Exception:
        return ''


def _agents_s2_rules() -> str:
    """从 AGENTS.md 抽说话相关规则段：guanjia-s2 标记块 + 摄像头与视觉段。"""
    text = _read('AGENTS.md')
    parts: list[str] = []
    m = re.search(r'<!-- guanjia-s2:begin -->(.*?)<!-- guanjia-s2:end -->', text, re.S)
    if m:
        parts.append(m.group(1).strip())
    m2 = re.search(r'## 摄像头与视觉.*?(?=\n## |\Z)', text, re.S)
    if m2:
        parts.append(m2.group(0).strip())
    return '\n\n'.join(parts)


_SAFETY = (
    '安全红线：不泄露家庭隐私数据；不做破坏性操作；执行外部发送类动作前先口头确认；'
    '拿不准就问一句。'
)

_VISION_NOTE = (
    '视觉说明：每轮语音消息会附带一张当前家用摄像头画面的缩略截图（可能降级模糊），'
    '画面里的信息可以直接引用；细节看不清时调 camera_look 主动看。'
)

_TOOLS = [
    {
        'type': 'function',
        'function': {
            'name': 'launch_app',
            'description': '启动已登记的程序或游戏（英雄联盟 lol / 网易云 music 等）。',
            'parameters': {'type': 'object', 'properties': {
                'name': {'type': 'string', 'description': 'apps.json 里的英文键，如 lol'}},
                'required': ['name'], 'additionalProperties': False},
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'get_weather',
            'description': '查天气：现在+今天+明天（温度、降水、带不带伞）。可省 city（=家）。',
            'parameters': {'type': 'object', 'properties': {
                'city': {'type': 'string', 'description': '城市名，可省略'}},
                'additionalProperties': False},
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'get_location',
            'description': '拿家里电脑当前经纬度（Windows 定位，不是 IP）。',
            'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False},
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'task_start',
            'description': '创建后台干活任务（超 5 秒、多步、要看屏幕的活）。worker 有截屏/执行能力。定时任务填 when（本地时间，如 20:00 或 2026-10-28T08:30）。',
            'parameters': {'type': 'object', 'properties': {
                'summary': {'type': 'string', 'description': '任务简述一句话'},
                'goal': {'type': 'string', 'description': '给 worker 的完整目标'},
                'when': {'type': 'string', 'description': '定时执行时间，不填立即执行'}},
                'required': ['summary', 'goal'], 'additionalProperties': False},
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'task_status',
            'description': '查任务状态（省略 id 查全部摘要）。',
            'parameters': {'type': 'object', 'properties': {
                'id': {'type': 'string', 'description': '任务编号，可省略'}},
                'additionalProperties': False},
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'task_cancel',
            'description': '取消任务（协作取消，不杀进程）。',
            'parameters': {'type': 'object', 'properties': {
                'id': {'type': 'string', 'description': '任务编号'},
                'reason': {'type': 'string', 'description': '原因，可省略'}},
                'required': ['id'], 'additionalProperties': False},
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'camera_look',
            'description': '用摄像头看画面并回答问题（会自动转头搜索画面）。',
            'parameters': {'type': 'object', 'properties': {
                'question': {'type': 'string', 'description': '想看什么'},
                'direction': {'type': 'string', 'enum': ['left', 'right', 'up', 'down'],
                              'description': '可选：先往哪边转'}},
                'required': ['question'], 'additionalProperties': False},
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'camera_ptz',
            'description': '只转摄像头不分析（往左/右/上/下转）。',
            'parameters': {'type': 'object', 'properties': {
                'direction': {'type': 'string', 'enum': ['left', 'right', 'up', 'down']},
                'duration_sec': {'type': 'number', 'description': '秒数 0.1~2.5，默认 0.8'}},
                'required': ['direction'], 'additionalProperties': False},
        },
    },
]


# ---------- 工具自执行（网关工具的本地等价实现） ----------

def _run_ps(script: str, args: list[str], timeout: float = 15.0) -> str:
    """跑 workspace/tools 下的 PowerShell 脚本，回 stdout。"""
    path = _WS / 'tools' / script
    completed = subprocess.run(
        ['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
         '-File', str(path), *args],
        capture_output=True, text=True, encoding='utf-8', timeout=timeout)
    return completed.stdout or ''


def _last_json(stdout: str):
    """取输出里最后一个能解析的 JSON 行（与插件 ps-tools 同约定）。"""
    for line in reversed([l.strip() for l in stdout.splitlines() if l.strip()]):
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return None


_BEIJING = {'lat': 39.9, 'lon': 116.41}
_WMO_CN = {0: '晴', 1: '多云', 2: '多云', 3: '多云', 45: '有雾', 48: '有雾'}


def _weather_text(code: int) -> str:
    if code in _WMO_CN:
        return _WMO_CN[code]
    if 51 <= code <= 57:
        return '毛毛雨'
    if 61 <= code <= 67 or 80 <= code <= 82:
        return '雨'
    if 71 <= code <= 77 or code in (85, 86):
        return '雪'
    if code >= 95:
        return '雷雨'
    return '天气不明'


class ToolBox:
    """说话轮的本地工具执行器；每个方法返回给模型看的文本结果。"""

    def __init__(self, task_caller: Callable[[str, dict], dict] | None = None) -> None:
        # task_caller: voice 进程注入的 TaskIPC 客户端（(action, body) -> resp dict）
        self._task = task_caller

    def run(self, name: str, arguments: dict) -> str:
        handler = getattr(self, f'_do_{name}', None)
        if handler is None:
            return f'没有这个工具：{name}'
        try:
            return handler(arguments or {})
        except Exception as exc:  # noqa: BLE001  工具失败要告诉模型，不能炸轮
            return f'工具执行失败：{type(exc).__name__}: {exc}'

    def _do_launch_app(self, args: dict) -> str:
        name = str(args.get('name') or '').strip()
        if not name:
            return '没有启动：名称为空。'
        r = _last_json(_run_ps('launch-app.ps1', ['-Name', name], 20))
        if not r:
            return '没有启动：启动器没有返回结果。'
        if r.get('ok'):
            return f"已启动并核实 {r.get('app')}。"
        known = '、'.join(r.get('known') or [])
        err = r.get('error')
        if err == 'not_in_map':
            return f"apps.json 里没有「{name}」。已登记的有：{known}。"
        if err == 'ambiguous':
            cands = '、'.join(r.get('candidates') or [])
            return f'你说的可能是：{cands}。是哪一个？'
        if err == 'unverified':
            return f"已发出启动请求，但没核实到 {r.get('app')} 的进程或窗口。"
        return f"没有启动：{err or '未知错误'}。"

    def _do_get_location(self, args: dict) -> str:
        r = _last_json(_run_ps('get-location.ps1', [], 15))
        if r and isinstance(r.get('lat'), (int, float)) and isinstance(r.get('lon'), (int, float)):
            return f"纬度 {r['lat']:.3f}，经度 {r['lon']:.3f}。"
        return '没定位到，按北京处理。'

    def _do_get_weather(self, args: dict) -> str:
        city = str(args.get('city') or '').strip()
        lat, lon, place = _BEIJING['lat'], _BEIJING['lon'], '北京'
        try:
            if city:
                with httpx.Client(timeout=8.0, trust_env=False) as c:
                    g = c.get('https://geocoding-api.open-meteo.com/v1/search',
                             params={'name': city, 'count': 1, 'language': 'zh'}).json()
                hit = (g.get('results') or [None])[0]
                if not hit:
                    return f'没查到「{city}」这个地方。'
                lat, lon, place = hit['latitude'], hit['longitude'], hit.get('name', city)
            else:
                r = _last_json(_run_ps('get-location.ps1', [], 15))
                if r and isinstance(r.get('lat'), (int, float)):
                    lat, lon, place = r['lat'], r['lon'], '家这边'
            q = ('current=temperature_2m,apparent_temperature,weather_code,precipitation'
                 '&daily=weather_code,temperature_2m_max,temperature_2m_min,'
                 'precipitation_probability_max&timezone=Asia%2FShanghai&forecast_days=2')
            with httpx.Client(timeout=8.0, trust_env=False) as c:
                w = c.get(f'https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&{q}').json()
            cur, d = w['current'], w['daily']
            today = (f"今天{round(d['temperature_2m_min'][0])}到{round(d['temperature_2m_max'][0])}度，"
                     f"{_weather_text(d['weather_code'][0])}，降水概率{d['precipitation_probability_max'][0]}%")
            tomorrow = (f"明天{round(d['temperature_2m_min'][1])}到{round(d['temperature_2m_max'][1])}度，"
                        f"{_weather_text(d['weather_code'][1])}，降水概率{d['precipitation_probability_max'][1]}%")
            rain_codes = {*(range(51, 68)), *(range(71, 78)), *(range(80, 83)), 85, 86, *range(95, 100)}
            umbrella = '要带伞' if d['weather_code'][0] in rain_codes or d['weather_code'][1] in rain_codes else '不用带伞'
            return f"{place}现在{round(cur['temperature_2m'])}度，{_weather_text(cur['weather_code'])}。{today}。{tomorrow}。{umbrella}。"
        except Exception as exc:  # noqa: BLE001
            return f'天气没查到：{type(exc).__name__}。'

    def _do_task(self, action: str, args: dict) -> str:
        if self._task is None:
            return '任务管家暂时不可用。'
        resp = self._task(action, args)
        if not resp.get('ok'):
            err = resp.get('error')
            if err == 'bad_when':
                return '任务没创建：时间格式看不懂，要用 20:00 或 2026-10-28T08:30 这种。'
            return '任务管家暂时不可用。'
        task = resp.get('task') or {}
        if action == '/start':
            run_at = task.get('run_at')
            if isinstance(run_at, (int, float)):
                import time as _t
                return f"定时任务已创建：{task.get('summary')}，到点执行。"
            return f"任务已创建：{task.get('summary')}。"
        return json.dumps(task.get('status') or task, ensure_ascii=False)[:400]

    def _do_task_start(self, args: dict) -> str:
        return self._do_task('/start', args)

    def _do_task_status(self, args: dict) -> str:
        return self._do_task('/status', {'id': args.get('id')} if args.get('id') else {})

    def _do_task_cancel(self, args: dict) -> str:
        return self._do_task('/cancel', {'id': args.get('id'), 'reason': args.get('reason', '')})

    def _do_camera_look(self, args: dict) -> str:
        tool = _ROOT / 'openclaw' / 'workspace' / 'tools' / 'camera-tool.py'
        py = os.environ.get('GUANJIA_PYTHON', str(_ROOT / '.venv' / 'Scripts' / 'python.exe'))
        q = str(args.get('question') or '看看现在画面里有什么')
        direction = str(args.get('direction') or '')
        cmd = [py, '-X', 'utf8', str(tool), 'look', q, direction, '0.8']
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', timeout=60).stdout
        except subprocess.TimeoutExpired:
            return '看画面超时了。'
        r = _last_json(out)
        if r and r.get('ok'):
            return f"{r.get('moved') or ''}{r.get('description') or ''}".strip()
        return f"没能看到画面：{(r or {}).get('error') or '未知错误'}。"

    def _do_camera_ptz(self, args: dict) -> str:
        tool = _ROOT / 'openclaw' / 'workspace' / 'tools' / 'camera-tool.py'
        py = os.environ.get('GUANJIA_PYTHON', str(_ROOT / '.venv' / 'Scripts' / 'python.exe'))
        d = str(args.get('direction') or '')
        dur = str(args.get('duration_sec') or '0.8')
        cmd = [py, '-X', 'utf8', str(tool), 'ptz', d, dur]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', timeout=30).stdout
        except subprocess.TimeoutExpired:
            return '摄像头转动超时。'
        r = _last_json(out)
        return (r.get('message') or '摄像头已转动。') if r and r.get('ok') else f"没转成：{(r or {}).get('error') or '未知错误'}。"


# ---------- 滚动历史（最近 N 轮原文，图蒸发留文字） ----------

class RollingHistory:
    """最近 _ROLLING_TURNS 轮的 user/assistant 原文；落盘 data/talk/history.json。"""

    def __init__(self) -> None:
        self._turns: list[dict] = []
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(_HISTORY_PATH.read_text(encoding='utf-8'))
            self._turns = data.get('turns', []) if isinstance(data, dict) else []
        except Exception:
            self._turns = []

    def _save(self) -> None:
        try:
            _HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
            _HISTORY_PATH.write_text(json.dumps({'turns': self._turns}, ensure_ascii=False),
                                      encoding='utf-8')
        except Exception:
            pass

    def append(self, user: str, reply: str, vision_line: str) -> None:
        """一轮入账；按语音轮规格截断（user 200 / reply 120——历史里超长回复会教坏模型跟着说长话）。"""
        self._turns.append({
            'u': user.strip()[:200],
            'a': reply.strip()[:120],
            'v': vision_line.strip()[:60],
        })
        if len(self._turns) > _ROLLING_TURNS:
            self._turns = self._turns[-_ROLLING_TURNS:]
        self._save()

    def as_messages(self) -> list[dict]:
        """滚动摘要转 messages（每轮 user/assistant 一对，画面描述并进 user 行）。"""
        msgs: list[dict] = []
        for t in self._turns:
            u = t.get('u') or ''
            if t.get('v'):
                u = f'{u}（画面：{t["v"]}）'
            msgs.append({'role': 'user', 'content': u})
            a = t.get('a') or ''
            if a:
                msgs.append({'role': 'assistant', 'content': a})
        return msgs

    def clear(self) -> None:
        self._turns = []
        self._save()


# ---------- 直调引擎 ----------

class TalkDirect:
    """一轮语音直调：底盘+滚动历史+当轮（话+图）→ flash（经保险丝），工具自执行循环。"""

    def __init__(self, toolbox: ToolBox | None = None) -> None:
        hub = ConfigHub()
        self._key = hub.key('zhipu')
        self._url = str(hub.get('models.brain.fuse', 'http://127.0.0.1:18793')).rstrip('/') + '/api/paas/v4/chat/completions'
        self._toolbox = toolbox or ToolBox()
        self._history = RollingHistory()
        self._base: str | None = None  # 底盘缓存（按源文件 mtime 失效）
        self._base_sig: str | None = None
        self.last_usage: dict = {}
        self._turn_usage: dict = {}

    def _base_prompt(self) -> str:
        """固定底盘：SOUL + USER + MEMORY + IDENTITY + 管家规则 + 红线 + 视觉说明。

        字节不变保前缀缓存；面板改了提示词文件（SOUL/USER/MEMORY/AGENTS）时
        按 mtime 自动重建——不重建就得等下次重启才生效。
        """
        sig = self._sources_sig()
        if self._base is None or sig != self._base_sig:
            parts = [
                _read('SOUL.md'),
                _read('USER.md'),
                _read('MEMORY.md'),
                _read('IDENTITY.md'),
                _agents_s2_rules(),
                _SAFETY,
                _VISION_NOTE,
            ]
            self._base = '\n\n'.join(p for p in parts if p)
            self._base_sig = sig
        return self._base

    @staticmethod
    def _sources_sig() -> str:
        """底盘源文件的 mtime 指纹（启动后面板编辑即可热生效）。"""
        import hashlib
        marks = []
        for name in ('SOUL.md', 'USER.md', 'MEMORY.md', 'IDENTITY.md', 'AGENTS.md'):
            try:
                marks.append(str((_WS / name).stat().st_mtime))
            except OSError:
                marks.append('x')
        return hashlib.md5('|'.join(marks).encode()).hexdigest()

    def _vision_frame_b64(self) -> tuple[str, str]:
        """最新隐私帧 → 640px base64；顺带返回画面本地摘要一行。"""
        note = ''
        try:
            latest = json.loads(_VISION_LATEST.read_text(encoding='utf-8'))
            note = str(latest.get('summary') or '')
            if not latest.get('send_to_model', True):
                return '', note  # 待命/夜间帧不送
            img_path = Path(latest.get('image_path') or '')
            if not (img_path and img_path.exists()):
                return '', note
            from PIL import Image
            with Image.open(img_path) as im:
                im = im.convert('RGB')
                if im.width > _IMG_MAX_W:
                    h = round(im.height * _IMG_MAX_W / im.width)
                    im = im.resize((_IMG_MAX_W, h), Image.Resampling.LANCZOS)
            import io
            buf = io.BytesIO()
            im.save(buf, format='JPEG', quality=80)
            return base64.b64encode(buf.getvalue()).decode('ascii'), note
        except Exception:
            return '', note

    def stream(self, text: str, speaker: str = '') -> Iterator[str]:
        """一轮直调：生成器逐段 yield 回复文本（TurnRunner.open_stream 兼容形状）。

        工具循环：模型返回 tool_calls → 本地执行 → 结果追加 → 再调，最多 _MAX_TOOL_ROUNDS 批。
        usage 通过 self.last_usage 交给 voice 侧记账。
        """
        self.last_usage = {}
        base = self._base_prompt()
        image_b64, vision_note = self._vision_frame_b64()

        messages: list[dict] = [{'role': 'system', 'content': base}]
        messages.extend(self._history.as_messages())
        user_content: list | str
        # 语音时间标记（与 AGENTS.md 语音轮规则同格式）：问几点/日期时模型直接答，不调工具
        now = time.localtime()
        weekdays = '一二三四五六日'
        tag = time.strftime(f'%Y-%m-%d %H:%M 周{weekdays[now.tm_wday]}', now)
        tagged = f'[语音 {tag}]{speaker and f" {speaker}" or ""} {text}'
        if vision_note:
            tagged = f'{tagged}（画面：{vision_note}）'
        if image_b64:
            user_content = [
                {'type': 'text', 'text': tagged},
                {'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{image_b64}'}},
            ]
        else:
            user_content = tagged
        messages.append({'role': 'user', 'content': user_content})

        reply_parts: list[str] = []
        with httpx.Client(timeout=120.0, trust_env=False) as client:
            for round_no in range(_MAX_TOOL_ROUNDS):
                body = {'model': _MODEL, 'messages': messages, 'tools': _TOOLS, 'stream': True}
                self._turn_usage = {}
                r = client.stream('POST', self._url, headers={
                    'Authorization': f'Bearer {self._key}',
                    'Content-Type': 'application/json',
                    'Accept': 'text/event-stream',
                }, json=body)
                with r as response:
                    response.raise_for_status()
                    content_parts: list[str] = []
                    tool_calls: dict[int, dict] = {}  # index -> {id, name, arguments}
                    for line in response.iter_lines():
                        if not line.startswith('data:'):
                            continue
                        data = line[5:].strip()
                        if data == '[DONE]':
                            break
                        chunk = json.loads(data)
                        self._absorb_usage(chunk.get('usage'))
                        choice = (chunk.get('choices') or [{}])[0]
                        delta = choice.get('delta') or {}
                        piece = delta.get('content')
                        if piece:
                            content_parts.append(piece)
                            reply_parts.append(piece)
                            yield piece
                        for tc in delta.get('tool_calls') or []:
                            idx = tc.get('index', 0)
                            slot = tool_calls.setdefault(idx, {'id': '', 'name': '', 'arguments': ''})
                            if tc.get('id'):
                                slot['id'] = tc['id']
                            fn = tc.get('function') or {}
                            if fn.get('name'):
                                slot['name'] = fn['name']
                            if fn.get('arguments'):
                                slot['arguments'] += fn['arguments']
                assistant_msg: dict = {'role': 'assistant', 'content': ''.join(content_parts) or None}
                if tool_calls:
                    assistant_msg['tool_calls'] = [
                        {'id': slot['id'], 'type': 'function',
                         'function': {'name': slot['name'], 'arguments': slot['arguments']}}
                        for _, slot in sorted(tool_calls.items())
                    ]
                messages.append(assistant_msg)
                self._merge_turn_usage()  # 趟结束：usage 入整轮账
                if not tool_calls:
                    break  # 纯文本收尾，出循环
                if round_no == _MAX_TOOL_ROUNDS - 1:
                    yield '（还有要做的活我记下来慢慢做。）'
                    break
                for _, slot in sorted(tool_calls.items()):
                    try:
                        args = json.loads(slot['arguments'] or '{}')
                    except json.JSONDecodeError:
                        args = {}
                    result = self._toolbox.run(slot['name'], args)
                    messages.append({'role': 'tool', 'tool_call_id': slot['id'],
                                     'content': str(result)[:800]})
        reply = ''.join(reply_parts).strip()
        self._history.append(text, reply, vision_note)

    def _absorb_usage(self, usage: dict | None) -> None:
        """SSE 帧内 usage 是累计值（覆盖取最新帧）；趟间累加在 _merge_turn_usage。"""
        if not isinstance(usage, dict) or not usage:
            return
        self._turn_usage = {
            'input': int(usage.get('prompt_tokens') or 0),
            'output': int(usage.get('completion_tokens') or 0),
            'cacheRead': int((usage.get('prompt_tokens_details') or {}).get('cached_tokens') or 0),
        }

    def _merge_turn_usage(self) -> None:
        """一趟模型调用结束：把本趟 usage 累加进整轮（工具循环多趟不丢账）。"""
        turn = getattr(self, '_turn_usage', {})
        self.last_usage = {
            'input': self.last_usage.get('input', 0) + turn.get('input', 0),
            'output': self.last_usage.get('output', 0) + turn.get('output', 0),
            'cacheRead': self.last_usage.get('cacheRead', 0) + turn.get('cacheRead', 0),
        }
        self._turn_usage = {}

    def reset_base(self) -> None:
        """外部强制重置底盘（mtime 指纹已覆盖日常场景，此方法留给显式调用）。"""
        self._base = None
        self._base_sig = None


__all__ = ['TalkDirect', 'ToolBox', 'RollingHistory']