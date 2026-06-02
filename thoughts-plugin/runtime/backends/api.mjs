// AgentBackend — 直接调用模型 API（默认 Anthropic Messages API）
// 完全脱离任何 AI 宿主 CLI，只需一个 API key。
// 该后端【没有】文件工具（agentic=false）：潜意识所需的记忆文件内容必须由
// daemon 预先拼进 prompt，模型返回的文本再由 daemon 落盘。

const DEFAULT_MODEL = 'claude-sonnet-4-6';
const ENDPOINT = 'https://api.anthropic.com/v1/messages';

export const apiBackend = {
    name: 'api',
    agentic: false,

    isAvailable() {
        return Boolean(process.env.ANTHROPIC_API_KEY);
    },

    /**
     * 调用 Messages API 跑一轮补全。
     * @param {{ prompt: string, timeoutMs?: number }} opts
     */
    async run({ prompt, timeoutMs = 60000 }) {
        const apiKey = process.env.ANTHROPIC_API_KEY;
        if (!apiKey) return { ok: false, text: '', error: '未设置 ANTHROPIC_API_KEY' };

        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), timeoutMs);
        try {
            const resp = await fetch(ENDPOINT, {
                method: 'POST',
                headers: {
                    'content-type': 'application/json',
                    'x-api-key': apiKey,
                    'anthropic-version': '2023-06-01',
                },
                body: JSON.stringify({
                    model: process.env.THOUGHTS_API_MODEL || DEFAULT_MODEL,
                    max_tokens: 1024,
                    messages: [{ role: 'user', content: prompt }],
                }),
                signal: controller.signal,
            });
            if (!resp.ok) {
                const detail = await resp.text().catch(() => '');
                return { ok: false, text: '', error: `API ${resp.status}: ${detail.slice(0, 300)}` };
            }
            const data = await resp.json();
            const text = (data.content ?? [])
                .filter((block) => block.type === 'text')
                .map((block) => block.text)
                .join('')
                .trim();
            return { ok: true, text };
        } catch (err) {
            return { ok: false, text: '', error: String(err?.message || err) };
        } finally {
            clearTimeout(timer);
        }
    },
};
