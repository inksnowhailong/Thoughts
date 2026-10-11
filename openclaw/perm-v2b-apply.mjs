// 权限 v2 补丁 b 的上线 / 回滚脚本（在家里电脑上运行，改的是线上网关配置和工作区，先读一遍再跑；不带参数什么都不做）。
//
// 做什么（对应 2026-10-11 事故的修复）：
//   1. 移除 approvals.plugin：它把"插件审批"推到主人微信（openclaw-weixin）。v2 守卫从不发起插件审批，
//      所有"需要确认"都是 工具返回 → 模型口头问 → 主人说允许；这条路由现在只会在将来某个插件调 requireApproval 时才有用，
//      而主人要求不走微信。移除后那类请求没有聊天渠道可投递，OpenClaw 会按"没有审批路由"直接拦下（fail-closed），
//      Control UI 开着时仍可在网页里批。它与 approvals.exec（exec 主机策略，现为 full/off）互相独立，不受影响。
//   2. --directory-mode（可选）：tools.toolSearch.mode = "directory"，目录里的工具可以按同名直接调用（含 web_fetch），不必先 tool_search。
//   3. --agents（可选）：把仓库里的规则段（AGENTS-S2-section.md）和两份技能（launch / lookup）同步到线上工作区。
//      这三份文字改成了"专用工具直接调用"，必须和插件一起上线（先备份，可回滚）。
//
// 用法（PowerShell，先加载 fnm 环境）：
//   fnm env --use-on-cd --shell power-shell | Out-String | Invoke-Expression; fnm use 24 | Out-Null
//   node F:\coding\guanjia\openclaw\perm-v2b-apply.mjs --check                # 只看会改什么，不写任何文件
//   node F:\coding\guanjia\openclaw\perm-v2b-apply.mjs --apply --agents       # 网关停着时执行
//   node F:\coding\guanjia\openclaw\perm-v2b-apply.mjs --apply --agents --directory-mode
//   node F:\coding\guanjia\openclaw\perm-v2b-apply.mjs --rollback             # 恢复备份（同样要网关停着）
//   --config <文件>   只用于 --check，指向别的 openclaw.json 副本（测试用）
//   --force           网关端口还在监听时也硬改（不建议）
//
// 配置用 openclaw 官方 CLI 改（config unset / set，带 schema 校验），不手改 JSON；用 node 调 openclaw.mjs 是因为 PS 5.1 会吞 JSON 里的双引号。
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const OPENCLAW = path.join(path.dirname(process.execPath), "node_modules", "openclaw", "openclaw.mjs");
const HOME_OC = path.join(os.homedir(), ".openclaw");
const WORKSPACE = path.join(HOME_OC, "workspace");
const STATE_FILE = path.join(HOME_OC, "perm-v2b-last.json");
const GATEWAY_PORT = 18789;
const BEGIN = "<!-- guanjia-s2:begin -->";
const END = "<!-- guanjia-s2:end -->";
/** 要同步的工作区文件：[仓库内路径, 线上路径]；规则段是 AGENTS.md 里两个标记之间的一段，其余是整文件 */
const SKILLS = ["guanjia-launch", "guanjia-lookup"].map((n) => [`workspace/skills/${n}/SKILL.md`, `skills/${n}/SKILL.md`]);

const args = process.argv.slice(2);
const has = (f) => args.includes(f);
const argValue = (f) => (args.includes(f) ? args[args.indexOf(f) + 1] : undefined);
const mode = ["--check", "--apply", "--rollback"].find(has);
if (!mode) {
  console.error("用法：node perm-v2b-apply.mjs --check | --apply [--agents] [--directory-mode] | --rollback（详见文件头注释）");
  process.exit(2);
}

if (has("--config") && mode !== "--check") {
  console.error("--config 只能配合 --check 用：--apply 通过 openclaw CLI 改的永远是线上默认配置。");
  process.exit(2);
}
const configPath = argValue("--config") ?? path.join(HOME_OC, "openclaw.json");
const portOpen = (port) =>
  new Promise((resolve) => {
    const s = net.connect(port, "127.0.0.1", () => { s.end(); resolve(true); });
    s.on("error", () => resolve(false));
  });

/** 调 openclaw CLI，打印输出；失败直接退出 */
function oc(cliArgs) {
  console.log(`> openclaw ${cliArgs.join(" ")}`);
  const r = spawnSync(process.execPath, [OPENCLAW, ...cliArgs], { encoding: "utf8" });
  console.log(((r.stdout ?? "") + (r.stderr ?? "")).trim().split(/\r?\n/).slice(-6).join("\n"));
  if (r.status !== 0) {
    console.error(`失败（exit ${r.status}），已停止；已做的备份见 ${STATE_FILE}`);
    process.exit(1);
  }
}

/** 规则段：取 / 换 AGENTS.md 里两个标记之间（含标记）的一段 */
function blockOf(text) {
  const a = text.indexOf(BEGIN), b = text.indexOf(END);
  if (a < 0 || b < a || text.indexOf(BEGIN, a + 1) >= 0) throw new Error("AGENTS.md 里管家规则的开始/结束标记不完整或重复，先人工修好");
  return [a, b + END.length];
}
const repoBlock = () => {
  const t = fs.readFileSync(path.join(here, "AGENTS-S2-section.md"), "utf8");
  const [a, b] = blockOf(t);
  return t.slice(a, b);
};

