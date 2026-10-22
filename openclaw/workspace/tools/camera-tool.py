from base64 import b64encode
from io import BytesIO
import json
from pathlib import Path
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET

try:
    from PIL import Image, ImageChops
except ModuleNotFoundError:
    Image = None

DEV_SRC = Path(r'F:\coding\guanjia-v2-development\src')
sys.path.insert(0, str(DEV_SRC))

from guanjia.output.adapters.camera_ptz import (
    LocalCameraConfig,
    LocalCameraPtzAdapter,
    OnvifPtzDriver,
)

WORKSPACE_DIR = Path.home() / '.openclaw' / 'workspace'
ROOM_MEMORY_PATH = WORKSPACE_DIR / 'memory' / 'topics' / 'room-layout.md'

DEFAULT_ROOM_MEMORY = """# 房间空间方位与家庭视觉记忆
<!-- layer: L3 | source: spatial-memory | visibility: family-default-on -->

## 摄像头视角空间分布（从左到右）
- **最左侧 (Left 尽头)**：电脑桌、显示器、左侧白墙。看到显示器或左侧白墙说明镜头已经到了最左边，若要找冰箱或沙发，下一步必须往右 (`right`) 转！
- **中偏左 (Center-Left)**：银色双门冰箱（顶上堆放着几个纸箱）、白色柜子。往左 (`left`) 是电脑桌，往右 (`right`) 是客厅沙发。
- **中偏右至最右侧 (Right)**：客厅沙发（放着黄色和绿色抱枕、玩偶）、墙上装饰挂画、浅色窗帘。看到沙发或浅色窗帘说明镜头已在右侧客厅区，若要找冰箱或电脑桌，下一步必须往左 (`left`) 转！

## 家庭成员视觉与位置习惯
- **主人**：短发、戴眼镜的男士，常在最左侧电脑桌前办公，或在右侧沙发休息。
- **老婆**：女士，常在右侧客厅沙发区域活动或休息。
"""

DIRECTION_VECTORS: dict[str, tuple[float, float, str]] = {
    'left': (-0.6, 0.0, '左'),
    '左': (-0.6, 0.0, '左'),
    '向左': (-0.6, 0.0, '左'),
    '往左': (-0.6, 0.0, '左'),
    'right': (0.6, 0.0, '右'),
    '右': (0.6, 0.0, '右'),
    '向右': (0.6, 0.0, '右'),
    '往右': (0.6, 0.0, '右'),
    'up': (0.0, 0.5, '上'),
    '上': (0.0, 0.5, '上'),
    '向上': (0.0, 0.5, '上'),
    '往上': (0.0, 0.5, '上'),
    'down': (0.0, -0.5, '下'),
    '下': (0.0, -0.5, '下'),
    '向下': (0.0, -0.5, '下'),
    '往下': (0.0, -0.5, '下'),
}

SEARCH_INTENT_RE = re.compile(r'找|在哪|哪儿|哪里|有没有|有人吗|看到.*吗|看看.*在不在|老婆|主人|冰箱|沙发|电脑')



def read_camera_pan():
    """Read calibrated pan position."""
    try:
        pos = Path('F:/coding/guanjia-v2-development/data/vision/position.json').read_text(encoding='utf-8')
        d = json.loads(pos)
        if d.get('calibrated'):
            return float(d.get('pan') or 0.0)
    except Exception:
        pass
    return None


def smart_start_direction(question):
    """Start toward the most likely area based on who we're looking for."""
    if re.search(r'老婆|媳妇|她|妻子|wife', question):
        return 'right'
    if re.search(r'主人|我在|电脑桌|工作', question):
        return 'left'
    if re.search(r'沙发|电视|客厅|窗帘|抱枕', question):
        return 'right'
    return None


def ensure_spatial_memory() -> str:
    """Load or initialize compact room spatial layout + key family visual memory."""
    ROOM_MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not ROOM_MEMORY_PATH.exists():
        ROOM_MEMORY_PATH.write_text(DEFAULT_ROOM_MEMORY, encoding='utf-8')
    room_text = ROOM_MEMORY_PATH.read_text(encoding='utf-8', errors='replace').strip()
    return room_text[:900]


