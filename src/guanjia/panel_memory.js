// 记忆页脚本：由 panel.py 的 html() 内联进 panel.html（用到主脚本里的 $ / el / api / getJson / budgetBar）。
// 全部用 textContent 渲染（不把文件内容当 HTML 解析）。
const M = { items: [], daily: [], rel: null, detail: null, sha: "", orig: "", histVersion: null };

function memSay(text, cls) { const m = $("memMsg"); m.className = "hint " + (cls || ""); m.textContent = text || ""; }
function memDirty() { return !$("memEditor").hidden && $("memText").value !== M.orig; }
function memLeaveOk() { return !memDirty() || confirm("编辑框里有没保存的修改，放弃吗？"); }
function memWhen(t) { return t ? new Date(t * 1000).toLocaleString("zh-CN", { hour12: false }) : "—"; }

async function memLoad() {
  const r = await getJson("/api/memory");
  const box = $("memList");
  box.textContent = "";
  if (!r.ok) { box.append(el("div", { class: "spk-bad", text: "读不出来：" + (r.data.reason || "未知错误") })); return; }
  M.items = r.data.items; M.daily = r.data.daily;
  const tb = el("tbody");
  for (const it of M.items) {
    const size = el("td", null, it.exists ? `${it.chars} 字` : "");
    if (it.budget && it.exists) size.append(" ", budgetBar(it.chars, it.budget), ` / ${it.budget}`);
    const tr = el("tr", { class: "prow" + (it.rel === M.rel ? " sel" : ""), "data-alt": "memory-" + it.rel },
      el("td", { text: it.title }), size,
      el("td", null, el("span", { class: "badge" + (it.exists ? "" : " diff"), text: it.exists ? "有" : "还没有" }),
        el("span", { class: "badge " + (it.effect === "下一轮" || it.effect === "即时" ? "" : "warn"), text: it.effect }),
        it.kind === "timeline" ? el("span", { class: "badge warn", text: "只追加" }) : null,
        it.kind === "facts" ? el("span", { class: "badge ro", text: "只许末尾追加" }) : null),
      el("td", { class: "hint", text: it.exists ? memWhen(it.mtime) : "" }));
    tr.addEventListener("click", () => memOpen(it.rel));
    tb.append(tr);
  }
  box.append(el("table", null, el("thead", null, el("tr", null, ...["文件", "字数", "状态 / 生效", "修改时间"].map((t) => el("th", { text: t })))), tb));
  const sel = $("memDaily");
  sel.textContent = "";
  if (!M.daily.length) sel.append(el("option", { value: "", text: "（还没有每日笔记）" }));
  for (const d of M.daily) sel.append(el("option", { value: d.rel, text: `${d.rel.slice(7, -3)}（${d.chars} 字）` }));
}

function memFill(d) {
  M.rel = d.rel; M.detail = d; M.sha = d.sha; M.orig = d.text; M.histVersion = null;
  $("memTitle").textContent = d.title;
  const b = $("memBadges");
  b.textContent = "";
  b.append(el("span", { class: "badge " + (d.effect === "下一轮" || d.effect === "即时" ? "" : "warn"), text: "生效：" + d.effect }));
  if (!d.exists) b.append(el("span", { class: "badge diff", text: "还没有这个文件，保存即新建" }));
  $("memNote").textContent = `文件：${d.rel}　` + (d.note || "");
  // 时间线：编辑时一直挂黄条；改了旧行保存后再换成具体警告
  const w = $("memWarn");
  w.hidden = d.kind !== "timeline" && d.kind !== "facts";
  w.textContent = d.kind === "timeline" ? "⚠ 技能约定时间线只追加：改旧行会让「我之前说过啥」对不上。允许改，但请只在确实写错时改。"
    : d.kind === "facts" ? "只许在每个数组末尾追加；删项、改顺序、删键、改「_说明」都会被拒绝（人名/设备编号与历史对话里的占位符一一对应）。" : "";
  $("memText").value = d.text;
  $("memConflict").hidden = true;
  $("memHistory").hidden = true;
  memCards(d);
  memSide();
  $("memEditor").hidden = false;
}

function memCards(d) {
  const box = $("memCards");
  box.textContent = "";
  if (d.kind !== "user") return;
  box.append(el("h3", { text: "已记下的条目（按 observed / status 注释解析）" }));
  if (!d.cards.length) { box.append(el("div", { class: "hint", text: "没有解析到条目（格式：<!-- observed: 日期 | status: active --> 后跟一行「- 指令」）" })); return; }
  for (const c of d.cards) {
    box.append(el("div", { class: "ed-box", "data-alt": "user-card" },
      el("span", { class: "badge " + (c.status === "active" ? "" : "ro"), text: c.status === "active" ? "生效中" : c.status === "superseded" ? "已被取代" : c.status }),
      c.placeholder ? el("span", { class: "badge diff", text: "模板示例" }) : null,
      el("span", { class: "hint", text: " " + c.date + "　" }), c.text));
  }
}

