/**
 * 「说出来」工具：让家里电脑用管家音色（SCV）念一句话。
 *
 *   模型 ──speak{text}──> 本插件 ──POST 127.0.0.1:18795/say──> 语音守护进程的播放队列（打断照样生效）
 *
 * 不经过 shell；守护进程没跑、静音、游戏中都返回明确的「没有念：原因」。分级 L1（levels.json）。
 * 工具搜索模式下它是延期工具：模型用 tool_call（id: speak，args: {text}）调用，AGENTS.md 里写明了。
 */

export const DEFAULT_SPEAK_URL = "http://127.0.0.1:18795/say";
export const SPEAK_MAX_CHARS = 300;
export const SPEAK_TIMEOUT_MS = 3_000;
/** 语音轮标记「[语音 2026-10-10 18:05 周六]」；前面允许再有一个网关加的 [时间] 信封 */
export const VOICE_TAG_RE = /^\s*(?:\[[^\]]*\]\s*)?\[语音[^\]]*\]\s*/;

/** 去掉语音轮标记，口头确认 / 习惯判断按主人说的原话做 */
export function stripVoiceTag(text: string): string {
  return text.replace(VOICE_TAG_RE, "");
}

type FetchLike = (
  url: string,
  init: { method: string; headers: Record<string, string>; body: string; signal: AbortSignal },
) => Promise<{ status: number; text(): Promise<string> }>;

export interface SpeakToolOptions {
  url?: string;
  fetchImpl?: FetchLike;
  timeoutMs?: number;
}

const REASON_CN: Record<string, string> = {
  muted: "电脑现在是静音状态（托盘里点过静音）",
  gaming: "主人在打游戏，电脑暂时不出声",
  empty: "要念的内容是空的",
  too_long: `一次最多念 ${SPEAK_MAX_CHARS} 字`,
};

function textResult(text: string, details: Record<string, unknown>) {
  return { content: [{ type: "text", text }], details };
}

export function createSpeakTool(opts: SpeakToolOptions = {}) {
  const url = opts.url ?? DEFAULT_SPEAK_URL;
  const doFetch = opts.fetchImpl ?? (globalThis.fetch as unknown as FetchLike);
  const timeoutMs = opts.timeoutMs ?? SPEAK_TIMEOUT_MS;
  return {
    name: "speak",
    label: "Speak aloud",
    description:
      "Speak a short sentence aloud on the home PC speakers in the butler's own voice (SCV). " +
      "Use when the owner asks to read or say something out loud on the computer (念一句 / 读出来 / 在电脑上说). " +
      "Input: text, up to 300 characters. Fails with a clear reason if the voice daemon is not running, muted, or the owner is gaming.",
    parameters: {
      type: "object",
      properties: { text: { type: "string", description: "要念出来的话（口语，最多 300 字）" } },
      required: ["text"],
      additionalProperties: false,
    },
    async execute(_toolCallId: string, params: { text?: unknown }) {
      const text = typeof params?.text === "string" ? params.text.trim() : "";
      if (!text) return textResult(`没有念：${REASON_CN.empty}。`, { ok: false, reason: "empty" });
      if (text.length > SPEAK_MAX_CHARS) return textResult(`没有念：${REASON_CN.too_long}，请缩短。`, { ok: false, reason: "too_long" });
      let res: { status: number; text(): Promise<string> };
      try {
        res = await doFetch(url, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text }),
          signal: AbortSignal.timeout(timeoutMs),
        });
      } catch (err) {
        return textResult(
          `没有念：电脑上的语音守护进程没有在运行（${url} 连不上）。请主人看一眼托盘，或重启电脑后再试。`,
          { ok: false, reason: "daemon_down", error: String(err) },
        );
      }
      let body: any = {};
      try {
        body = JSON.parse(await res.text());
      } catch {
        // 非 JSON 按状态码处理
      }
      if (res.status === 200 && body?.ok) return textResult(`已让电脑念出来：「${text}」`, { ok: true, reason: "queued" });
      const reason = String(body?.reason ?? `http_${res.status}`);
      return textResult(`没有念：${REASON_CN[reason] ?? `语音守护进程拒绝了（${reason}）`}。`, { ok: false, reason });
    },
  };
}