function readConfig() {
  const cfg = JSON.parse(fs.readFileSync(configPath, "utf8"));
  return cfg;
}

if (mode === "--check") {
  const cfg = readConfig();
  const plugin = cfg.approvals?.plugin;
  if (plugin) {
    const targets = (plugin.targets ?? []).map((t) => `${t.channel}→${t.to}${t.accountId ? ` (${t.accountId})` : ""}`).join("；");
    console.log(`[配置] 将移除 approvals.plugin（enabled=${plugin.enabled}，mode=${plugin.mode}，目标：${targets || "无"}）`);
  } else {
    console.log("[配置] 已经没有 approvals.plugin，无需改动");
  }
  console.log(`[配置] 当前 tools.toolSearch = ${JSON.stringify(cfg.tools?.toolSearch ?? "（未设，默认 structured tools 模式）")}${has("--directory-mode") ? " → 将设为 {\"mode\":\"directory\"}" : "（不改）"}`);
  if (has("--agents")) {
    const agentsPath = path.join(WORKSPACE, "AGENTS.md");
    if (fs.existsSync(agentsPath)) {
      const live = fs.readFileSync(agentsPath, "utf8");
      const [a, b] = blockOf(live);
      console.log(`[工作区] AGENTS.md 规则段：线上 ${b - a} 字符，仓库 ${repoBlock().length} 字符，${live.slice(a, b) === repoBlock() ? "完全一致" : "不一致（会被仓库版覆盖，先备份）"}`);
    } else {
      console.log(`[工作区] 找不到 ${agentsPath}（这台机器不是线上，或路径不同）`);
    }
    for (const [repo, live] of SKILLS) {
      const lp = path.join(WORKSPACE, live);
      const same = fs.existsSync(lp) && fs.readFileSync(lp, "utf8") === fs.readFileSync(path.join(here, repo), "utf8");
      console.log(`[工作区] ${live}：${fs.existsSync(lp) ? (same ? "与仓库一致" : "与仓库不同（会被仓库版覆盖，先备份）") : "线上不存在"}`);
    }
  }
  console.log(`网关端口 ${GATEWAY_PORT} 监听中：${await portOpen(GATEWAY_PORT)}（--apply 要求先停网关）`);
} else if (mode === "--apply") {
  if ((await portOpen(GATEWAY_PORT)) && !has("--force")) {
    console.error(`网关还在监听 ${GATEWAY_PORT}。先 openclaw gateway stop --force，确认进程退出后再运行（或加 --force 硬改）。`);
    process.exit(1);
  }
  const ts = Date.now();
  const saved = [];
  /** 备份到同目录 <名>.bak-permv2b-<ts>，记进状态文件供回滚 */
  const backup = (file) => {
    const bak = `${file}.bak-permv2b-${ts}`;
    fs.copyFileSync(file, bak);
    saved.push({ file, bak });
    console.log(`备份 ${file} → ${bak}`);
  };
  backup(configPath);
  if (has("--agents")) {
    const agentsPath = path.join(WORKSPACE, "AGENTS.md");
    backup(agentsPath);
    for (const [, live] of SKILLS) backup(path.join(WORKSPACE, live));
  }
  fs.writeFileSync(STATE_FILE, JSON.stringify({ ts, saved }, null, 2));

  if (readConfig().approvals?.plugin) oc(["config", "unset", "approvals.plugin"]);
  else console.log("approvals.plugin 已不存在，跳过");
  if (has("--directory-mode")) oc(["config", "set", "tools.toolSearch", JSON.stringify({ mode: "directory" }), "--strict-json"]);

  if (has("--agents")) {
    const agentsPath = path.join(WORKSPACE, "AGENTS.md");
    const live = fs.readFileSync(agentsPath, "utf8");
    const [a, b] = blockOf(live);
    fs.writeFileSync(agentsPath, live.slice(0, a) + repoBlock() + live.slice(b));
    for (const [repo, dest] of SKILLS) fs.copyFileSync(path.join(here, repo), path.join(WORKSPACE, dest));
    console.log("已同步 AGENTS.md 规则段和两份技能");
  }
  console.log(`\n完成。回滚：node perm-v2b-apply.mjs --rollback（备份清单 ${STATE_FILE}）。下一步：启动网关，确认进程启动时间晚于配置修改时间。`);
} else {
  if (await portOpen(GATEWAY_PORT)) {
    console.error(`网关还在监听 ${GATEWAY_PORT}。先停网关再回滚。`);
    process.exit(1);
  }
  if (!fs.existsSync(STATE_FILE)) {
    console.error(`找不到 ${STATE_FILE}：没有可回滚的备份（这个脚本没跑过 --apply）。`);
    process.exit(1);
  }
  const { saved } = JSON.parse(fs.readFileSync(STATE_FILE, "utf8"));
  for (const { file, bak } of saved) {
    fs.copyFileSync(bak, file);
    console.log(`恢复 ${file} ← ${bak}`);
  }
  fs.renameSync(STATE_FILE, `${STATE_FILE}.rolled-back`);
  console.log("已回滚。启动网关后配置与规则回到补丁前；插件代码另用 git 回退（见回执里的回滚步骤）。");
}
