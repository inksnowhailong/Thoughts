// P4-6b：初代画像暂存审阅。所有写操作仅写 staging/decisions.json。
const G1 = { items: [], stage: null };

function gen1Say(text, cls) {
  const box = $("gen1Message");
  box.className = "hint " + (cls || "");
  box.textContent = text || "";
}
function gen1Options(id, values) {
  const select = $(id), value = select.value;
  select.textContent = "";
  select.append(el("option", { value: "", text: "全部" }));
  for (const item of values) select.append(el("option", { value: item, text: item }));
  select.value = values.includes(value) ? value : "";
}
function gen1Filtered() {
  const layer = $("gen1Layer").value, target = $("gen1Target").value, topic = $("gen1Topic").value;
  return (G1.stage ? G1.stage.candidates : []).filter(item =>
    (!layer || item.target_layer === layer) && (!target || item.target === target) && (!topic || item.topic === topic)
    && (!$("gen1Pending").checked || !item.decision));
}
function gen1Render() {
  const list = $("gen1List");
  list.textContent = "";
  if (!G1.stage) return;
  const rows = gen1Filtered();
  if (!G1.stage.candidates.length) {
    list.append(el("div", { class: "hint", text: "此暂存区没有候选；没有任何画像事实可供确认。" }));
    return;
  }
  if (!rows.length) {
    list.append(el("div", { class: "hint", text: "当前过滤条件下没有待确认候选。" }));
    return;
  }
  for (const item of rows) {
    const decision = item.decision;
    const row = el("div", { class: "gen1-row" },
      el("div", { text: item.content }),
      el("div", { class: "gen1-meta", text: `${item.target_layer} · ${item.target} · ${item.topic} · 来源 ${item.source}` }),
      el("div", { class: "gen1-meta", text: `隐私 ${item.privacy} · 可见性 ${item.visibility} · 哈希 ${item.content_sha256.slice(0, 12)}` }));
    if (decision) {
      row.append(el("div", { class: "gen1-decision", text: `已暂存决定：${decision.action}（尚未导入 workspace）` }));
    } else {
      const actions = el("div", { class: "gen1-actions" });
      actions.append(gen1Button("采纳", () => gen1Decide(item, "accept")));
      actions.append(gen1Button("修改", () => {
        const text = prompt("修改候选内容（只写暂存决定）：", item.content);
        if (text !== null) gen1Decide(item, "edit", { text });
      }));
      actions.append(gen1Button("改层", () => {
        const target_layer = prompt("目标层：L1 或 L3", item.target_layer);
        if (target_layer === null) return;
        const target = prompt("目标位置（例如 core.user.owner 或 people/owner）：", item.target);
        if (target !== null) gen1Decide(item, "move", { target_layer, target });
      }));
      actions.append(gen1Button("不要", () => gen1Decide(item, "reject")));
      row.append(actions);
    }
    list.append(row);
  }
}
function gen1Button(text, fn) {
  const button = el("button", { type: "button", text, "data-alt": "gen1-" + text });
  button.addEventListener("click", fn);
  return button;
}
function gen1ShowStage(stage) {
  G1.stage = stage;
  const candidates = stage.candidates || [];
  gen1Options("gen1Target", [...new Set(candidates.map(item => item.target).filter(Boolean))].sort());
  gen1Options("gen1Topic", [...new Set(candidates.map(item => item.topic).filter(Boolean))].sort());
  gen1Say(`候选 ${candidates.length} 条，待确认 ${stage.pending} 条。所有决定仍只在暂存区。`);
  gen1Render();
}
async function gen1LoadSelected() {
  const dir = $("gen1Stage").value;
  if (!dir) { G1.stage = null; gen1Say("还没有可用的 gen1 暂存区。"); $("gen1List").textContent = ""; return; }
  const r = await getJson("/api/staging/gen1", { dir });
  if (!r.ok) { gen1Say("暂存读取失败：" + (r.data.reason || "未知错误"), "spk-bad"); return; }
  gen1ShowStage(r.data.stage);
}
async function gen1Refresh() {
  const r = await getJson("/api/staging/gen1");
  if (!r.ok) { gen1Say("暂存列表读取失败：" + (r.data.reason || "未知错误"), "spk-bad"); return; }
  const select = $("gen1Stage"), previous = select.value;
  select.textContent = "";
  for (const item of r.data.items || []) {
    select.append(el("option", { value: item.dir, text: `${item.dir}（待确认 ${item.pending}/${item.candidates}）` }));
  }
  if (!select.options.length) {
    G1.stage = null; $("gen1List").textContent = "";
    gen1Say("没有找到 gen1 暂存区；尚未导入或生成任何真实画像。");
    return;
  }
  select.value = [...select.options].some(option => option.value === previous) ? previous : select.options[0].value;
  await gen1LoadSelected();
}
async function gen1Decide(item, action, extra) {
  const body = { dir: G1.stage.dir, id: item.id, action, ...(extra || {}) };
  const r = await api("/api/staging/gen1", body);
  if (!r.ok) { gen1Say("未写入暂存决定：" + (r.data.reason || "未知错误"), "spk-bad"); return; }
  gen1ShowStage(r.data.stage);
}

$("btnGen1Refresh").addEventListener("click", gen1Refresh);
$("gen1Stage").addEventListener("change", gen1LoadSelected);
for (const id of ["gen1Layer", "gen1Target", "gen1Topic", "gen1Pending"]) $(id).addEventListener("change", gen1Render);
gen1Refresh();
