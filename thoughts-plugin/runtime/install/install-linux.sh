#!/bin/bash
# 思绪 — Linux 开机自启安装（systemd user unit）
# 用法: ./install-linux.sh <实例> [后端]
set -e

INSTANCE="$1"
BACKEND="${2:-auto}"
[ -z "$INSTANCE" ] && echo "用法: ./install-linux.sh <实例> [后端]" && exit 1

CLI="$(cd "$(dirname "$0")/.." && pwd)/cli.mjs"
NODE="$(command -v node)"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT="$UNIT_DIR/thoughts-$INSTANCE.service"
mkdir -p "$UNIT_DIR"

cat > "$UNIT" <<EOF
[Unit]
Description=Thoughts companion daemon ($INSTANCE)
After=default.target

[Service]
ExecStart=$NODE $CLI start $INSTANCE --backend=$BACKEND
Restart=on-failure

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now "thoughts-$INSTANCE.service"
echo "已安装 systemd 自启: thoughts-$INSTANCE.service"
echo "查看日志: journalctl --user -u thoughts-$INSTANCE -f"
echo "卸载:     systemctl --user disable --now thoughts-$INSTANCE.service && rm \"$UNIT\""
