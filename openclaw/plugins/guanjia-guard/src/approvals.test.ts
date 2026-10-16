import { createApprovalLatches } from "./approvals.ts";

const entries: Record<string, unknown> = {};
const audits: any[] = [];
const store: any = {
  readJson: (name: string, fallback: unknown) => entries[name] ?? fallback,
  writeJson: (name: string, data: unknown) => { entries[name] = data; },
  audit: (entry: unknown) => audits.push(entry),
};
const check = (value: unknown, name: string) => {
  if (!value) throw new Error(`FAIL ${name}`);
  console.log(`PASS ${name}`);
};
const latches = createApprovalLatches(store);
const first = latches.open({ toolName: "exec", params: { command: "del a.txt", ask: "off" }, sessionKey: "agent:main:main", source: "voice", summary: "删除文件" });

check(!latches.consume("agent:main:main", "voice", "exec", { command: "del a.txt", ask: "off" }), "unapproved call is refused");
check(!latches.decide(first.id, "approve", "agent:other:main"), "wrong session cannot approve");
check(latches.decide(first.id, "approve", "agent:main:main"), "approved with matching session");
check(!latches.consume("agent:main:main", "voice", "exec", { ask: "off", command: "del b.txt" }), "different params are refused");
check(!latches.consume("agent:other:main", "voice", "exec", { command: "del a.txt", ask: "off" }), "different session is refused");
check(latches.consume("agent:main:main", "voice", "exec", { ask: "off", command: "del a.txt" }), "saved canonical params pass once");
check(!latches.consume("agent:main:main", "voice", "exec", { ask: "off", command: "del a.txt" }), "latch is burned after one use");

const rejected = latches.open({ toolName: "exec", params: { command: "del c.txt" }, sessionKey: "agent:main:main", source: "voice", summary: "删除文件" });
check(latches.decide(rejected.id, "reject", "agent:main:main"), "rejection is recorded");
check(!latches.consume("agent:main:main", "voice", "exec", { command: "del c.txt" }), "rejected call is refused");
const expired = latches.open({ toolName: "exec", params: { command: "del expired.txt" }, sessionKey: "agent:main:main", source: "voice", summary: "删除文件" });
const raw = entries["pending-approvals.json"] as any[];
raw.find((item) => item.id === expired.id).expiresAt = 0;
check(!latches.consume("agent:main:main", "voice", "exec", { command: "del expired.txt" }), "expired call is refused");
check(audits.some((entry) => entry.decision === "pending.open") && audits.some((entry) => entry.decision === "pending.executed"),
      "pending lifecycle is audited");
console.log("ALL PASS approvals.test");
