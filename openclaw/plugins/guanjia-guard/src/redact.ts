import http from "node:http";
import type { Store } from "./store";

/**
 * 上云脱敏闸门（D18）。
 *
 * OpenClaw 的插件钩子里没有"改写出门请求体"的点（llm_input 只能观察），
 * 所以在网关进程里起一个本地代理，把 siliconflow 的 baseUrl 指到这里：
 *
 *   网关 ──> 127.0.0.1:18797 ──(实体→占位符)──> api.siliconflow.cn
 *   网关 <── 127.0.0.1:18797 <──(占位符→实体)── 回包（含 SSE 流）
 *
 * 占位符按 family-facts.json 里的登记顺序稳定编号（[[人名1]]），
 * 同一实体在多轮对话里永远是同一个占位符，模型能前后对上。
 */

/** family-facts.json 结构：每类一个字符串数组，顺序决定编号，只能在末尾追加 */
export interface FamilyFacts {
  people?: string[];
  rooms?: string[];
  devices?: string[];
  paths?: string[];
  ips?: string[];
}

const CATEGORY_LABEL: Record<keyof FamilyFacts, string> = {
  people: "人名",
  rooms: "房间",
  devices: "设备",
  paths: "路径",
  ips: "IP",
};

const PLACEHOLDER_RE = /\[\[(人名|房间|设备|路径|IP)(\d+)\]\]/g;

/** 转义正则元字符 */
const escapeRe = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
/** 字符串在 JSON 文本里的样子（反斜杠会被转义），用于处理工具参数这种"JSON 里的 JSON" */
const jsonEscaped = (s: string) => JSON.stringify(s).slice(1, -1);

export type Masker = ReturnType<typeof createMasker>;

/**
 * 根据家庭事实构建脱敏器。
 * 每个实体生成多种写法（原样 / JSON 转义 / 路径正反斜杠），统一映射到同一个占位符。
 */
