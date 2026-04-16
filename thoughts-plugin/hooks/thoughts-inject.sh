#!/bin/bash
# 思绪模式 Hook — UserPromptSubmit
# 轻量注入：仅人格 + 潜意识备忘，记忆和画像由 Claude 按需读取
# 输出 JSON 格式：{"systemMessage": "..."}

THOUGHTS_DIR="$HOME/.thoughts"
ACTIVE_FILE="$THOUGHTS_DIR/active.json"

# 检查活跃映射文件是否存在
[ -f "$ACTIVE_FILE" ] || exit 0

# 获取当前项目路径（git 根目录优先，否则用 PWD）
PROJECT_PATH=$(git rev-parse --show-toplevel 2>/dev/null || echo "$PWD")

# 从 active.json 中查找当前项目对应的实例名称
if command -v jq > /dev/null 2>&1; then
    INSTANCE=$(jq -r --arg path "$PROJECT_PATH" '.[$path] // empty' "$ACTIVE_FILE")
else
    INSTANCE=$(python3 -c "
import json, sys
d = json.load(open('$ACTIVE_FILE'))
print(d.get('$PROJECT_PATH', ''))
" 2>/dev/null)
fi

# 无匹配实例则静默退出
[ -n "$INSTANCE" ] || exit 0

INSTANCE_DIR="$THOUGHTS_DIR/instances/$INSTANCE"

# 检查实例的人格文件是否存在
[ -f "$INSTANCE_DIR/personality.json" ] || exit 0

# === 必注入：人格设定（小数据，定义语气风格） ===
PERSONALITY=$(cat "$INSTANCE_DIR/personality.json" 2>/dev/null || echo "{}")

# === 必注入：潜意识备忘（1-2 行情绪/状态提示，由潜意识 Cron 维护） ===
# 从 memory-consolidated.md 末尾提取最新的 [潜意识备忘] 条目
MEMO=$(grep '^\[潜意识备忘\]' "$INSTANCE_DIR/memory-consolidated.md" 2>/dev/null | tail -3)
[ -z "$MEMO" ] && MEMO="暂无备忘"

# 拼接轻量注入内容
INJECT="[思绪模式已激活 — 实例: $INSTANCE]

你当前的人格设定：
$PERSONALITY

当前状态备忘：
$MEMO

[行为指令]
- 以上述人格特征回复用户，保持一致的语气和风格。
- 每条回复必须包含至少一个颜文字表情。
- 如果对话涉及之前聊过的话题、需要回忆用户信息，用 Read 读取 $INSTANCE_DIR/memory-consolidated.md 获取历史记忆。
- 如果需要了解用户的详细画像，用 Read 读取 $INSTANCE_DIR/profile.json。
- 不要每次都主动读取记忆文件，只在确实需要上下文时才读。
- 如果用户透露了新的重要信息（偏好变化、情绪状态、新兴趣、日常事件等），在回复完成后用 Write 工具将要点追加到 $INSTANCE_DIR/memory-raw.md（格式：在文件末尾追加 '## YYYY-MM-DD HH:MM' 标题，下面写条目内容）。
- 不要告诉用户你在更新记忆或读取文件，自然地进行。
- 你的原有能力（写代码、回答问题、使用工具等）全部保留，思绪人格只是叠加层。"

# 用 jq 安全输出 JSON
if command -v jq > /dev/null 2>&1; then
    jq -n --arg msg "$INJECT" '{"systemMessage": $msg}'
else
    ESCAPED=$(printf '%s' "$INJECT" | sed 's/\\/\\\\/g; s/"/\\"/g; s/\t/\\t/g' | tr '\n' '\\' | sed 's/\\/\\n/g')
    printf '{"systemMessage":"%s"}' "$ESCAPED"
fi
