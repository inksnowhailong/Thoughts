// 夜间习惯抽取（6b 窄版）：由 OpenClaw 的 command 型 cron 每天 03:00 运行。
//
// 为什么不用模型抽：D18 规定"时间线原文永不出门"，而这里的模型在云上，
// 所以用本地统计规则：同一星期几、同一小时、同一件事，在最近 6 周里出现在 >=3 个不同的周
// → 视为周期性事件候选，写进 workspace/guanjia/habit-drafts.json（只草案，不生效）。
//
// 时间线由 S2 写（workspace/memory/timeline.*），这里只读，兼容两种形态：
//   JSONL：每行一个对象，时间取 ts/time/at/timestamp，文本取 text/content/event/summary
//   Markdown：形如 "- 2026-09-30 15:00 交房租" 的行
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const workspace = process.env.GUANJIA_WORKSPACE ?? path.join(os.homedir(), ".openclaw", "workspace");
const gdir = path.join(workspace, "guanjia");
const memDir = path.join(workspace, "memory");
const WINDOW_DAYS = 42;
const MIN_WEEKS = 3;

/** 追加审计（只记统计，不记原文） */
const audit = (entry) =>
  fs.appendFileSync(path.join(gdir, "audit.jsonl"), JSON.stringify({ ts: new Date().toISOString(), ...entry }) + "\n", "utf8");

/** 读取所有 timeline.* 文件并解析成 {at: Date, text} */
function loadTimeline() {
  if (!fs.existsSync(memDir)) return [];
  const files = fs.readdirSync(memDir).filter((f) => /^timeline/i.test(f));
  const events = [];
  for (const f of files) {
    const raw = fs.readFileSync(path.join(memDir, f), "utf8").replace(/^﻿/, "");
    for (const line of raw.split(/\r?\n/)) {
      const t = line.trim();
      if (!t) continue;
      if (t.startsWith("{")) {
        try {
          const o = JSON.parse(t);
          const at = new Date(o.ts ?? o.time ?? o.at ?? o.timestamp ?? NaN);
          const text = o.text ?? o.content ?? o.event ?? o.summary;
          if (!isNaN(at) && typeof text === "string") events.push({ at, text });
        } catch {}
        continue;
      }
      // S2 格式：- <记录时刻> | 来源:语音 | 类型:提醒 | 原话:"…" | 时间:<提醒时刻> | 任务:<id>
      if (/^-\s*\S+\s*\|/.test(t)) {
        const [head, ...rest] = t.replace(/^-\s*/, "").split(/\s*\|\s*/);
        const kv = Object.fromEntries(rest.map((p) => [p.split(":")[0], p.slice(p.indexOf(":") + 1)]));
        if (kv["类型"] === "取消") continue;
        // 提醒类以"提醒时刻"为事件发生时间，记事类以记录时刻为准
        const at = new Date(kv["时间"] || head);
        const text = (kv["原话"] ?? "").replace(/^"|"$/g, "");
        if (!isNaN(at) && text) events.push({ at, text });
        continue;
      }
      const m = t.match(/^[-*]?\s*(\d{4}-\d{2}-\d{2})[ T](\d{1,2}:\d{2})\s+(.+)$/);
      if (m) events.push({ at: new Date(`${m[1]}T${m[2].padStart(5, "0")}:00`), text: m[3] });
    }
  }
  return events;
}

/** 去掉口头指令词与时间词，留下"事情本身"（"记一下明天下午三点交房租" → "交房租"） */
const clean = (s) =>
  s
    .replace(/记一下|帮我记(一下)?|记得|提醒我|提醒一下|别忘了|到时候|叫我/g, "")
    .replace(/今天|明天|后天|今晚|明晚|每天|每周|下周|这周|本周|(周|星期|礼拜)[一二三四五六日天]/g, "")
    .replace(/凌晨|早上|上午|中午|下午|傍晚|晚上/g, "")
    .replace(/[零一二三四五六七八九十两\d]+(点|:|：)(半|[零一二三四五六七八九十\d]+分?|钟)?/g, "")
    .replace(/[\s，,。.!！?？、"'“”()（）\[\]【】-]/g, "");
/** 分组键：清洗后的前 12 个字 */
const norm = (s) => clean(s).slice(0, 12);

/** ISO 周编号（年*100+周），用于数"出现在几个不同的周" */
function weekId(d) {
  const t = new Date(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()));
  const day = t.getUTCDay() || 7;
  t.setUTCDate(t.getUTCDate() + 4 - day);
  const y = t.getUTCFullYear();
  return y * 100 + Math.ceil(((t - Date.UTC(y, 0, 1)) / 86400000 + 1) / 7);
}

fs.mkdirSync(gdir, { recursive: true });
const draftsPath = path.join(gdir, "habit-drafts.json");
let file = { drafts: [], rejected: [] };
try { file = JSON.parse(fs.readFileSync(draftsPath, "utf8").replace(/^﻿/, "")); } catch {}
file.drafts ??= [];
file.rejected ??= [];

const since = Date.now() - WINDOW_DAYS * 86400000;
const events = loadTimeline().filter((e) => e.at.getTime() >= since && norm(e.text));

// 分组：星期几 + 小时 + 归一化文本
const groups = new Map();
for (const e of events) {
  const key = `${e.at.getDay()}-${e.at.getHours()}-${norm(e.text)}`;
  const g = groups.get(key) ?? { weeks: new Set(), minutes: [], text: e.text, weekday: e.at.getDay(), hour: e.at.getHours(), key };
  g.weeks.add(weekId(e.at));
  g.minutes.push(e.at.getMinutes());
  g.text = e.text; // 保留最近一次的原话
  groups.set(key, g);
}

let added = 0;
const known = new Set([...file.drafts.map((d) => d.key), ...file.rejected]);
for (const g of groups.values()) {
  if (g.weeks.size < MIN_WEEKS || known.has(g.key)) continue;
  // 提醒时刻取该小时内最早出现的分钟，向下取整到 5 分钟
  const minute = Math.floor(Math.min(...g.minutes) / 5) * 5;
  file.drafts.push({
    key: g.key,
    weekday: g.weekday,
    time: `${String(g.hour).padStart(2, "0")}:${String(minute).padStart(2, "0")}`,
    text: (clean(g.text) || g.text).slice(0, 30),
    weeks: g.weeks.size,
    status: "draft",
  });
  added++;
}

file.updatedAt = new Date().toISOString();
fs.writeFileSync(draftsPath + ".tmp", JSON.stringify(file, null, 2), "utf8");
fs.renameSync(draftsPath + ".tmp", draftsPath);
audit({ kind: "habit-extract", scanned: events.length, groups: groups.size, added, totalDrafts: file.drafts.length, cloud: false });
console.log(`habit-extract: scanned=${events.length} groups=${groups.size} added=${added}`);
