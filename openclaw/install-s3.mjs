// S3 安装 / 回滚脚本（在家里电脑上运行，改的是在线网关配置，请先读一遍再跑）。
//
// 用法（PowerShell，先加载 fnm 环境）：
//   fnm env --use-on-cd --shell power-shell | Out-String | Invoke-Expression; fnm use 24 | Out-Null
//   node F:\coding\guanjia\openclaw\install-s3.mjs            # 第 1 步：装插件 + L3 审批路由 + 两个 cron
//   node F:\coding\guanjia\openclaw\install-s3.mjs --redact   # 第 2 步：确认代理在监听后，把 siliconflow 指向脱敏代理
//   node F:\coding\guanjia\openclaw\install-s3.mjs --rollback # 全部撤销
//
// 用 node 调 openclaw.mjs 而不是 PowerShell 直接调：PS 5.1 调原生程序会吞掉 JSON 里的双引号。
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";

const OPENCLAW = path.join(path.dirname(process.execPath), "node_modules", "openclaw", "openclaw.mjs");
const PLUGIN_DIR = "F:\\coding\\guanjia\\openclaw\\plugins\\guanjia-guard";
const PROXY_PORT = 18797;
const UPSTREAM_BASE = "https://api.siliconflow.cn/v1";
const WECHAT_TARGET = { channel: "openclaw-weixin", to: "o9cq804p69hz5bwbyqkbcsvuq67u@im.wechat", accountId: "18f8cd8761c6-im-bot" };
const JOBS = [
  { name: "guanjia-habit-extract", expr: "0 3 * * *", script: "habit-extract.mjs" },
  { name: "guanjia-weather-check", expr: "30 6 * * *", script: "weather-check.mjs" },
];

/** 调 openclaw，打印并返回输出；失败直接退出 */
function oc(args, { allowFail = false } = {}) {
  console.log(`> openclaw ${args.join(" ")}`);
  const r = spawnSync(process.execPath, [OPENCLAW, ...args], { encoding: "utf8" });
  const out = (r.stdout ?? "") + (r.stderr ?? "");
  console.log(out.trim().split(/\r?\n/).slice(-8).join("\n"));
  if (r.status !== 0 && !allowFail) {
    console.error(`失败（exit ${r.status}），已停止。`);
    process.exit(1);
  }
  return out;
}

/** 按名字找已存在的 cron，保证脚本可重复执行 */
function jobIdsByName(name) {
  const out = oc(["automations", "list", "--json"], { allowFail: true });
  try {
    const j = JSON.parse(out.slice(out.indexOf("{")));
    return (j.jobs ?? j).filter((x) => x.name === name).map((x) => x.id);
  } catch {
    return [];
  }
}

const portOpen = (port) =>
  new Promise((resolve) => {
    const s = net.connect(port, "127.0.0.1", () => { s.end(); resolve(true); });
    s.on("error", () => resolve(false));
  });

const mode = process.argv[2] ?? "--install";

if (mode === "--install") {
  const bak = path.join(os.homedir(), ".openclaw", `openclaw.json.bak-s3-${Date.now()}`);
  fs.copyFileSync(path.join(os.homedir(), ".openclaw", "openclaw.json"), bak);
  console.log(`配置已备份到 ${bak}`);

  oc(["plugins", "install", "--link", PLUGIN_DIR, "--force"]);
  oc(["plugins", "enable", "guanjia-guard"]);
  oc(["config", "set", "plugins.entries.guanjia-guard.hooks.allowConversationAccess", "true", "--strict-json"]);
  // L3 带外审批：推到微信（手机），Control UI（桌面）本身就是审批面
  oc(["config", "set", "approvals.plugin", JSON.stringify({ enabled: true, mode: "targets", agentFilter: ["main"], targets: [WECHAT_TARGET] }), "--strict-json"]);

  for (const job of JOBS) {
    if (jobIdsByName(job.name).length) {
      console.log(`cron ${job.name} 已存在，跳过`);
      continue;
    }
    oc(["automations", "create", job.expr, "--name", job.name, "--tz", "Asia/Shanghai",
      "--command-argv", JSON.stringify([process.execPath, path.join(PLUGIN_DIR, "scripts", job.script)]),
      "--timeout-seconds", "120"]);
  }
  oc(["plugins", "inspect", "guanjia-guard", "--runtime", "--json"], { allowFail: true });
  console.log(`\n脱敏代理端口 ${PROXY_PORT} 监听中：${await portOpen(PROXY_PORT)}（false 的话等网关热加载完或 openclaw gateway restart 后再看）`);
  console.log("下一步：确认上面为 true 后运行 --redact 才会真正把模型流量走脱敏代理。");
} else if (mode === "--redact") {
  if (!(await portOpen(PROXY_PORT))) {
    console.error(`127.0.0.1:${PROXY_PORT} 没有在监听，拒绝切换（切了会让所有模型调用失败）。`);
    process.exit(1);
  }
  oc(["config", "set", "models.providers.siliconflow.baseUrl", `http://127.0.0.1:${PROXY_PORT}/v1`]);
  console.log("已切换。回滚：node install-s3.mjs --rollback");
} else if (mode === "--rollback") {
  oc(["config", "set", "models.providers.siliconflow.baseUrl", UPSTREAM_BASE], { allowFail: true });
  for (const job of JOBS) for (const id of jobIdsByName(job.name)) oc(["automations", "remove", id], { allowFail: true });
  oc(["config", "unset", "approvals.plugin"], { allowFail: true });
  oc(["plugins", "disable", "guanjia-guard"], { allowFail: true });
  console.log("已回滚。workspace/guanjia/ 下的数据文件保留未删。");
} else {
  console.error("未知参数：" + mode);
  process.exit(1);
}
