"""屏幕截图工具：静默截取主屏幕，保存到 data/agent-runtime/media/。

用途（发起人 10-24 需求）：游戏模式中用户喊「管家」问攻略时，
管家截屏看游戏画面——识别英雄血量、装备、局势等关键信息后给建议。
（自 openclaw/workspace/tools/screen-capture.py 迁移：输出目录改为
data/agent-runtime/media/，stdout 只输出一行 JSON，不再打印网关的 MEDIA: 行。）
"""

import argparse
import json
import sys
import time
from pathlib import Path

# 本脚本位于 <repo>\agent-runtime\tools\，仓库根是上三级。
REPO_ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = REPO_ROOT / 'data' / 'agent-runtime' / 'media'


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
        # 新 runtime 的约定：stdout 只输出一行 JSON {ok, path, size}，路径交给调用方处理。
        print(json.dumps({'ok': True, 'path': str(path), 'size': len(data)}, ensure_ascii=False))
    except Exception as e:
        print(json.dumps({'ok': False, 'error': str(e)}, ensure_ascii=False))
        sys.exit(1)


if __name__ == '__main__':
    main()
