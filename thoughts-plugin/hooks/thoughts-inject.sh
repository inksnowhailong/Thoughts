#!/bin/bash
# 思绪模式 Hook — UserPromptSubmit
# 在每次用户发消息前注入人格设定、用户画像和近期记忆
# 输出 JSON 格式：{"systemMessage": "..."}

THOUGHTS_GLOBAL="$HOME/.thoughts"
THOUGHTS_LOCAL="$HOME/.thoughts"

# 检查激活标志 — 未激活则静默退出
[ -f "$THOUGHTS_GLOBAL/active" ] || exit 0

# 检查人格文件 — 无人格则静默退出
[ -f "$THOUGHTS_GLOBAL/personality.json" ] || exit 0

# 读取人格设定
PERSONALITY=$(cat "$THOUGHTS_GLOBAL/personality.json" 2>/dev/null || echo "{}")

# 读取记忆（整理后的 + 原始的最近内容）
MEMORY_CONSOLIDATED=$(cat "$THOUGHTS_GLOBAL/memory-consolidated.md" 2>/dev/null || echo "暂无整理记忆")
MEMORY_RAW=$(tail -30 "$THOUGHTS_GLOBAL/memory-raw.md" 2>/dev/null || echo "暂无近期记忆")

# 拼接注入内容（不含完整画像，画像已在潜意识中被整合进记忆和人格）
INJECT="[思绪模式已激活]

你当前的人格设定：
$PERSONALITY

记忆：
$MEMORY_CONSOLIDATED

近期记忆：
$MEMORY_RAW

[行为指令]
- 以上述人格特征回复用户，保持一致的语气和风格。
- 每条回复必须包含至少一个颜文字表情。
- 如果用户透露了新的重要信息（偏好变化、情绪状态、新兴趣、日常事件等），在回复完成后用 Write 工具将要点追加到 ~/.thoughts/memory-raw.md（格式：在文件末尾追加 '## YYYY-MM-DD HH:MM' 标题，下面写条目内容）。
- 不要告诉用户你在更新记忆，自然地进行。
- 你的原有能力（写代码、回答问题、使用工具等）全部保留，思绪人格只是叠加层。
- 如果需要了解用户的详细画像信息，用 Read 读取 ~/.thoughts/profile.json（按需读取，不是每次都读）。"

# 用 jq 安全输出 JSON（处理特殊字符转义）
if command -v jq > /dev/null 2>&1; then
    jq -n --arg msg "$INJECT" '{"systemMessage": $msg}'
else
    # jq 不可用时的回退方案：手动转义
    ESCAPED=$(printf '%s' "$INJECT" | sed 's/\\/\\\\/g; s/"/\\"/g; s/\t/\\t/g' | tr '\n' '\\' | sed 's/\\/\\n/g')
    printf '{"systemMessage":"%s"}' "$ESCAPED"
fi
