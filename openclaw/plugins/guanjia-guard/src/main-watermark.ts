import { APPROVAL_RETRY_MARKER } from "./approvals.ts";
import { hasVoiceTag, stripVoiceTag } from "./speak.ts";
import type { Store } from "./store.ts";

export type MainWatermarkSource = "voice" | "wechat" | "owner" | "unknown";
const MAIN = "agent:main:main";

/** 只持久化活动水位；不存正文、工具参数或 worker session。 */
export function recordMainWatermark(store: Store, input: { sessionKey?: string; text: string; channel?: string; accountId?: string; now?: number }) {
  if (input.sessionKey !== MAIN || stripVoiceTag(input.text).trim() === APPROVAL_RETRY_MARKER) return false;
  const source: MainWatermarkSource = hasVoiceTag(input.text) ? "voice"
    : input.channel === "openclaw-weixin" && typeof input.accountId === "string" && input.accountId.length > 0
      ? "wechat" : "unknown";
  const previous = store.readJson<{ seq?: number }>("main-watermark.json", {});
  const payload = { version: 1, last_activity_at: input.now ?? Date.now() / 1000, source,
    seq: (Number.isSafeInteger(previous.seq) ? previous.seq! : 0) + 1, session: MAIN };
  store.writeJson("main-watermark.json", payload);
  store.audit({ kind: "main-watermark", decision: "activity", source, seq: payload.seq, sessionKey: MAIN });
  return true;
}
