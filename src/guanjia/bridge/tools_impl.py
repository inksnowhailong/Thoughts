"""bridge 的 Python 原生工具实现：web_fetch / get_weather / get_location / speak。

行为对齐线上真相：
- get_weather/get_location：与旧守卫插件 ps-tools.ts 同源——open-meteo 天气/地名接口、
  Windows 定位脚本 get-location.ps1（已迁移至 agent-runtime/tools/）、北京兜底；
- speak：POST 语音守护进程控制口 /say（静音/游戏中的静默决策由守护进程自己判，
  与 announcement 的口径一致）；
- web_fetch：httpx 抓正文，只返回前 4000 字。
所有外呼一律 trust_env=False（家里常开 VPN，不吃系统代理）。
"""

from __future__ import annotations

import html as html_module
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Callable
from urllib.parse import quote

import httpx

_REPO_ROOT = Path(__file__).resolve().parents[3]
_TOOLS_DIR = Path(os.environ.get('GUANJIA_TOOLS_DIR') or (_REPO_ROOT / 'agent-runtime' / 'tools'))
_GET_LOCATION_PS1 = _TOOLS_DIR / 'get-location.ps1'
SAY_URL = os.environ.get('GUANJIA_SAY_URL', 'http://127.0.0.1:18795/say')
SPEAK_MAX_CHARS = 300
WEB_FETCH_TIMEOUT_SEC = 30
WEATHER_TIMEOUT_SEC = 8
LOCATION_TIMEOUT_SEC = 15
WEB_FETCH_TEXT_LIMIT = 4000
BEIJING = {'lat': 39.9042, 'lon': 116.4074}

_SPEAK_REASONS = {
    'muted': '电脑现在是静音状态（托盘里点过静音）',
    'gaming': '主人在打游戏，电脑暂时不出声',
    'empty': '要念的内容是空的',
    'too_long': f'一次最多念 {SPEAK_MAX_CHARS} 字',
    'busy': '电脑这会儿排着好几段要念，等念完再说',
}


def _ok(output: object) -> dict:
    return {'ok': True, 'output': output}


def _fail(code: str, message: str) -> dict:
    return {'ok': False, 'error': {'code': code, 'message': message}}