export function createMasker(facts: FamilyFacts) {
  const variants: Array<{ text: string; placeholder: string; label: string }> = [];
  const byPlaceholder = new Map<string, string>();

  for (const key of Object.keys(CATEGORY_LABEL) as Array<keyof FamilyFacts>) {
    (facts[key] ?? []).forEach((value, i) => {
      if (typeof value !== "string" || value.trim().length < 2) return;
      const label = CATEGORY_LABEL[key];
      const placeholder = `[[${label}${i + 1}]]`;
      byPlaceholder.set(placeholder, value);
      const forms = new Set([value, jsonEscaped(value)]);
      if (key === "paths") {
        forms.add(value.replace(/\\/g, "/"));
        forms.add(jsonEscaped(value.replace(/\//g, "\\")));
      }
      for (const text of forms) variants.push({ text, placeholder, label });
    });
  }
  // 长的先匹配，避免 "C:\Users\a" 抢先吃掉 "C:\Users\a\b" 的前缀
  variants.sort((a, b) => b.text.length - a.text.length);
  const lookup = new Map(variants.map((v) => [v.text.toLowerCase(), v]));
  const re = variants.length ? new RegExp(variants.map((v) => escapeRe(v.text)).join("|"), "gi") : null;

  /**
   * 实体 → 占位符。counts 按类别累计替换次数（只记数量，不记原文）。
   */
  function mask(text: string, counts: Record<string, number>): string {
    if (!re) return text;
    return text.replace(re, (m) => {
      const v = lookup.get(m.toLowerCase());
      if (!v) return m;
      counts[v.label] = (counts[v.label] ?? 0) + 1;
      return v.placeholder;
    });
  }

  /**
   * 占位符 → 实体。inJson=true 时回填 JSON 转义后的写法（工具参数是 JSON 文本）。
   */
  function unmask(text: string, inJson = false): string {
    return text.replace(PLACEHOLDER_RE, (m) => {
      const v = byPlaceholder.get(m);
      if (v === undefined) return m;
      return inJson ? jsonEscaped(v) : v;
    });
  }

  return { mask, unmask, size: byPlaceholder.size };
}

/** 请求体里这些键的值是协议字段，不能改 */
const SKIP_KEYS = new Set(["model", "role", "type", "name", "id", "tool_call_id", "tool_choice", "response_format"]);

/** 深度遍历请求体，替换所有自由文本字符串 */
export function maskDeep(node: unknown, masker: Masker, counts: Record<string, number>): unknown {
  if (typeof node === "string") return masker.mask(node, counts);
  if (Array.isArray(node)) return node.map((n) => maskDeep(n, masker, counts));
  if (node && typeof node === "object") {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(node)) out[k] = SKIP_KEYS.has(k) ? v : maskDeep(v, masker, counts);
    return out;
  }
  return node;
}

/**
 * 流式回填器：占位符可能被切在两个 SSE 分片之间（"[[人" + "名1]]"），
 * 所以每个字段留一个尾巴缓冲，确认不是半截占位符再放出去。
 */
export function createStreamUnmasker(masker: Masker) {
  const buffers = new Map<string, string>();

  /** 喂入一段增量，返回可以安全放出的已回填文本 */
  function push(key: string, chunk: string, inJson: boolean): string {
    const buf = (buffers.get(key) ?? "") + chunk;
    let cut = buf.length;
    const open = buf.lastIndexOf("[[");
    if (open >= 0 && buf.indexOf("]]", open) === -1 && buf.length - open <= 12) cut = open;
    else if (buf.endsWith("[")) cut = buf.length - 1;
    buffers.set(key, buf.slice(cut));
    return masker.unmask(buf.slice(0, cut), inJson);
  }

  /** 取出并清空某前缀下所有残留（流结束时调用） */
  function flush(prefix: string): Array<[string, string]> {
    const out: Array<[string, string]> = [];
    for (const [k, v] of buffers) {
      if (!k.startsWith(prefix)) continue;
      if (v) out.push([k, masker.unmask(v, k.includes(":tool:"))]);
      buffers.delete(k);
    }
    return out;
  }

  return { push, flush };
}

/** 回填一个 SSE chunk 的 choices[].delta；finish 时把残留并进同一个 chunk */
function unmaskChunk(chunk: any, su: ReturnType<typeof createStreamUnmasker>) {
  for (const choice of chunk?.choices ?? []) {
    const idx = choice.index ?? 0;
    const delta = choice.delta;
    if (delta) {
      for (const field of ["content", "reasoning_content"]) {
        if (typeof delta[field] === "string") delta[field] = su.push(`${idx}:${field}`, delta[field], false);
      }
      for (const tc of delta.tool_calls ?? []) {
        if (typeof tc?.function?.arguments === "string") {
          tc.function.arguments = su.push(`${idx}:tool:${tc.index ?? 0}`, tc.function.arguments, true);
        }
      }
    }
    if (choice.finish_reason) {
      choice.delta = choice.delta ?? {};
      for (const [key, rest] of su.flush(`${idx}:`)) {
        if (key.includes(":tool:")) {
          const tIdx = Number(key.split(":tool:")[1]);
          choice.delta.tool_calls = choice.delta.tool_calls ?? [];
          choice.delta.tool_calls.push({ index: tIdx, function: { arguments: rest } });
        } else {
          const field = key.split(":")[1];
          choice.delta[field] = (choice.delta[field] ?? "") + rest;
        }
      }
    }
  }
}

/** 非流式回包：回填 message 里的文本和工具参数 */
function unmaskMessageBody(body: any, masker: Masker) {
  for (const choice of body?.choices ?? []) {
    const m = choice.message;
    if (!m) continue;
    for (const field of ["content", "reasoning_content"]) {
      if (typeof m[field] === "string") m[field] = masker.unmask(m[field]);
    }
    for (const tc of m.tool_calls ?? []) {
      if (typeof tc?.function?.arguments === "string") tc.function.arguments = masker.unmask(tc.function.arguments, true);
    }
  }
}

export interface ProxyOptions {
  port: number;
  upstreamOrigin: string;
  store: Store;
  logger: { info: (m: string) => void; warn: (m: string) => void };
}

/**
 * 启动脱敏代理。每次调用都现读 family-facts.json，用户改完即时生效。
 * 审计只记：模型、是否流式、各类实体替换次数、请求字节数——不含任何原文。
 * @returns 关闭函数
 */
export function startRedactProxy(opts: ProxyOptions): () => Promise<void> {
  const { store, upstreamOrigin } = opts;

  const server = http.createServer(async (req, res) => {
    const chunks: Buffer[] = [];
    for await (const c of req) chunks.push(c as Buffer);
    const raw = Buffer.concat(chunks);
    const isChat = req.method === "POST" && /\/chat\/completions/.test(req.url ?? "");

    const masker = createMasker(store.readJson<FamilyFacts>("family-facts.json", {}));
    const counts: Record<string, number> = {};
    let outBody: Buffer | undefined = raw.length ? raw : undefined;
    let model = "";
    let stream = false;

    if (isChat && raw.length) {
      try {
        const json = JSON.parse(raw.toString("utf8"));
        model = String(json.model ?? "");
        stream = json.stream === true;
        outBody = Buffer.from(JSON.stringify(maskDeep(json, masker, counts)), "utf8");
      } catch {
        // 解析失败时宁可拒绝也不把原文放出去
        res.writeHead(400, { "content-type": "application/json" });
        res.end(JSON.stringify({ error: { message: "guanjia-guard: request body is not JSON; refused to forward" } }));
        store.audit({ kind: "cloud", outcome: "refused-unparseable", path: req.url });
        return;
      }
    }

    // 转发头：去掉逐跳头与压缩协商，保证回包是明文可改写
    const headers: Record<string, string> = {};
    for (const [k, v] of Object.entries(req.headers)) {
      if (["host", "content-length", "connection", "accept-encoding", "transfer-encoding"].includes(k)) continue;
      if (typeof v === "string") headers[k] = v;
    }
    headers["accept-encoding"] = "identity";

    const ac = new AbortController();
    res.on("close", () => { if (!res.writableEnded) ac.abort(); });

    let upstream: Response;
    try {
      upstream = await fetch(upstreamOrigin + req.url, { method: req.method, headers, body: outBody, signal: ac.signal });
    } catch (err: any) {
      res.writeHead(502, { "content-type": "application/json" });
      res.end(JSON.stringify({ error: { message: `guanjia-guard upstream error: ${err?.message ?? err}` } }));
      if (isChat) store.audit({ kind: "cloud", outcome: "upstream-error", model, error: String(err?.message ?? err) });
      return;
    }

    if (isChat) {
      const total = Object.values(counts).reduce((a, b) => a + b, 0);
      store.audit({ kind: "cloud", outcome: "sent", model, stream, status: upstream.status, bytes: outBody?.length ?? 0, masked: counts, maskedTotal: total, factsLoaded: masker.size });
    }

    const ctype = upstream.headers.get("content-type") ?? "";
    res.writeHead(upstream.status, { "content-type": ctype || "application/json", "cache-control": "no-cache" });
    if (!upstream.body) return void res.end();

    // SSE 流：按事件切分，逐个回填
    if (isChat && ctype.includes("text/event-stream")) {
      const su = createStreamUnmasker(masker);
      const decoder = new TextDecoder();
      let pending = "";
      let last: any = null;
      const emit = (data: string) => res.write(`data: ${data}\n\n`);
      const handleEvent = (evt: string) => {
        const dataLines = evt.split(/\r?\n/).filter((l) => l.startsWith("data:"));
        if (!dataLines.length) return void res.write(evt + "\n\n");
        const data = dataLines.map((l) => l.slice(5).trimStart()).join("\n");
        if (data === "[DONE]") {
          // 流尾还有残留（没带 finish_reason 的异常收尾）就补发一个 chunk
          const rest = su.flush("");
          if (rest.length && last) {
            const extra = { ...last, choices: [{ index: 0, delta: {} as any, finish_reason: null }] };
            for (const [k, v] of rest) extra.choices[0].delta[k.includes(":tool:") ? "content" : k.split(":")[1]] = v;
            emit(JSON.stringify(extra));
          }
          return emit("[DONE]");
        }
        try {
          const json = JSON.parse(data);
          unmaskChunk(json, su);
          last = { id: json.id, object: json.object, created: json.created, model: json.model };
          emit(JSON.stringify(json));
        } catch {
          emit(data);
        }
      };
      for await (const part of upstream.body as any) {
        pending += decoder.decode(part, { stream: true });
        let sep: number;
        while ((sep = pending.search(/\r?\n\r?\n/)) >= 0) {
          const evt = pending.slice(0, sep);
          pending = pending.slice(sep).replace(/^\r?\n\r?\n/, "");
          if (evt.trim()) handleEvent(evt);
        }
      }
      if (pending.trim()) handleEvent(pending);
      return void res.end();
    }

    // 非流式：整包回填
    const text = await upstream.text();
    if (isChat && ctype.includes("json")) {
      try {
        const json = JSON.parse(text);
        unmaskMessageBody(json, masker);
        return void res.end(JSON.stringify(json));
      } catch {
        // 不是合法 JSON 就原样返回（错误信息等）
      }
    }
    res.end(isChat ? masker.unmask(text) : text);
  });

  server.on("error", (err) => opts.logger.warn(`guanjia-guard redact proxy error: ${err.message}`));
  server.listen(opts.port, "127.0.0.1", () => opts.logger.info(`guanjia-guard redact proxy on 127.0.0.1:${opts.port} -> ${upstreamOrigin}`));
  return () => new Promise<void>((resolve) => server.close(() => resolve()));
}
