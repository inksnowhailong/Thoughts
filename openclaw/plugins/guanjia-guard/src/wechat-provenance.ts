import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import type { Store } from "./store.ts";

const MAIN = "agent:main:main";
const TTL_MS = 30_000;
type Record = { version: 1; runId: string; sessionKey: string; accountId: string; senderUserId: string; createdAt: number; expiresAt: number; nonce: string; signature: string };
const stable = (value: Omit<Record, "signature">) => JSON.stringify(value);
const keyFile = (store: Store) => store.file("wechat-provenance.key");
const recordFile = (store: Store, runId: string) => path.join(store.file("wechat-provenance"), crypto.createHash("sha256").update(runId).digest("hex") + ".json");

/** 仅消费 shim 已签名、同一 OpenClaw run 的主人微信来源记录。 */
export function consumeOwnerWechatProvenance(store: Store, ctx: any, now = Date.now()): boolean {
  if (ctx?.channel !== "openclaw-weixin" || ctx?.sessionKey !== MAIN || typeof ctx?.runId !== "string") return false;
  const file = recordFile(store, ctx.runId);
  let record: Record;
  let key: Buffer;
  try {
    record = JSON.parse(fs.readFileSync(file, "utf8"));
    key = fs.readFileSync(keyFile(store));
  } catch { return false; }
  const unsigned = (({ signature: _signature, ...rest }) => rest)(record);
  const expected = crypto.createHmac("sha256", key).update(stable(unsigned)).digest("hex");
  const supplied = typeof record.signature === "string" && /^[a-f0-9]{64}$/.test(record.signature) ? record.signature : "";
  const valid = record.version === 1 && record.runId === ctx.runId && record.sessionKey === MAIN
    && record.accountId === ctx.accountId && record.expiresAt >= now && record.createdAt <= now
    && typeof record.nonce === "string" && /^[A-Za-z0-9_-]{32,}$/.test(record.nonce)
    && Boolean(supplied) && crypto.timingSafeEqual(Buffer.from(expected, "hex"), Buffer.from(supplied, "hex"));
  if (!valid) { store.audit({ kind: "wechat-provenance", decision: "rejected-invalid" }); return false; }
  try {
    const config = JSON.parse(fs.readFileSync(path.join(os.homedir(), ".openclaw", "openclaw.json"), "utf8"));
    const commands = config?.commands ?? {};
    if (!Array.isArray(commands.ownerAllowFrom) || !Array.isArray(commands.allowFrom)
      || !commands.ownerAllowFrom.includes(record.senderUserId) || !commands.allowFrom.includes(record.senderUserId)) {
      store.audit({ kind: "wechat-provenance", decision: "rejected-not-owner", accountId: record.accountId });
      return false;
    }
    fs.renameSync(file, `${file}.consumed`); // 原子烧毁，重复/replay 不能再用
  } catch {
    store.audit({ kind: "wechat-provenance", decision: "rejected-consume" });
    return false;
  }
  store.audit({ kind: "wechat-provenance", decision: "owner-consumed", accountId: record.accountId });
  return true;
}

export const provenanceTtlMs = TTL_MS;
