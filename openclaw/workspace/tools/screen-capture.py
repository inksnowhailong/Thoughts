"""屏幕截图工具：静默截取主屏幕，保存到 data/vision/screenshots/。

用途（发起人 10-24 需求）：游戏模式中用户喊「管家」问攻略时，
管家截屏看游戏画面——识别英雄血量、装备、局势等关键信息后给建议。
"""

import argparse
import json
import sys
import time
from pathlib import Path

OUT_DIR = Path(r'F:\coding\guanjia-v2-development\data\vision\screenshots')


def capture_screen():
    """用 PIL 的 ImageGrab 截取主屏幕，返回 JPEG bytes。"""
    from PIL import ImageGrab
    import io
    img = ImageGrab.grab()
    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=75)
    return buf.getvalue()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['shot'], help='shot=截屏')
    parser.add_argument('--label', default='', help='截图标签（存入文件名）')
    args = parser.parse_args()

    if args.action != 'shot':
        print(json.dumps({'ok': False, 'error': 'unknown action'}))
        return

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime('%Y%m%d_%H%M%S')
    label = ''.join(c for c in args.label if c.isalnum() or c in '_-')[:20]
    name = f'screen_{ts}' + (f'_{label}' if label else '') + '.jpg'
    path = OUT_DIR / name

    try:
        data = capture_screen()
        path.write_bytes(data)
        # MEDIA: 指令（网关捕获为回复附件——用户在语音/微信里能看到截图）
        print(json.dumps({'ok': True, 'path': str(path), 'size': len(data)}, ensure_ascii=False))
        print(f'MEDIA: {path}')
    except Exception as e:
        print(json.dumps({'ok': False, 'error': str(e)}, ensure_ascii=False))


if __name__ == '__main__':
    main()