def _last_json(stdout: str) -> dict | None:
    for line in reversed((stdout or '').splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _run_ps1(script: Path, timeout_sec: int) -> dict | None:
    """跑一个 PowerShell 脚本并取 stdout 最后一行 JSON（退出码只做旁证）。"""
    try:
        result = subprocess.run(
            ['powershell', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
             '-File', str(script)],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout_sec,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return _last_json(result.stdout or '')


def _locate() -> dict:
    """跑定位脚本；失败按北京（与旧 locate() 一致）。"""
    located = _run_ps1(_GET_LOCATION_PS1, LOCATION_TIMEOUT_SEC)
    if located is not None and isinstance(located.get('lat'), (int, float)) \
            and isinstance(located.get('lon'), (int, float)):
        return {'lat': float(located['lat']), 'lon': float(located['lon']),
                'source': str(located.get('source') or 'windows-location'),
                'reason': located.get('reason')}
    return {**BEIJING, 'source': 'fallback-beijing', 'reason': 'script-failed'}


def _weather_cn(code: int) -> str:
    if code == 0:
        return '晴'
    if code <= 3:
        return '多云'
    if code in (45, 48):
        return '有雾'
    if 51 <= code <= 57:
        return '毛毛雨'
    if 61 <= code <= 67:
        return '雨'
    if 71 <= code <= 77:
        return '雪'
    if 80 <= code <= 82:
        return '阵雨'
    if code in (85, 86):
        return '阵雪'
    if code >= 95:
        return '雷雨'
    return '天气不明'


def _rainy(code: int) -> bool:
    return (51 <= code <= 67) or (80 <= code <= 82) or code >= 95 or (71 <= code <= 77) or code in (85, 86)


def _http_get_json(url: str) -> dict:
    with httpx.Client(trust_env=False, timeout=WEATHER_TIMEOUT_SEC) as client:
        response = client.get(url, headers={'User-Agent': 'guanjia-bridge/0.1'})
        response.raise_for_status()
        return response.json()


# ---------- 工具实现 ----------

def get_location(args: dict) -> dict:
    located = _locate()
    where = '电脑自己定位到的' if located['source'] != 'fallback-beijing' else '没定位到，按北京'
    return _ok({
        'text': f"纬度 {located['lat']}，经度 {located['lon']}（{where}）。",
        'lat': located['lat'], 'lon': located['lon'], 'source': located['source'],
    })


def get_weather(args: dict) -> dict:
    city = str((args or {}).get('city') or '').strip()
    try:
        if city:
            geo = _http_get_json(
                f'https://geocoding-api.open-meteo.com/v1/search?name={quote(city)}&count=1&language=zh')
            hits = geo.get('results') or []
            if not hits:
                return _fail('city_not_found', f'没查到「{city}」这个地方，换个写法（例如拼音）再试。')
            hit = hits[0]
            lat, lon = float(hit['latitude']), float(hit['longitude'])
            place = str(hit.get('name') or city)
        else:
            located = _locate()
            lat, lon = located['lat'], located['lon']
            place = '北京' if located['source'] == 'fallback-beijing' else '家这边'
        la, lo = round(lat, 2), round(lon, 2)
        forecast = _http_get_json(
            f'https://api.open-meteo.com/v1/forecast?latitude={la}&longitude={lo}'
            '&current=temperature_2m,apparent_temperature,weather_code,precipitation'
            '&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max'
            '&timezone=Asia%2FShanghai&forecast_days=2'
        )
        daily = forecast['daily']
        current = forecast['current']

        def day(i: int) -> str:
            return (f"{round(daily['temperature_2m_min'][i])}到{round(daily['temperature_2m_max'][i])}度，"
                    f"{_weather_cn(daily['weather_code'][i])}，"
                    f"降水概率{daily['precipitation_probability_max'][i]}%")

        umbrella = any(
            _rainy(daily['weather_code'][i]) or daily['precipitation_probability_max'][i] >= 40
            for i in (0, 1)
        )
        text = (f'{place}：现在{round(current["temperature_2m"])}度'
                f'（体感{round(current["apparent_temperature"])}度），'
                f'{_weather_cn(current["weather_code"])}；今天{day(0)}；明天{day(1)}。'
                f'{"要带伞。" if umbrella else "不用带伞。"}')
        return _ok({'text': text, 'place': place, 'lat': la, 'lon': lo, 'umbrella': umbrella})
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        return _fail('fetch_failed', f'没查到天气：天气服务连不上。改用 get_location 加 web_fetch 查。（{exc}）')


def _html_to_text(raw: str) -> str:
    raw = re.sub(r'(?is)<(script|style)\b.*?</\1>', ' ', raw)
    raw = re.sub(r'(?s)<[^>]+>', ' ', raw)
    raw = html_module.unescape(raw)
    return re.sub(r'\s+', ' ', raw).strip()


def web_fetch(args: dict) -> dict:
    url = str((args or {}).get('url') or '').strip()
    if not re.match(r'^https?://', url, re.IGNORECASE):
        return _fail('bad_url', 'url 只支持 http(s)。')
    try:
        with httpx.Client(trust_env=False, timeout=WEB_FETCH_TIMEOUT_SEC, follow_redirects=True) as client:
            response = client.get(url, headers={'User-Agent': 'Mozilla/5.0 (guanjia-web-fetch)'})
    except httpx.HTTPError as exc:
        return _fail('fetch_failed', f'抓不到 {url}：{exc}')
    content_type = response.headers.get('content-type', '')
    if 'html' in content_type.lower():
        text = _html_to_text(response.text)
    elif 'json' in content_type.lower():
        try:
            text = json.dumps(response.json(), ensure_ascii=False)
        except ValueError:
            text = response.text
    else:
        text = response.text.strip()
    text = text[:WEB_FETCH_TEXT_LIMIT]
    if not text:
        return _fail('empty_body', f'{url} 没有可读正文。')
    return _ok({'url': url, 'status': response.status_code, 'text': text})


def speak(args: dict) -> dict:
    text = str((args or {}).get('text') or '').strip()
    if not text:
        return _fail('empty', f'没有念：{_SPEAK_REASONS["empty"]}。')
    if len(text) > SPEAK_MAX_CHARS:
        return _fail('too_long', f'没有念：{_SPEAK_REASONS["too_long"]}，请缩短。')
    try:
        with httpx.Client(trust_env=False, timeout=3.0) as client:
            response = client.post(SAY_URL, json={'text': text})
    except httpx.HTTPError as exc:
        return _fail('daemon_down', f'没有念：电脑上的语音守护进程没有在运行（{SAY_URL} 连不上）。'
                     f'请主人看一眼托盘，或重启电脑后再试。（{exc}）')
    body: dict = {}
    try:
        parsed = response.json()
        if isinstance(parsed, dict):
            body = parsed
    except ValueError:
        body = {}
    if response.status_code == 200 and body.get('ok') is True:
        return _ok({'text': text, 'reason': 'queued'})
    reason = str(body.get('reason') or f'http_{response.status_code}')
    return _fail(reason, f'没有念：{_SPEAK_REASONS.get(reason, f"语音守护进程拒绝了（{reason}）")}。')


# ---------- 任务三件套（Wave1-C/Phase5）：语音进程 task-ipc 的薄客户端 ----------

# OpenClaw guard 插件 tasks.ts 同源：描述文件里是 {port, capability}；
# OpenClaw 退役后描述文件仍由语音守护进程写（语音侧不动）。
TASK_DESCRIPTOR = Path(os.environ.get(
    'GUANJIA_TASK_DESCRIPTOR',
    str(Path.home() / '.openclaw' / 'workspace' / 'guanjia' / 'task-ipc.json'),
))


def _task_ipc(route: str, body: dict) -> dict:
    try:
        cfg = json.loads(TASK_DESCRIPTOR.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return _fail('unavailable', '任务管家暂时不可用：任务 IPC 描述文件缺失或损坏。')
    port, capability = cfg.get('port'), cfg.get('capability')
    if not isinstance(port, int) or not isinstance(capability, str):
        return _fail('unavailable', '任务管家暂时不可用：任务 IPC 描述文件不完整。')
    try:
        with httpx.Client(trust_env=False, timeout=5.0) as client:
            response = client.post(f'http://127.0.0.1:{port}{route}', json=body,
                                   headers={'X-Guanjia-Task-Cap': capability})
    except httpx.HTTPError as exc:
        return _fail('unavailable', f'任务管家暂时不可用：task-ipc 连不上（{exc}）。')
    try:
        return response.json()
    except ValueError:
        return _fail('rejected', f'task-ipc 返回了非 JSON（http_{response.status_code}）。')


def task_start(args: dict) -> dict:
    """创建后台任务（L1）：{summary, goal, when?, verify?}。只是排队，不执行任何命令。"""
    summary = str((args or {}).get('summary') or '').strip()
    goal = str((args or {}).get('goal') or '').strip()
    if not summary or not goal:
        return _fail('bad_input', '任务没有创建：summary 和 goal 都不能为空。')
    when = str((args or {}).get('when') or '')
    verify = (args or {}).get('verify')
    body: dict = {'summary': summary, 'goal': goal, 'when': when}
    if isinstance(verify, dict):
        body['verify'] = verify
    result = _task_ipc('/start', body)
    if result.get('ok') is True:
        task = result.get('task') or {}
        return _ok({'message': f'任务已创建：{task.get("summary", summary)}。', 'task': task})
    error = str(result.get('error') or 'rejected')
    if error == 'bad_when':
        return _fail('bad_when', '任务没有创建：when 时间格式看不懂（示例 2026-10-25T20:00 或 20:00）。')
    return _fail(error, f'任务没有创建：{result.get("error") or "task-ipc 拒绝"}。')


def task_status(args: dict) -> dict:
    """查任务进度：{id}。"""
    task_id = str((args or {}).get('id') or '').strip()
    if not task_id:
        return _fail('bad_input', '需要任务 id。')
    result = _task_ipc('/status', {'id': task_id})
    if result.get('ok') is True:
        return _ok({'task': result.get('task')})
    return _fail(str(result.get('error') or 'rejected'), result.get('error') or '查询失败。')


def task_cancel(args: dict) -> dict:
    """取消任务（协作式）：{id, reason?}。"""
    task_id = str((args or {}).get('id') or '').strip()
    if not task_id:
        return _fail('bad_input', '需要任务 id。')
    reason = str((args or {}).get('reason') or '主人叫停')
    result = _task_ipc('/cancel', {'id': task_id, 'reason': reason})
    if result.get('ok') is True:
        return _ok({'message': '任务已叫停。', 'task': result.get('task')})
    return _fail(str(result.get('error') or 'rejected'), result.get('error') or '取消失败。')


TOOLS: dict[str, Callable[[dict], dict]] = {
    'get_weather': get_weather,
    'get_location': get_location,
    'web_fetch': web_fetch,
    'speak': speak,
    'task_start': task_start,
    'task_status': task_status,
    'task_cancel': task_cancel,
}


def execute_capability(capability_id: str, args: dict,
                       overrides: dict[str, Callable[[dict], dict]] | None = None) -> dict:
    """按能力 id 执行；overrides 供测试/嵌入方替换实现，优先于内置。"""
    if overrides and capability_id in overrides:
        return overrides[capability_id](args if isinstance(args, dict) else {})
    impl = TOOLS.get(capability_id)
    if impl is None:
        return _fail('tool_not_implemented', f'{capability_id} 在 bridge 侧没有 Python 实现。')
    return impl(args if isinstance(args, dict) else {})
