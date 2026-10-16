import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { createApprovalLatches } from "./approvals.ts";
import { startApprovalIpc } from "./approval-ipc.ts";

const dir = fs.mkdtempSync(path.join(os.tmpdir(), "guanjia-approval-"));
const entries: Record<string, unknown> = {};
const store: any = {
  file: (name: string) => path.join(dir, name),
  readJson: (name: string, fallback: unknown) => entries[name] ?? fallback,
  writeJson: (name: string, data: unknown) => { entries[name] = data; },
  audit: () => {},
};
const check = (value: unknown, name: string) => {
  if (!value) throw new Error(`FAIL ${name}`);
  console.log(`PASS ${name}`);
};
const latches = createApprovalLatches(store);
const pending = latches.open({ toolName: "exec", params: { command: "del x.txt" }, sessionKey: "agent:main:main", source: "voice", summary: "删除文件" });
const stop = startApprovalIpc(store, latches, { info: () => {}, warn: () => {} });
const descriptorPath = store.file("approval-ipc.json");
for (let i = 0; i < 30 && !fs.existsSync(descriptorPath); i += 1) await new Promise((resolve) => setTimeout(resolve, 10));
const descriptor = JSON.parse(fs.readFileSync(descriptorPath, "utf8"));
const wrong = await fetch(`http://127.0.0.1:${descriptor.port}/decide`, {
  method: "POST", headers: { "content-type": "application/json", "x-guanjia-approval-cap": "wrong" },
  body: JSON.stringify({ id: pending.id, session: "agent:main:main", decision: "approve" }),
});
check(wrong.status === 403, "wrong capability is refused");
await stop();
fs.rmSync(dir, { recursive: true, force: true });
console.log("ALL PASS approval-ipc.test");
