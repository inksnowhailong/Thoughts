#!/bin/bash
# 思绪 — macOS 开机自启安装（launchd）
# 用法: ./install-mac.sh <实例> [后端]
set -e

INSTANCE="$1"
BACKEND="${2:-auto}"
[ -z "$INSTANCE" ] && echo "用法: ./install-mac.sh <实例> [后端]" && exit 1

CLI="$(cd "$(dirname "$0")/.." && pwd)/cli.mjs"
NODE="$(command -v node)"
LABEL="com.thoughts.$INSTANCE"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$NODE</string>
        <string>$CLI</string>
        <string>start</string>
        <string>$INSTANCE</string>
        <string>--backend=$BACKEND</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
</dict>
</plist>
EOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "已安装 launchd 自启: $LABEL"
echo "卸载: launchctl unload \"$PLIST\" && rm \"$PLIST\""
