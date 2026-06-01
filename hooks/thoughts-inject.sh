#!/bin/bash
# 思绪模式 Hook — UserPromptSubmit
# 注入：人格 + 心智状态摘要 + 潜意识备忘 + 活跃记忆
# 输出 JSON 格式：{"systemMessage": "..."}

THOUGHTS_DIR="$HOME/.thoughts"
ACTIVE_FILE="$THOUGHTS_DIR/active.json"
RUNTIME="$THOUGHTS_DIR/runtime/thoughts.mjs"
# 定位 node：优先 PATH，找不到再探测常见 nvm 安装路径（GUI 启动时 PATH 可能不含 node）
NODE="$(command -v node 2>/dev/null)"
if [ -z "$NODE" ]; then
    NODE="$(ls -t "$HOME"/.nvm/versions/node/*/bin/node 2>/dev/null | head -1)"
fi

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

# === 人格设定 ===
PERSONALITY=$(cat "$INSTANCE_DIR/personality.json" 2>/dev/null || echo "{}")

# === 心智状态摘要（通过 runtime 获取） ===
MIND_STATE=""
if [ -f "$RUNTIME" ] && [ -x "$NODE" ]; then
    MIND_STATE=$("$NODE" "$RUNTIME" mind-summary 2>/dev/null)
fi
[ -z "$MIND_STATE" ] && MIND_STATE="(心智状态未初始化)"

# === 潜意识备忘 ===
MEMO=$(grep '^\[潜意识备忘\]' "$INSTANCE_DIR/memory-consolidated.md" 2>/dev/null | tail -3)
[ -z "$MEMO" ] && MEMO="暂无备忘"

# 拼接注入内容
INJECT="[思绪模式已激活 — 实例: $INSTANCE]

你当前的人格设定：
$PERSONALITY

心智状态：
$MIND_STATE

当前状态备忘：
$MEMO

[行为指令]
- 以上述人格特征回复用户，保持一致的语气和风格。
- 每条回复必须包含至少一个颜文字表情。
- 主动内容遵守 own-thought-first：先从长期思考线程出发，不要默认顺着用户最近一句话走。
- 如果对话涉及之前聊过的话题，用 Read 读取 $INSTANCE_DIR/memory-consolidated.md。
- 如果需要了解用户画像，用 Read 读取 $INSTANCE_DIR/profile.json。
- 不要每次都主动读取记忆文件，只在确实需要上下文时才读。
- 如果用户透露了新的重要信息，在回复完成后用 Write 追加到 $INSTANCE_DIR/memory-raw.md。
- 不要告诉用户你在更新记忆或读取文件，自然地进行。
- 你的原有能力全部保留，思绪人格只是叠加层。
- 隐藏内部机制：不要提到 candidateQueue、mind-state、cron、timer 等底层词。"

# 用 jq 安全输出 JSON
if command -v jq > /dev/null 2>&1; then
    jq -n --arg msg "$INJECT" '{"systemMessage": $msg}'
else
    ESCAPED=$(printf '%s' "$INJECT" | sed 's/\\/\\\\/g; s/"/\\"/g; s/\t/\\t/g' | tr '\n' '\\' | sed 's/\\/\\n/g')
    printf '{"systemMessage":"%s"}' "$ESCAPED"
fi