def append_spatial_note(note: str) -> None:
    """Optionally append newly discovered landmark notes to room-layout.md."""
    cleaned = note.strip()
    if not cleaned or len(cleaned) > 80:
        return
    try:
        current = ensure_spatial_memory()
        if cleaned in current:
            return
        with ROOM_MEMORY_PATH.open('a', encoding='utf-8') as f:
            f.write(f'\n- 近期观察补充：{cleaned}\n')
    except OSError:
        pass


def capture_jpeg_bytes(cfg: LocalCameraConfig, driver: OnvifPtzDriver) -> bytes:
    """Fast local snapshot capture using known ONVIF URI (120ms)."""
    default_uri = f'http://{cfg.ip}:{cfg.http_port}/onvifsnapshot/media_service/snapshot?channel=1&subtype=0'
    pwd_mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    pwd_mgr.add_password(None, f'http://{cfg.ip}/', cfg.username, cfg.password)
    pwd_mgr.add_password(None, default_uri, cfg.username, cfg.password)
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPDigestAuthHandler(pwd_mgr),
        urllib.request.HTTPBasicAuthHandler(pwd_mgr),
    )
    try:
        with opener.open(default_uri, timeout=3.0) as resp:
            raw = resp.read()
    except Exception:
        token = driver.resolve_profile_token()
        xml_text = driver._soap_post(
            cfg.onvif_media_url,
            f'<trt:GetSnapshotUri><trt:ProfileToken>{token}</trt:ProfileToken></trt:GetSnapshotUri>',
        )
        root = ET.fromstring(xml_text)
        uri = next(
            (elem.text.strip() for elem in root.iter() if elem.tag.endswith('Uri') and elem.text),
            default_uri,
        )
        pwd_mgr.add_password(None, uri, cfg.username, cfg.password)
        with opener.open(uri, timeout=4.0) as resp:
            raw = resp.read()

    if Image is None:
        return raw
    img = Image.open(BytesIO(raw)).convert('RGB')
    if img.width > 640:
        img = img.resize((640, 360))
    buf = BytesIO()
    img.save(buf, format='JPEG', quality=82)
    return buf.getvalue()


def load_vlm_endpoint() -> tuple[str, str]:
    oc_cfg = json.loads((Path.home() / '.openclaw' / 'openclaw.json').read_text(encoding='utf-8-sig'))
    # Use Zhipu's free glm-5.3-flash for vision (19.6M tokens, supports images)
    zp = oc_cfg['models']['providers']['zhipu']
    base = zp.get('baseUrl', 'http://127.0.0.1:18793/api/paas/v4')
    key = zp.get('apiKey', '')
    if isinstance(key, dict):
        # SecretRef: read the actual key from the secrets store
        key_file = Path.home() / '.openclaw' / 'secrets' / 'zhipu_key_file' / 'value'
        if key_file.exists():
            key = key_file.read_text(encoding='utf-8').strip()
        else:
            key = ''
    return base.rstrip('/'), key


def call_vlm_raw(base_url: str, api_key: str, jpeg_bytes: bytes, prompt: str, max_tokens: int = 500) -> str:
    b64_img = b64encode(jpeg_bytes).decode('ascii')
    payload = {
        'model': 'glm-5.3-flash',
        'messages': [
            {
                'role': 'user',
                'content': [
                    {'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{b64_img}'}},
                    {'type': 'text', 'text': prompt},
                ],
            }
        ],
        'max_tokens': max_tokens,
        'temperature': 0.1,
    }
    req = urllib.request.Request(
        f'{base_url}/chat/completions',
        data=json.dumps(payload).encode('utf-8'),
        headers={
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
        },
        method='POST',
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=30.0) as resp:
        body = json.loads(resp.read().decode('utf-8'))
    return str(body['choices'][0]['message']['content']).strip()


