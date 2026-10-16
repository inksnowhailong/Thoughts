import crypto from "node:crypto";
import type { Store } from "./store";

export const APPROVAL_RETRY_MARKER = "__guanjia_approved_retry__";
const TTL_MS = 10 * 60_000;

export interface PendingApproval {
  id: string;
  toolName: string;
  canonicalParams: string;
  sessionKey: string;
  source: string;
  summary: string;
  fingerprint: string;
  createdAt: number;
  expiresAt: number;
  status: "open" | "approved" | "rejected" | "expired" | "executed" | "failed";
}

function stableJson(value: unknown): string {
  if (value === null || typeof value === "boolean" || typeof value === "number" || typeof value === "string") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value as Record<string, unknown>).sort().map((key) =>
      `${JSON.stringify(key)}:${stableJson((value as Record<string, unknown>)[key])}`).join(",")}}`;
  }
  throw new TypeError("工具参数必须是 JSON 值");
}

export function canonicalParams(params: Record<string, unknown>): string {
  return stableJson(params);
}

export function createApprovalLatches(store: Store) {
  const name = "pending-approvals.json";
  const read = (): PendingApproval[] => store.readJson<PendingApproval[]>(name, []);
  const write = (records: PendingApproval[]) => store.writeJson(name, records);
  const expire = (records: PendingApproval[]) => {
    const now = Date.now();
    let changed = false;
    for (const record of records) {
      if ((record.status === "open" || record.status === "approved") && record.expiresAt <= now) {
        record.status = "expired";
        store.audit({ kind: "pending", decision: "pending.expired", pendingId: record.id, sessionKey: record.sessionKey, summary: record.summary });
        changed = true;
      }
    }
    if (changed) write(records);
    return records;
  };
  const latest = (sessionKey?: string) => {
    const records = expire(read()).filter((record) => !sessionKey || record.sessionKey === sessionKey);
    return records.filter((record) => record.status === "open" || record.status === "approved").sort((a, b) => b.createdAt - a.createdAt)[0];
  };

  return {
    open(input: Omit<PendingApproval, "id" | "canonicalParams" | "fingerprint" | "createdAt" | "expiresAt" | "status"> & { params: Record<string, unknown> }) {
      const now = Date.now();
      const canonical = canonicalParams(input.params);
      const record: PendingApproval = {
        id: crypto.randomUUID(),
        toolName: input.toolName,
        canonicalParams: canonical,
        sessionKey: input.sessionKey,
        source: input.source,
        summary: input.summary,
        fingerprint: crypto.createHash("sha256").update(`${input.toolName}\0${canonical}`).digest("hex"),
        createdAt: now,
        expiresAt: now + TTL_MS,
        status: "open",
      };
      const records = expire(read());
      records.push(record);
      write(records);
      store.audit({ kind: "pending", decision: "pending.open", pendingId: record.id, sessionKey: record.sessionKey, summary: record.summary, expiresAt: record.expiresAt });
      return record;
    },
    latest(sessionKey?: string) {
      const record = latest(sessionKey);
      return record && { id: record.id, summary: record.summary, status: record.status, expiresAt: record.expiresAt };
    },
    decide(id: string, decision: "approve" | "reject", sessionKey?: string) {
      const records = expire(read());
      const record = records.find((item) => item.id === id && (!sessionKey || item.sessionKey === sessionKey));
      if (!record || record.status !== "open") return undefined;
      record.status = decision === "approve" ? "approved" : "rejected";
      write(records);
      store.audit({ kind: "pending", decision: decision === "approve" ? "pending.approved" : "pending.rejected",
                    pendingId: record.id, sessionKey: record.sessionKey, summary: record.summary });
      return record;
    },
    instruction(sessionKey: string) {
      const record = latest(sessionKey);
      if (!record || record.status !== "approved") return undefined;
      return "（系统：已批准一件待确认操作。现在只可用已批准的原工具和完全相同的参数重试一次；参数不同会被拒绝。若无法重试，如实说明，不要尝试变体。）";
    },
    consume(sessionKey: string | undefined, source: string, toolName: string, params: Record<string, unknown>) {
      if (!sessionKey) return false;
      const records = expire(read());
      const canonical = canonicalParams(params);
      const record = records.filter((item) => item.status === "approved").sort((a, b) => b.createdAt - a.createdAt)
        .find((item) => item.sessionKey === sessionKey && item.source === source && item.toolName === toolName && item.canonicalParams === canonical);
      if (!record) return false;
      record.status = "executed"; // 执行入口前即烧毁，工具失败也不能重复执行
      write(records);
      store.audit({ kind: "pending", decision: "pending.executed", pendingId: record.id, sessionKey, summary: record.summary });
      return true;
    },
  };
}
