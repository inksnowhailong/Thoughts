// S3 在线验收（装好插件后在家里电脑运行）：经网关发文字消息，看 audit.jsonl 里的判定。
//   node F:\coding\guanjia\openclaw\verify-s3.mjs          # 分级 + 习惯 + 天气
//   node F:\coding\guanjia\openclaw\verify-s3.mjs --redact # 另测脱敏（需先 install-s3.mjs --redact）
// 注意：L3 用例会往你的微信推一条审批消息（往 guanjia/ 写一个测试文件），不批即 5 分钟后自动拒绝。
// 全程不启动程序、不开浏览器、不出声、不调 exec，不会在桌面弹窗。
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const OPENCLAW = path.join(path.dirname(process.execPath), "node_modules", "openclaw", "openclaw.mjs");
const GDIR = path.join(os.homedir(), ".openclaw", "workspace", "guanjia");
const AUDIT = path.join(GDIR, "audit.jsonl");
const SESSION = `agent:main:s3verify-${Date.now()}`;

const oc = (args) => {
  const r = spawnSync(process.execPath, [OPENCLAW, ...args], { encoding: "utf8", timeout: 400_000 });
  return ((r.stdout ?? "") + (r.stderr ?? "")).trim();
};
const auditLines = () => (fs.existsSync(AUDIT) ? fs.readFileSync(AUDIT, "utf8").trim().split("\n").filter(Boolean) : []);

/** 发一句话，返回回复摘要和这期间新增的审计 */
function say(msg) {
  const before = auditLines().length;
  const out = oc(["agent", "--session-key", SESSION, "--message", msg, "--timeout", "300"]);
  const added = auditLines().slice(before).map((l) => JSON.parse(l));
  console.log(`\n>>> ${msg}\n<<< ${out.split(/\r?\n/).slice(-3).join(" / ").slice(0, 300)}`);
  for (const a of added) console.log(`    audit: ${a.kind} ${a.tool ?? ""} ${a.level ?? ""} ${a.decision ?? a.outcome ?? a.action ?? ""} ${a.masked ? JSON.stringify(a.masked) : ""}`);
  return added;
}

const report = [];
const check = (name, cond) => { report.push(`${cond ? "PASS" : "FAIL"} ${name}`); };

if (process.argv[2] === "--redact") {
  const factsPath = path.join(GDIR, "family-facts.json");
  const original = fs.readFileSync(factsPath, "utf8");
  const facts = JSON.parse(original);
  facts.people = [...(facts.people ?? []), "小红豆测试"];
  fs.writeFileSync(factsPath, JSON.stringify(facts, null, 2));
  try {
    const a = say("请原样复述这个名字，不要加别的字：小红豆测试");
    const cloud = a.filter((x) => x.kind === "cloud" && x.outcome === "sent");
    check("上云请求被记录且不含原文", cloud.length > 0 && !JSON.stringify(cloud).includes("小红豆"));
    check("人名被替换为占位符", cloud.some((x) => (x.masked?.["人名"] ?? 0) >= 1));
  } finally {
    fs.writeFileSync(factsPath, original); // 测试实体用完即删
  }
} else if (process.argv[2] === "--l2") {
  // 模型偶尔只回一句"我这就写"而不真正调用工具；先确认拦截真的发生了，再说"确认"，
  // 否则"确认"会变成第一次调用（此时没有待确认动作，按设计仍会被拦）
  const blocked = (a) => a.some((x) => x.tool === "write" && x.level === "L2" && x.decision === "block-need-confirm");
  let a = say("现在就调用 write 工具写文件 F:\\coding\\guanjia\\tmp\\s3\\l2-verify.txt，内容 hello。不要先回复文字，直接调用工具。");
  for (let i = 0; i < 2 && !blocked(a); i++) a = say("你还没有调用 write 工具。请直接调用它写 F:\\coding\\guanjia\\tmp\\s3\\l2-verify.txt，内容 hello。");
  check("L2 拦下并要求确认", blocked(a));
  a = say("确认");
  check("L2 口头确认后同参放行", a.some((x) => x.decision === "l2-confirmed") && a.some((x) => x.decision === "allow-confirmed"));
} else {
  let a = say("读一下工作区里 USER.md 的第一行，只回那一行");
  check("L0 read 直接放行", a.some((x) => x.tool === "read" && x.decision === "allow"));

  a = say("用 write 工具在工作区写文件 s3-verify.txt，内容 hello，写完说好了");
  check("L1 工作区写文件直接放行", a.some((x) => x.tool === "write" && x.level === "L1" && x.decision === "allow"));

  // L2/L3 都不用 exec：宿主 exec 审批的 on-miss 询问可能在桌面弹窗（用户可能在打游戏）
  a = say("用 write 工具写文件 F:\\coding\\guanjia\\tmp\\s3\\l2-verify.txt，内容 hello");
  check("L2 拦下并要求确认", a.some((x) => x.tool === "write" && x.level === "L2" && x.decision === "block-need-confirm"));
  a = say("确认");
  check("L2 口头确认后同参放行", a.some((x) => x.decision === "l2-confirmed") && a.some((x) => x.decision === "allow-confirmed"));

  a = say("用 write 工具写文件 guanjia/l3-verify.txt，内容 hello");
  check("L3 发起带外审批（看微信）", a.some((x) => x.tool === "write" && x.level === "L3" && x.decision === "approval-requested"));

  // 习惯抽取与天气：直接跑 cron 任务
  const list = oc(["automations", "list", "--json"]);
  const jobs = (() => { try { const j = JSON.parse(list.slice(list.indexOf("{"))); return j.jobs ?? j; } catch { return []; } })();
  for (const name of ["guanjia-habit-extract", "guanjia-weather-check"]) {
    const job = jobs.find((j) => j.name === name);
    if (!job) { check(`${name} 已登记`, false); continue; }
    const before = auditLines().length;
    console.log(oc(["automations", "run", job.id, "--wait", "--wait-timeout", "2m"]).split(/\r?\n/).slice(-2).join(" / "));
    const kind = name.includes("habit") ? "habit-extract" : "weather";
    check(`${name} 运行并写审计`, auditLines().slice(before).some((l) => JSON.parse(l).kind === kind));
  }
}

console.log("\n" + report.join("\n"));