def active_visual_search_loop(
    cfg: LocalCameraConfig,
    driver: OnvifPtzDriver,
    adapter: LocalCameraPtzAdapter,
    question: str,
    initial_dir: str = '',
    initial_dur: float = 0.8,
    max_steps: int = 4,
) -> dict[str, object]:
    """Spatial-memory-guided visual exploration loop (~350 tokens context, zero tool schema bloat)."""
    base_url, api_key = load_vlm_endpoint()
    spatial_context = ensure_spatial_memory()
    history: list[str] = []
    steps_taken: list[str] = []

    if initial_dir and initial_dir in DIRECTION_VECTORS:
        h, v, cn = DIRECTION_VECTORS[initial_dir]
        adapter.move(h, v, initial_dur, auto_stop=True)
        steps_taken.append(f'先往{cn}转({initial_dur:.1f}s)')

    can_explore = bool(SEARCH_INTENT_RE.search(question))
    total_steps = max_steps if can_explore else 1

    for step_idx in range(1, total_steps + 1):
        jpeg = capture_jpeg_bytes(cfg, driver)
        if total_steps == 1:
            desc = call_vlm_raw(
                base_url,
                api_key,
                jpeg,
                f'【房间与家人记忆】\n{spatial_context}\n\n【主人问题】{question}\n（请结合房间方位与人物记忆，直接用简短自然口语回答，控制在两句话以内）',
            )
            prefix = f"{'、'.join(steps_taken)}后看到：" if steps_taken else ''
            return {'ok': True, 'moved': prefix, 'description': desc, 'steps': len(steps_taken) + 1}

        hist_text = '；'.join(history) if history else '无（这是第1眼）'
        step_prompt = (
            f'【房间空间方位与家庭记忆】\n{spatial_context}\n\n'
            f'【主人的寻找任务】：「{question}」\n'
            f'【本轮已转头记录】：{hist_text}\n'
            f'这是当前第 {step_idx}/{total_steps} 步看到的实时画面。\n'
            '请根据当前画面里看到的参照物（如显示器/白墙=最左、冰箱=中左、沙发/抱枕/窗帘=右侧），判断目前镜头朝向哪儿，以及目标应该往哪个方向转才能找到！\n'
            '请只输出一行 JSON（不要 markdown 代码块）：\n'
            '1) 如果当前画面已经看到了目标，输出：\n'
            '{"found": true, "answer": "用一两句自然口语告诉主人看到了什么、在房间哪个位置", "new_landmark": ""}\n'
            '2) 如果当前画面还没看到目标，必须根据参照物所在方位决定下一步往哪边转（left/right/up/down）以及转多少秒（0.5~1.8），输出：\n'
            '{"found": false, "seen": "当前看到的参照物", "next_direction": "left或right或up或down", "duration_sec": 1.2}'
        )
        raw_reply = call_vlm_raw(base_url, api_key, jpeg, step_prompt, max_tokens=500)
        parsed = None
        m = re.search(r'\{.*\}', raw_reply, re.S)
        if m:
            try:
                parsed = json.loads(m.group(0))
            except Exception:
                parsed = None

        if not isinstance(parsed, dict):
            return {'ok': True, 'moved': '、'.join(steps_taken), 'description': raw_reply, 'steps': step_idx}

        if parsed.get('found') is True or step_idx == total_steps:
            if isinstance(parsed.get('new_landmark'), str) and parsed['new_landmark'].strip():
                append_spatial_note(parsed['new_landmark'])
            ans = str(parsed.get('answer') or parsed.get('seen') or raw_reply).strip()
            prefix = f"（结合房间记忆自主转头 {len(steps_taken)} 次：{' -> '.join(steps_taken)}）" if steps_taken else ''
            return {'ok': True, 'moved': prefix, 'description': ans, 'steps': step_idx}

        seen_note = str(parsed.get('seen') or '未见目标').strip()
        next_dir = str(parsed.get('next_direction') or 'right').strip().lower()
        if next_dir not in DIRECTION_VECTORS:
            next_dir = 'right'
        try:
            dur = max(0.4, min(1.8, float(parsed.get('duration_sec') or 1.1)))
        except ValueError:
            dur = 1.1
        h, v, cn = DIRECTION_VECTORS[next_dir]
        adapter.move(h, v, dur, auto_stop=True)
        steps_taken.append(f'往{cn}转({dur:.1f}s)')
        history.append(f'第{step_idx}步看到[{seen_note}]->往{cn}转{dur:.1f}s')

    return {'ok': True, 'moved': '、'.join(steps_taken), 'description': '转了一圈还没看到目标。', 'steps': total_steps}



