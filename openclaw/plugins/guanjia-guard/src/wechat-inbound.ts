import fs from "node:fs";
import type { Store } from "./store.ts";

export interface InboundIdentity {
  timestamp: string;
  channel: "openclaw-weixin";
  accountId: string;
  senderUserId: string;
  senderSource: "context" | "sessionKey";
  sessionCandidate: string;
}

const FILE = "wechat-inbound-identities.jsonl";
const MAX_RECORDS = 100;
const safeAccountId = (value: unknown) => typeof value === "string" && /^[A-Za-z0-9_-]{1,128}$/.test(value);
const safePeerId = (value: unknown) => typeof value === "string" && /^[A-Za-z0-9._@-]{1,256}$/.test(value);
const directSession = /^agent:main:openclaw-weixin:([A-Za-z0-9_-]{1,128}):direct:([A-Za-z0-9._@-]{1,256})$/;

/** 已由真实 probe 证实：Guard context 提供 channel/accountId/senderId。 */
export function extractWechatInboundIdentity(ctx: any, now = new Date()): InboundIdentity | undefined {
  const accountId = ctx?.accountId;
  if (ctx?.channel !== "openclaw-weixin" || !safeAccountId(accountId)) return undefined;
  const direct = typeof ctx?.sessionKey === "string" ? directSession.exec(ctx.sessionKey) : undefined;
  const validDirect = direct?.[1] === accountId ? direct : undefined;
  const directPeer = validDirect?.[2];
  const fromContext = safePeerId(ctx?.senderId) ? ctx.senderId : undefined;
  const senderUserId = fromContext ?? directPeer;
  const senderSource = fromContext ? "context" : directPeer ? "sessionKey" : undefined;
  if (!senderUserId || !senderSource) return undefined;
  return {
    timestamp: now.toISOString(),
    channel: "openclaw-weixin",
    accountId,
    senderUserId,
    senderSource,
    sessionCandidate: validDirect?.[0] ?? `agent:main:openclaw-weixin:${accountId}:direct:${senderUserId.toLowerCase()}`,
  };
}

/** 目录由 Store 创建并继承 workspace/guanjia 的登录用户私有 ACL；只存身份元数据。 */
export function captureWechatInboundIdentity(store: Store, ctx: any, now?: Date): InboundIdentity | undefined {
  const record = extractWechatInboundIdentity(ctx, now);
  if (!record) {
    if (ctx?.channel === "openclaw-weixin") store.audit({ kind: "wechat-inbound-identity", decision: "rejected-missing-fields" });
    return undefined;
  }
  const target = store.file(FILE);
  let lines: string[] = [];
  try { lines = fs.readFileSync(target, "utf8").split(/\r?\n/).filter(Boolean).slice(-MAX_RECORDS + 1); } catch { /* 首次捕获 */ }
  lines.push(JSON.stringify(record));
  const tmp = `${target}.tmp`;
  fs.writeFileSync(tmp, `${lines.join("\n")}\n`, "utf8");
  fs.renameSync(tmp, target);
  store.audit({ kind: "wechat-inbound-identity", decision: "captured", accountId: record.accountId, senderUserId: record.senderUserId, sessionCandidate: record.sessionCandidate });
  return record;
}

/** 首次接线诊断只记录字段名与存在性，不读取或持久化正文/身份值。 */
export function diagnoseWechatInboundContext(store: Store, event: any, ctx: any) {
  const requester = ctx?.requester;
  const channel = requester?.channel ?? ctx?.channel ?? event?.channel;
  if (channel !== "openclaw-weixin") return false;
  store.audit({
    kind: "wechat-inbound-identity",
    decision: "context-probe",
    eventKeys: event && typeof event === "object" ? Object.keys(event).sort() : [],
    contextKeys: ctx && typeof ctx === "object" ? Object.keys(ctx).sort() : [],
    requesterKeys: requester && typeof requester === "object" ? Object.keys(requester).sort() : [],
    hasAccountId: typeof ctx?.accountId === "string",
    hasSenderId: typeof ctx?.senderId === "string",
  });
  return true;
}