function memSide() {
  const d = M.detail, side = $("memSide");
  side.textContent = "";
  if (!d) return;
  const n = $("memText").value.length, lim = d.budget || 0;
  const over = (lim && n > lim) || n > 20000;
  side.append(el("div", { class: over ? "spk-bad" : "hint", text: `${n} 字（粗估 ${Math.round(n * 0.7)} token）` + (lim ? `，预算 ${lim} 字` : "") + "，上限 20000 字" }));
  if (lim) side.append(budgetBar(n, lim));
  if (n > 20000) side.append(el("div", { class: "spk-bad", text: "超过上限，保存会被拒绝" }));
  if (d.kind === "facts") {
    try { JSON.parse($("memText").value); } catch (e) { side.append(el("div", { class: "spk-bad", text: "现在不是合法 JSON：" + e.message })); }
  }
}
$("memText").addEventListener("input", memSide);

async function memOpen(rel) {
  if (!memLeaveOk()) return;
  const r = await getJson("/api/memory/file", { rel });
  if (!r.ok) { memSay("打不开：" + (r.data.reason || "未知错误"), "spk-bad"); return; }
  memSay("");
  memFill(r.data);
  memLoad();
  $("memEditor").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

async function memSave(body, okText) {
  const r = await api("/api/memory/file", { rel: M.rel, base_sha: M.sha, ...body });
  if (r.ok) {
    memFill(r.data);
    if (r.data.warn) { $("memWarn").hidden = false; $("memWarn").textContent = "⚠ " + r.data.warn; }
    memSay(okText || `已保存，${r.data.effect}生效`, "spk-ok");
    memLoad();
    return r;
  }
  if (r.status === 409) {
    $("memConflictText").textContent = (r.data.reason || "文件刚被改过") + "。你的修改还保留在编辑框里。";
    $("memConflict").hidden = false;
    memSay("没保存：冲突", "spk-bad");
  } else memSay("没保存：" + (r.data.reason || "未知错误"), "spk-bad");  // 400（家庭事实不合规则等）、423 锁被占都带中文原因
  return r;
}
$("btnMemSave").addEventListener("click", () => memSave({ text: $("memText").value }));
$("btnMemReload").addEventListener("click", async () => {
  const r = await getJson("/api/memory/file", { rel: M.rel });
  if (r.ok) { memFill(r.data); memSay("已重新载入"); } else memSay("打不开：" + (r.data.reason || "未知错误"), "spk-bad");
});
$("btnMemForce").addEventListener("click", async () => {
  if (!confirm("用你的版本覆盖文件里现在的内容？对方（多半是模型）刚写的会存进历史版本。")) return;
  const mine = $("memText").value;
  const r = await getJson("/api/memory/file", { rel: M.rel });
  if (!r.ok) { memSay("没成功：" + (r.data.reason || "未知错误"), "spk-bad"); return; }
  M.sha = r.data.sha;  // 拿新的 sha 再存一次，文本还是你的
  const res = await memSave({ text: mine }, "已用你的版本覆盖");
  if (!res.ok) $("memText").value = mine;
});
$("btnMemClose").addEventListener("click", () => {
  if (!memLeaveOk()) return;
  $("memEditor").hidden = true; M.rel = null; M.detail = null;
  memLoad();
});
$("btnMemHist").addEventListener("click", () => {
  const box = $("memHistory"), list = $("memHistList");
  box.hidden = !box.hidden;
  list.textContent = "";
  $("memHistText").textContent = "";
  $("btnMemRestore").disabled = true;
  M.histVersion = null;
  const h = (M.detail && M.detail.history) || [];
  if (!h.length) list.append(el("div", { class: "hint", text: "还没有历史版本（第一次保存时才会备份旧版）" }));
  for (const v of h) {
    const row = el("div", { text: `${fmtVersion(v.id)}（${v.bytes} 字节）` });
    row.addEventListener("click", async () => {
      const r = await getJson("/api/memory/file", { rel: M.rel, version: v.id });
      if (!r.ok) { memSay("读不出这一版：" + (r.data.reason || "未知错误"), "spk-bad"); return; }
      M.histVersion = v.id;
      $("memHistText").textContent = r.data.version_text;
      $("btnMemRestore").disabled = false;
    });
    list.append(row);
  }
});
$("btnMemRestore").addEventListener("click", () => {
  if (!M.histVersion || !confirm("恢复成这一版？当前内容会存进历史版本。")) return;
  memSave({ action: "restore", version: M.histVersion }, "已恢复这一版");
});
$("btnMemDailyOpen").addEventListener("click", () => { const v = $("memDaily").value; if (v) memOpen(v); });
$("btnMemDailyNew").addEventListener("click", () => {
  const d = new Date(), p = (n) => String(n).padStart(2, "0");
  memOpen(`memory/${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}.md`);
});

memLoad();