def calibrate_range(cfg: LocalCameraConfig, driver: OnvifPtzDriver, adapter: LocalCameraPtzAdapter,
                    speed: float = 0.6, step: float = 0.6, max_steps: int = 80) -> dict[str, object]:
    """Measure full pan travel by walking into both end stops, then return to center."""
    def small_frame():
        return Image.open(BytesIO(capture_jpeg_bytes(cfg, driver))).convert('L').resize((160, 90))

    def frame_diff(a, b):
        if a is None or b is None:
            return 1.0
        histogram = ImageChops.difference(a, b).histogram()
        return sum(i * c for i, c in enumerate(histogram)) / (255 * 160 * 90)

    def walk(direction: int) -> int:
        steps, stall, last = 0, 0, None
        while steps < max_steps:
            adapter.move(speed * direction, 0, step)
            time.sleep(0.4)
            current = small_frame()
            steps += 1
            if last is not None and frame_diff(last, current) < 0.01:
                stall += 1
                if stall >= 2:
                    break
            else:
                stall = 0
            last = current
        return steps

    adapter.move(-speed, 0, 1.0)
    time.sleep(0.4)
    walk(-1)
    travel_steps = walk(1)
    travel_sec = travel_steps * step
    adapter.set_travel(travel_sec)
    adapter.move(-speed, 0, travel_sec / 2)
    adapter.set_position(pan=0.0)
    return {'ok': True, 'travel_sec': round(travel_sec, 2), 'steps': travel_steps,
            'note': f'全范围约 {travel_sec:.1f} 秒，已回到中央'}


def main() -> None:
    action = sys.argv[1] if len(sys.argv) > 1 else 'look'
    arg1 = sys.argv[2] if len(sys.argv) > 2 else ''
    arg2 = sys.argv[3] if len(sys.argv) > 3 else ''
    arg3 = sys.argv[4] if len(sys.argv) > 4 else ''

    cfg = LocalCameraConfig(
        **{
            **LocalCameraConfig.from_local_env().__dict__,
            'profile_token': 'Profile000',
        }
    )
    driver = OnvifPtzDriver(cfg, timeout=3.5)
    driver._profile_token = 'Profile000'
    adapter = LocalCameraPtzAdapter(driver)

    try:
        if action == 'calibrate':
            result = calibrate_range(cfg, driver, adapter)
            print(json.dumps(result, ensure_ascii=False))
            return

        if action == 'photo':
            move_dir = arg1.strip().lower()
            if move_dir in DIRECTION_VECTORS:
                h, v, cn = DIRECTION_VECTORS[move_dir]
                try:
                    d = max(0.15, min(2.5, float(arg2))) if arg2 else 0.8
                except ValueError:
                    d = 0.8
                adapter.move(h, v, d, auto_stop=True)
            raw = capture_jpeg_bytes(cfg, driver)
            import time as _t
            _photos = DEV_SRC.parent / 'data' / 'vision' / 'photos'
            _photos.mkdir(parents=True, exist_ok=True)
            _name = 'photo_' + _t.strftime('%Y%m%d_%H%M%S') + '.jpg'
            (_photos / _name).write_bytes(raw)
            print(json.dumps({'ok': True, 'path': str(_photos / _name), 'size': len(raw)}, ensure_ascii=False))
            return

        if action == 'ptz':
            direction = arg1.strip().lower()
            if direction in ('stop', '停', '停止'):
                adapter.stop()
                print(json.dumps({'ok': True, 'action': 'stop', 'message': '摄像头已停止转动。'}, ensure_ascii=False))
                return
            if direction not in DIRECTION_VECTORS:
                print(json.dumps({'ok': False, 'error': f'不支持的方向: {arg1}，可选 left/right/up/down/stop'}, ensure_ascii=False))
                return
            h, v, cn = DIRECTION_VECTORS[direction]
            try:
                dur = max(0.15, min(2.5, float(arg2))) if arg2 else 0.55
            except ValueError:
                dur = 0.55
            adapter.move(h, v, dur, auto_stop=True)
            print(json.dumps({'ok': True, 'direction': cn, 'duration': dur, 'message': f'摄像头已往{cn}转了一下。'}, ensure_ascii=False))
            return

        if action == 'look':
            question = arg1.strip() or '看看现在画面里有什么'
            move_dir = arg2.strip().lower()
            try:
                dur = max(0.2, min(2.5, float(arg3))) if arg3 else 0.8
            except ValueError:
                dur = 0.8
            result = active_visual_search_loop(cfg, driver, adapter, question, move_dir, dur)
            print(json.dumps(result, ensure_ascii=False))
            return

        print(json.dumps({'ok': False, 'error': f'未知操作: {action}'}, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({'ok': False, 'error': f'{type(exc).__name__}: {exc}'}, ensure_ascii=False))


if __name__ == '__main__':
    main()






