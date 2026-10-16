import crypto from "node:crypto";
import fs from "node:fs";
import http from "node:http";
import type { Store } from "./store";

/** 仅 127.0.0.1；能力串每次网关启动重生，绝不写入配置或审计。 */
export function startApprovalIpc(store: Store, latches: ReturnType<typeof import("./approvals").createApprovalLatches>,
                                 logger: { info: (m: string) => void; warn: (m: string) => void },
                                 onDecision?: (record: { sessionKey: string; id: string }, decision: "approve" | "reject") => void) {
  const capability = crypto.randomBytes(32).toString("base64url");
  const descriptor = store.file("approval-ipc.json");
  const reject = (res: http.ServerResponse, code: number) => { res.writeHead(code, { "content-type": "application/json" }); res.end('{"ok":false}'); };
  const server = http.createServer(async (req, res) => {
    if (req.socket.remoteAddress !== "127.0.0.1" || req.headers["x-guanjia-approval-cap"] !== capability) return reject(res, 403);
    const url = new URL(req.url ?? "/", "http://127.0.0.1");
    if (req.method === "GET" && url.pathname === "/latest") {
      const item = latches.latest(url.searchParams.get("session") ?? undefined);
      res.writeHead(200, { "content-type": "application/json" });
      return void res.end(JSON.stringify({ ok: true, pending: item ?? null }));
    }
    if (req.method === "POST" && url.pathname === "/decide") {
      const chunks: Buffer[] = [];
      for await (const chunk of req) chunks.push(chunk as Buffer);
      try {
        const body = JSON.parse(Buffer.concat(chunks).toString("utf8"));
        const record = typeof body.id === "string"
          && (body.decision === "approve" || body.decision === "reject")
          && latches.decide(body.id, body.decision, typeof body.session === "string" ? body.session : undefined);
        if (record) onDecision?.(record, body.decision);
        res.writeHead(record ? 200 : 409, { "content-type": "application/json" });
        return void res.end(JSON.stringify({ ok: Boolean(record) }));
      } catch {
        return reject(res, 400);
      }
    }
    return reject(res, 404);
  });
  server.listen(0, "127.0.0.1", () => {
    const address = server.address();
    if (!address || typeof address === "string") return;
    // 目录继承当前登录用户 ACL；文件仅含随机单次能力与 loopback 端口，不含参数。
    fs.writeFileSync(descriptor, JSON.stringify({ port: address.port, capability }), { encoding: "utf8", mode: 0o600 });
    logger.info(`guanjia approval ipc on 127.0.0.1:${address.port}`);
  });
  server.on("error", (err) => logger.warn(`guanjia approval ipc error: ${err.message}`));
  return () => new Promise<void>((resolve) => {
    try { fs.unlinkSync(descriptor); } catch { /* 已不存在 */ }
    server.close(() => resolve());
  });
}
