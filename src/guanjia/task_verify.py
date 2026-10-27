"""L1 任务验收器（方案 D）：worker 声称完成后跑后置条件，过了才 complete。

四种后置条件（verify.kind）：
  process         核实进程在跑（启动类任务的权威判据）
  file            核实文件存在且不小（产出类）
  camera_contains 摄像头画面里能看到目标文字（物理世界类）
  screen_contains 屏幕截屏里能看到目标文字（屏幕类）
"""

from __future__ import annotations

import subprocess
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def _ps_json(script_args: list[str], timeout: float = 20.0) -> dict | None:
    try:
        out = subprocess.run(
            ['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
             *script_args],
            capture_output=True, text=True, encoding='gbk', errors='replace',
            timeout=timeout).stdout
    except Exception:
        return None
    for line in reversed([l.strip() for l in out.splitlines() if l.strip()]):
        try:
            return __import__('json').loads(line)
        except Exception:
            continue
    return None


def _vision_contains(text: str, mode: str) -> tuple[bool, str]:
    """拍图（摄像头 photo / 屏幕 shot）→ flash 直调看图判定，可靠版。"""
    import base64 as _b64
    import json as _json
    import httpx as _httpx
    tool = _ROOT / 'openclaw' / 'workspace' / 'tools' / 'camera-tool.py' if mode == 'camera' \
        else _ROOT / 'openclaw' / 'workspace' / 'tools' / 'screen-capture.py'
    py = _ROOT / '.venv' / 'Scripts' / 'python.exe'
    if mode == 'camera':
        cmd = [str(py), '-X', 'utf8', str(tool), 'photo', '', '0.1']
    else:
        cmd = [str(py), '-X', 'utf8', str(tool), 'shot', '--label', 'verify']
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8',
                             errors='replace', timeout=60).stdout
    except Exception as exc:  # noqa: BLE001
        return False, f'验收拍照失败：{type(exc).__name__}'
    img_path = ''
    for line in reversed([l.strip() for l in out.splitlines() if l.strip()]):
        try:
            r = _json.loads(line)
        except Exception:
            continue
        if r.get('ok') and r.get('path'):
            img_path = r['path']
            break
    if not img_path or not Path(img_path).exists():
        return False, '验收没拍到图'
    try:
        b64 = _b64.b64encode(Path(img_path).read_bytes()).decode('ascii')
    except Exception:
        return False, '验收读图失败'
    # flash 看图判定（经保险丝，与主动环同款直调）
    from guanjia.config_hub import ConfigHub as _CH
    hub = _CH()
    key = hub.key('zhipu')
    url = str(hub.get('models.brain.fuse', 'http://127.0.0.1:18793')).rstrip('/') + '/api/paas/v4/chat/completions'
    body = {
        'model': 'glm-5.3-flash',
        'messages': [{'role': 'user', 'content': [
            {'type': 'text', 'text': f'这个画面里能看到「{text}」吗？只回答"能"或"不能"。'},
            {'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{b64}'}},
        ]}],
        'thinking': {'type': 'enabled'}, 'reasoning_effort': 'low',
        'do_sample': False, 'stream': True,
    }
    parts: list[str] = []
    try:
        with _httpx.Client(timeout=60.0, trust_env=False) as c:
            with c.stream('POST', url, headers={'Authorization': f'Bearer {key}',
                                                'Content-Type': 'application/json'}, json=body) as r:
                for line in r.iter_lines():
                    if not line.startswith('data:'):
                        continue
                    data = line[5:].strip()
                    if data == '[DONE]':
                        break
                    for choice in (_json.loads(data).get('choices') or []):
                        parts.append((choice.get('delta') or {}).get('content') or '')
    except Exception:
        return False, '验收看图调用失败'
    answer = ''.join(parts).strip()
    ok = answer.startswith('能') or '能' in answer[:6]
    return ok, f'看图判定：{"能" if ok else "不能"}看到「{text}」'


def run_verify(verify: dict) -> tuple[bool, str]:
    """跑一条后置条件；返回 (是否通过, 证据/原因)。永不抛异常。"""
    try:
        kind = str(verify.get('kind') or '')
        if kind == 'process':
            name = str(verify.get('name') or '').strip()
            if not name:
                return False, 'verify.process 缺 name'
            r = _ps_json(['-Command',
                          f"$p = Get-Process -Name '{name}' -ErrorAction SilentlyContinue | Select-Object -First 1; if ($p) {{ @{{ ok = $true; pid = $p.Id }} | ConvertTo-Json -Compress }} else {{ @{{ ok = $false }} | ConvertTo-Json -Compress }}"])
            if r and r.get('ok'):
                return True, f"进程 {name} 在跑（pid {r.get('pid')}）"
            return False, f'没核实到进程 {name}'
        if kind == 'file':
            path = Path(str(verify.get('path') or ''))
            if not path.is_file():
                return False, f'文件不存在：{path}'
            raw_min = verify.get('min_kb')
            min_kb = float(raw_min) if raw_min is not None else 1.0  # 0 是合法值（只查存在），不能用 or 兜底
            size_kb = path.stat().st_size / 1024
            if size_kb < min_kb:
                return False, f'文件太小：{size_kb:.0f}KB < {min_kb:.0f}KB'
            return True, f'文件在：{path}（{size_kb:.0f}KB）'
        if kind == 'camera_contains':
            text = str(verify.get('text') or '').strip()
            if not text:
                return False, 'verify.camera_contains 缺 text'
            return _vision_contains(text, 'camera')
        if kind == 'screen_contains':
            text = str(verify.get('text') or '').strip()
            if not text:
                return False, 'verify.screen_contains 缺 text'
            return _vision_contains(text, 'screen')
        return False, f'未知验收类型：{kind or "(空)"}'
    except Exception as exc:  # noqa: BLE001
        return False, f'验收器异常：{type(exc).__name__}: {exc}'


__all__ = ['run_verify']