/* RoundTable Lite 前端（需求 6）：原生 JS，无构建、无 CDN。
   原则：所有动态文本一律 textContent 注入，杜绝 XSS。 */
"use strict";

const $ = (sel) => document.querySelector(sel);
const listEl = $("#messages");

/* ---------------- 基础工具 ---------------- */

let toastTimer = null;
function toast(msg, ms = 2600) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), ms);
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch { /* ignore */ }
  if (!res.ok) {
    const d = (data && data.detail) || (data || {});
    const err = new Error(typeof d === "string" ? d : (d.reason || `HTTP ${res.status}`));
    err.status = res.status;
    err.data = d;
    throw err;
  }
  return data;
}

function fmtTime(iso) { return (iso || "").slice(11, 16); }
function esc(s) { return s ?? ""; }

/* ---------------- 全局状态 ---------------- */

const S = {
  meetings: [],
  meetingId: null,
  meeting: null,
  participants: [],
  forms: [],
  messages: new Map(), // id -> message
  lastSeq: 0,
  ws: null,
  wsBackoff: 1000,
  wsGen: 0, // 代际：切换会议后旧连接的回调作废
};

/* ---------------- 会议列表 ---------------- */

async function loadMeetings() {
  const d = await api("/api/meetings");
  S.meetings = d.meetings || [];
  renderMeetingList();
}

function renderMeetingList() {
  const ul = $("#meeting-list");
  ul.textContent = "";
  for (const m of S.meetings) {
    const li = document.createElement("li");
    if (m.id === S.meetingId) li.classList.add("active");
    const name = document.createElement("div");
    name.className = "grow";
    const n1 = document.createElement("div");
    n1.className = "name"; n1.textContent = m.name;
    const n2 = document.createElement("div");
    n2.className = "sub"; n2.textContent = `${m.participant_count ?? 0} 成员 · ${m.message_count ?? 0} 条`;
    name.append(n1, n2);
    li.append(name);
    li.addEventListener("click", () => openMeeting(m.id));
    ul.append(li);
  }
}

/* ---------------- 进入会议 ---------------- */

function resetMeetingView() {
  S.messages.clear();
  S.lastSeq = 0;
  S.participants = [];
  S.forms = [];
  listEl.textContent = "";
  const hint = document.createElement("div");
  hint.className = "empty-hint";
  hint.textContent = "还没有消息，说点什么吧";
  listEl.append(hint);
}

async function openMeeting(id) {
  S.meetingId = id;
  S.wsGen += 1;
  resetMeetingView();
  renderMeetingList();
  $("#control-bar").classList.remove("hidden");
  $("#composer").classList.remove("hidden");
  await Promise.all([loadDetail(), loadMessages(0), loadForms()]);
  connectWS();
}

async function loadDetail() {
  const d = await api(`/api/meetings/${S.meetingId}`);
  S.meeting = d.meeting;
  S.participants = d.participants || [];
  $("#meeting-title").textContent = S.meeting.name;
  renderParticipants();
  updateControlBar();
}

async function loadParticipants() {
  if (!S.meetingId) return;
  const d = await api(`/api/meetings/${S.meetingId}/participants`);
  S.participants = d.participants || [];
  renderParticipants();
}

async function loadForms() {
  if (!S.meetingId) return;
  const d = await api(`/api/meetings/${S.meetingId}/forms`);
  S.forms = d.forms || [];
  renderForms();
}

async function loadMessages(after) {
  if (!S.meetingId) return;
  const d = await api(`/api/meetings/${S.meetingId}/messages?after_seq=${after}`);
  for (const m of d.messages || []) upsertMessage(m);
  S.lastSeq = Math.max(S.lastSeq, d.max_seq || S.lastSeq);
}

/* ---------------- 消息渲染 ---------------- */

function nearBottom() {
  return listEl.scrollHeight - listEl.scrollTop - listEl.clientHeight < 80;
}

function scrollBottom() { listEl.scrollTop = listEl.scrollHeight; }

function upsertMessage(m) {
  const stick = nearBottom();
  S.messages.set(m.id, m);
  S.lastSeq = Math.max(S.lastSeq, m.seq || 0);
  let el = listEl.querySelector(`[data-id="${m.id}"]`);
  const fresh = renderMessage(m);
  if (el) {
    el.replaceWith(fresh);
  } else {
    // 按 seq 有序插入（WS 与 REST 补齐竞态时保序）
    el = fresh;
    const after = [...listEl.children].find(
      (c) => c.dataset && c.dataset.seq && Number(c.dataset.seq) > m.seq
    );
    const hint = listEl.querySelector(".empty-hint");
    if (hint) hint.remove();
    if (after) listEl.insertBefore(el, after);
    else listEl.append(el);
  }
  const empty = listEl.querySelector(".empty-hint");
  if (empty && S.messages.size > 0) empty.remove();
  if (stick) scrollBottom();
}

function renderMessage(m) {
  const div = document.createElement("div");
  div.dataset.id = m.id;
  div.dataset.seq = String(m.seq ?? 0);

  if (m.author_kind === "system") {
    div.className = "bubble system";
    div.textContent = m.content;
    return div;
  }

  div.className = "bubble" + (m.author_kind === "human" ? " mine" : "");
  if (m.status === "failed") div.classList.add("failed");
  if (m.status === "streaming") div.classList.add("streaming");

  const meta = document.createElement("div");
  meta.className = "meta";
  const nm = document.createElement("span");
  nm.textContent = m.author_name;
  const tm = document.createElement("span");
  tm.textContent = fmtTime(m.created_at);
  meta.append(nm, tm);
  div.append(meta);

  const body = document.createElement("div");
  body.className = "content";
  if (m.status === "streaming") {
    body.classList.add("dots");
    body.textContent = " 正在思考";
  } else {
    body.textContent = m.content || "";
  }
  div.append(body);

  if (m.status === "failed" && m.error) {
    const tags = document.createElement("div");
    tags.className = "tags";
    const tag = document.createElement("span");
    tag.className = "tag err";
    tag.textContent = `失败：${m.error}`;
    tags.append(tag);
    div.append(tags);
  }
  if (m.delivery === "no_recipients") {
    const tags = document.createElement("div");
    tags.className = "tags";
    const tag = document.createElement("span");
    tag.className = "tag";
    tag.textContent = "未送达：没有命中任何成员";
    tags.append(tag);
    div.append(tags);
  }
  return div;
}

/* ---------------- 发送 ---------------- */

$("#composer").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("#input");
  const text = input.value.trim();
  if (!text || !S.meetingId) return;
  try {
    const d = await api(`/api/meetings/${S.meetingId}/messages`, {
      method: "POST",
      body: { content: text },
    });
    input.value = "";
    closeMentionPop();
    upsertMessage(d.message); // 服务端即返；Agent 回复随后经 WS 到达
  } catch (err) {
    if (err.status === 429) {
      const ra = err.data && err.data.retry_after;
      toast(`发送过快，请 ${ra ?? "?"} 秒后再试`, 3200);
    } else if (err.status === 409) {
      toast("会议已停止，请先点「重置」", 3200);
    } else {
      toast(`发送失败：${err.message}`, 3200);
    }
  }
});

$("#input").addEventListener("keydown", (e) => {
  // @ 浮层打开时，Enter/Tab 先用于选择
  if (!$("#mention-pop").classList.contains("hidden")) {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      moveMentionSel(e.key === "ArrowDown" ? 1 : -1);
      return;
    }
    if (e.key === "Enter" || e.key === "Tab") {
      e.preventDefault();
      applyMentionSel();
      return;
    }
    if (e.key === "Escape") { closeMentionPop(); return; }
  }
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    $("#composer").requestSubmit();
  }
});

/* ---------------- @ 自动补全（需求 F5） ---------------- */

const mention = { items: [], sel: 0, from: -1, to: -1 };

function updateMentionPop() {
  const input = $("#input");
  const pos = input.selectionStart;
  const before = input.value.slice(0, pos);
  const m = before.match(/(^|\s)@([^\s@＠]{0,40})$/);
  const pop = $("#mention-pop");
  if (!m || !S.participants.length) { closeMentionPop(); return; }
  const q = m[2].toLowerCase();
  mention.items = S.participants.filter(
    (p) => p.enabled && p.display_name.toLowerCase().startsWith(q)
  ).slice(0, 8);
  if (!mention.items.length) { closeMentionPop(); return; }
  mention.sel = 0;
  mention.from = pos - m[2].length - 1;
  mention.to = pos;
  pop.textContent = "";
  mention.items.forEach((p, i) => {
    const it = document.createElement("div");
    it.className = "item" + (i === 0 ? " sel" : "");
    it.textContent = (p.kind === "human" ? "👤 " : "🤖 ") + p.display_name;
    it.addEventListener("mousedown", (ev) => {
      ev.preventDefault();
      mention.sel = i;
      applyMentionSel();
    });
    pop.append(it);
  });
  pop.classList.remove("hidden");
}

function renderMentionSel() {
  [...$("#mention-pop").children].forEach((el, i) =>
    el.classList.toggle("sel", i === mention.sel));
}

function moveMentionSel(d) {
  if (!mention.items.length) return;
  mention.sel = (mention.sel + d + mention.items.length) % mention.items.length;
  renderMentionSel();
}

function applyMentionSel() {
  const p = mention.items[mention.sel];
  if (!p) { closeMentionPop(); return; }
  const input = $("#input");
  const v = input.value;
  input.value = v.slice(0, mention.from) + "@" + p.display_name + " " + v.slice(mention.to);
  const np = mention.from + p.display_name.length + 2;
  input.setSelectionRange(np, np);
  closeMentionPop();
  input.focus();
}

function closeMentionPop() {
  mention.items = [];
  $("#mention-pop").classList.add("hidden");
}

$("#input").addEventListener("input", updateMentionPop);
$("#input").addEventListener("blur", () => setTimeout(closeMentionPop, 120));

/* ---------------- 参与者侧栏（F11） ---------------- */

function renderParticipants() {
  const ul = $("#participant-list");
  ul.textContent = "";
  for (const p of S.participants) {
    const li = document.createElement("li");
    const dot = document.createElement("span");
    dot.className = "dot " + (p.kind === "human" ? "human" : (p.status || "idle"));
    dot.title = p.kind === "human" ? "主席（我）" : (p.status || "idle");
    const grow = document.createElement("div");
    grow.className = "grow";
    const n = document.createElement("div");
    n.className = "name";
    n.textContent = p.display_name + (p.kind === "human" ? "（我）" : "");
    const sub = document.createElement("div");
    sub.className = "sub";
    sub.textContent = p.kind === "human" ? "主席"
      : (p.transport === "mcp" ? "MCP" : `CLI ${p.enabled ? "" : "· 已停用"}`) +
        (p.status === "failed" ? " · 上次失败" : "");
    grow.append(n, sub);
    li.append(dot, grow);
    if (p.kind === "agent") {
      const kick = document.createElement("button");
      kick.className = "btn btn-sm btn-danger";
      kick.textContent = "踢出";
      kick.addEventListener("click", () => kickParticipant(p));
      li.append(kick);
    }
    ul.append(li);
  }
}

async function kickParticipant(p) {
  if (!confirm(`把 ${p.display_name} 移出会议？`)) return;
  try {
    await api(`/api/meetings/${S.meetingId}/participants/${p.id}`, { method: "DELETE" });
    await loadDetail();
  } catch (err) { toast(`移除失败：${err.message}`); }
}

/* ---------------- 加入成员对话框（F7 + 预设） ---------------- */

let presetsCache = null;

$("#btn-add-participant").addEventListener("click", async () => {
  if (!S.meetingId) { toast("请先选择会议"); return; }
  const dlg = $("#dlg-participant");
  $("#form-participant").reset();
  $("#p-cli-fields").style.display = "";
  if (!presetsCache) {
    try {
      const d = await api("/api/presets");
      presetsCache = d.presets || [];
      const sel = $("#p-preset");
      for (const p of presetsCache) {
        const opt = document.createElement("option");
        opt.value = p.key;
        opt.textContent = p.name + (p.available ? "" : "（不可用）");
        sel.append(opt);
      }
    } catch { presetsCache = []; }
  }
  dlg.showModal();
});

$("#p-preset").addEventListener("change", (e) => {
  const p = (presetsCache || []).find((x) => x.key === e.target.value);
  if (!p) return;
  $("#p-name").value = p.display_name;
  $("#p-transport").value = p.transport;
  $("#p-cmd").value = p.cli_cmd || "";
  $("#p-args").value = JSON.stringify(p.cli_args || [], null, 0);
  $("#p-sp").value = p.system_prompt || "";
  $("#p-trigger").value = p.trigger_mode || "mention";
  if (p.note) toast(p.note, 3600);
  $("#p-cli-fields").style.display = p.transport === "cli" ? "" : "none";
});

$("#p-transport").addEventListener("change", (e) => {
  $("#p-cli-fields").style.display = e.target.value === "cli" ? "" : "none";
});

$("#form-participant").addEventListener("submit", async (e) => {
  e.preventDefault();
  let args = [];
  const raw = $("#p-args").value.trim();
  if (raw) {
    try { args = JSON.parse(raw); }
    catch { toast("参数必须是 JSON 数组"); return; }
  }
  try {
    await api(`/api/meetings/${S.meetingId}/participants`, {
      method: "POST",
      body: {
        display_name: $("#p-name").value.trim(),
        transport: $("#p-transport").value,
        cli_cmd: $("#p-cmd").value.trim(),
        cli_args: args,
        system_prompt: $("#p-sp").value.trim(),
        trigger_mode: $("#p-trigger").value,
      },
    });
    $("#dlg-participant").close();
    await loadDetail();
  } catch (err) { toast(`加入失败：${err.message}`, 3600); }
});

/* ---------------- 表单向导（F10） ---------------- */

function renderForms() {
  const ul = $("#form-list");
  ul.textContent = "";
  const pending = S.forms.filter((f) => f.status === "pending");
  const badge = $("#form-badge");
  badge.classList.toggle("hidden", pending.length === 0);
  badge.textContent = String(pending.length);
  for (const f of pending) {
    const li = document.createElement("li");
    const grow = document.createElement("div");
    grow.className = "grow";
    const n = document.createElement("div");
    n.className = "name"; n.textContent = f.title;
    const sub = document.createElement("div");
    sub.className = "sub";
    sub.textContent = `${f.author_name} · ${fmtTime(f.created_at)}`;
    grow.append(n, sub);
    li.append(grow);
    li.addEventListener("click", () => openFormDlg(f));
    ul.append(li);
  }
}

function openFormDlg(f) {
  $("#f-title").textContent = f.title;
  $("#f-author").textContent = `来自 ${f.author_name} · ${fmtTime(f.created_at)}`;
  const box = $("#f-fields");
  box.textContent = "";
  let fields = [];
  try { fields = JSON.parse(f.fields || "[]"); } catch { fields = []; }
  for (const fd of fields) {
    const block = document.createElement("div");
    block.className = "field-block";
    const lab = document.createElement("div");
    lab.className = "label";
    lab.textContent = (fd.label || fd.key) + (fd.required ? " *" : "");
    block.append(lab);
    if (fd.type === "radio" || fd.type === "checkbox") {
      for (const opt of fd.options || []) {
        const wrap = document.createElement("label");
        wrap.className = "opt";
        const input = document.createElement("input");
        input.type = fd.type;
        input.name = fd.key + (fd.type === "radio" ? "_r" : "_c");
        input.value = opt;
        input.dataset.key = fd.key;
        wrap.append(input, document.createTextNode(" " + opt));
        block.append(wrap);
      }
    } else if (fd.type === "textarea") {
      const input = document.createElement("textarea");
      input.rows = 3;
      input.dataset.key = fd.key;
      input.dataset.type = "textarea";
      block.append(input);
    } else {
      const input = document.createElement("input");
      input.type = "text";
      input.dataset.key = fd.key;
      input.dataset.type = "text";
      block.append(input);
    }
    block.dataset.key = fd.key;
    block.dataset.required = fd.required ? "1" : "";
    box.append(block);
  }
  $("#form-answer").dataset.formId = f.id;
  $("#dlg-form").showModal();
}

$("#form-answer").addEventListener("submit", async (e) => {
  e.preventDefault();
  const formEl = e.target;
  const fid = formEl.dataset.formId;
  const answer = {};
  for (const block of formEl.querySelectorAll(".field-block")) {
    const key = block.dataset.key;
    const required = block.dataset.required === "1";
    const radios = [...block.querySelectorAll('input[type=radio]')];
    const boxes = [...block.querySelectorAll('input[type=checkbox]')];
    if (radios.length) {
      const sel = radios.find((r) => r.checked);
      if (required && !sel) { toast("有必填项未作答"); return; }
      if (sel) answer[key] = sel.value;
    } else if (boxes.length) {
      const sel = boxes.filter((b) => b.checked).map((b) => b.value);
      if (required && !sel.length) { toast("有必填项未作答"); return; }
      if (sel.length) answer[key] = sel;
    } else {
      const input = block.querySelector("input[type=text],textarea");
      const val = (input.value || "").trim();
      if (required && !val) { toast("有必填项未作答"); return; }
      if (val) answer[key] = val;
    }
  }
  try {
    await api(`/api/meetings/${S.meetingId}/forms/${fid}/answer`, {
      method: "POST", body: { answer },
    });
    $("#dlg-form").close();
    $("#form-answer").reset();
    toast("已回答，答案已回灌会议");
    await loadForms();
  } catch (err) { toast(`提交失败：${err.message}`); }
});

$("#f-cancel").addEventListener("click", async () => {
  const fid = $("#form-answer").dataset.formId;
  try {
    await api(`/api/meetings/${S.meetingId}/forms/${fid}/answer`, {
      method: "POST", body: { cancel: true },
    });
    $("#dlg-form").close();
    await loadForms();
  } catch (err) { toast(`操作失败：${err.message}`); }
});

/* ---------------- 主席控制条（F14） ---------------- */

function updateControlBar() {
  const st = S.meeting ? S.meeting.status : "running";
  const chip = $("#meeting-status");
  chip.textContent = st;
  chip.className = "chip" + (st !== "running" ? " " + st : "");
  $("#btn-pause").textContent = st === "paused" ? "继续" : "暂停";
}

$("#btn-pause").addEventListener("click", async () => {
  const paused = S.meeting && S.meeting.status !== "paused";
  try {
    await api("/api/control/pause", { method: "POST", body: { paused, meeting_id: S.meetingId } });
    await loadDetail();
  } catch (err) { toast(`操作失败：${err.message}`); }
});

$("#btn-stop").addEventListener("click", async () => {
  if (!confirm("停止会议？正在运行的 Agent 子进程将被终止。")) return;
  try {
    await api("/api/control/stop", { method: "POST", body: { meeting_id: S.meetingId } });
    await loadDetail();
  } catch (err) { toast(`操作失败：${err.message}`); }
});

$("#btn-reset").addEventListener("click", async () => {
  try {
    await api("/api/control/reset", { method: "POST", body: { meeting_id: S.meetingId } });
    await loadDetail();
    toast("已恢复 running");
  } catch (err) { toast(`操作失败：${err.message}`); }
});

$("#btn-meeting-delete").addEventListener("click", async () => {
  const m = S.meeting;
  if (!m || !S.meetingId) return;
  if (!confirm(`删除会议「${m.name}」？\n消息、成员、表单将一并清除，运行中的 Agent 子进程会被终止，不可恢复。`)) return;
  try {
    await api(`/api/meetings/${S.meetingId}`, { method: "DELETE" });
    toast(`已删除「${m.name}」`);
    S.meetingId = null;
    S.meeting = null;
    S.wsGen += 1;                       // 作废旧 WS 回调
    if (S.ws) { try { S.ws.close(); } catch { /* ignore */ } S.ws = null; }
    $("#control-bar").classList.add("hidden");
    $("#composer").classList.add("hidden");
    resetMeetingView();
    await loadMeetings();
  } catch (err) { toast(`删除失败：${err.message}`); }
});

/* ---------------- WebSocket（F13：指数退避重连 + after_seq 补齐） ---------------- */

function connectWS() {
  if (S.ws) { try { S.ws.close(); } catch { /* ignore */ } S.ws = null; }
  const gen = S.wsGen;
  const mid = S.meetingId;
  if (!mid) return;
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws?meeting_id=${encodeURIComponent(mid)}`);
  S.ws = ws;

  ws.onopen = () => {
    if (gen !== S.wsGen) return;
    S.wsBackoff = 1000;
    // 重连后按 after_seq 补齐（需求 5.2）
    loadMessages(S.lastSeq).catch(() => {});
    loadParticipants().catch(() => {});
  };

  ws.onmessage = (ev) => {
    if (gen !== S.wsGen) return;
    let data;
    try { data = JSON.parse(ev.data); } catch { return; }
    handleEvent(data.event, data.data || {});
  };

  ws.onclose = () => {
    if (gen !== S.wsGen) return;
    setTimeout(() => { if (gen === S.wsGen) connectWS(); }, S.wsBackoff);
    S.wsBackoff = Math.min(S.wsBackoff * 2, 10000);
  };
  ws.onerror = () => { try { ws.close(); } catch { /* ignore */ } };
}

function handleEvent(event, d) {
  switch (event) {
    case "message.new":
      if (!S.messages.has(d.id)) upsertMessage(d);
      break;
    case "message.updated":
      upsertMessage(d);
      break;
    case "participant.status": {
      const p = S.participants.find((x) => x.id === d.id);
      if (p) { p.status = d.status; renderParticipants(); }
      break;
    }
    case "participant.new":
    case "participant.deleted":
      loadParticipants().catch(() => {});
      break;
    case "form.pending":
      loadForms().catch(() => {});
      toast(`新表单：${d.title || ""}`, 3600);
      break;
    case "form.answered":
      loadForms().catch(() => {});
      break;
    case "control.state":
      if (S.meeting && d.meeting_id === S.meetingId) {
        S.meeting.status = d.stopped ? "stopped" : (d.paused ? "paused" : "running");
        updateControlBar();
      }
      break;
    case "meeting.deleted":
      toast("该会议已被删除");
      S.meetingId = null; S.meeting = null; S.wsGen += 1;
      if (S.ws) { try { S.ws.close(); } catch { /* ignore */ } }
      $("#control-bar").classList.add("hidden");
      $("#composer").classList.add("hidden");
      resetMeetingView();
      loadMeetings().catch(() => {});
      break;
    default:
      break;
  }
}

/* ---------------- 新建会议（F1） ---------------- */

$("#btn-new-meeting").addEventListener("click", () => {
  $("#form-meeting").reset();
  $("#dlg-meeting").showModal();
});

$("#m-save").addEventListener("click", async (e) => {
  e.preventDefault();
  const name = $("#m-name").value.trim();
  if (!name) { toast("请填写会议名称"); return; }
  try {
    const d = await api("/api/meetings", {
      method: "POST",
      body: { name, description: $("#m-desc").value.trim() },
    });
    $("#dlg-meeting").close();
    await loadMeetings();
    await openMeeting(d.meeting.id);
  } catch (err) { toast(`创建失败：${err.message}`); }
});

/* 对话框关闭按钮 */
document.querySelectorAll("[data-close]").forEach((btn) => {
  btn.addEventListener("click", () => btn.closest("dialog").close());
});

/* ---------------- 兜底轮询：WS 不可用时每 8s 补齐 ---------------- */

setInterval(() => {
  if (!S.meetingId) return;
  const wsOk = S.ws && S.ws.readyState === WebSocket.OPEN;
  if (!wsOk) {
    loadMessages(S.lastSeq).catch(() => {});
    loadParticipants().catch(() => {});
    loadForms().catch(() => {});
  }
}, 8000);

/* ---------------- 接入帮助（? 按钮） ---------------- */

const HUB = `${location.protocol}//${location.host}`;

const HELP_TYPES = {
  codex: {
    note: "适用于 VS Code / Obsidian 等一切使用本机 codex 的对话（前提：roundtable MCP 已注册，命令见下方；注册后重启插件宿主一次）。",
    cfg: true,
    prompt(name, mid) {
      const joinCall = mid
        ? `join(name="${name}", meeting_id="${mid}")`
        : `join(name="${name}")`;
      return [
        `你是圆桌会议成员，显示名 ${name}。使用 roundtable 的 MCP 工具：`,
        `1) 调用 ${joinCall} 加入会议，阅读返回的行为协议；`,
        "2) 然后进入循环：调用 listen 等待新消息（单次最多阻塞 20 秒，返回后继续）；",
        `3) 有人 @${name} 或需要你回应时，思考后用 say 发言，内容里可以 @其他成员 触发接力；`,
        "4) 需要向主持人提问时用 ask_operator 推表单；",
        "5) 持续值守，不要主动退出，也不要反复向我要指令。",
      ].join("\n");
    },
  },
  dsh: {
    note: "适用于 DeepSeek Harness 会话（前提：已安装 roundtable-join 技能，默认装在 ~/.dsh/skills，对所有 DSH 会话生效）。提示词已含执行环境解法：首选 /permission 切「完全权限」（一次确认不再弹窗），备选逐条提权。",
    cfg: false,
    prompt(name, mid) {
      const midPart = mid ? `meeting_id "${mid}" ` : "";
      return [
        `调用 roundtable-join 技能，以显示名 ${name} 加入圆桌会议（${midPart}默认最近活跃会议）并值守。`,
        "重要（执行环境）：本机沙箱有故障，沙箱内 pwsh 必然 0xC0000142 崩溃。两种解法任选：",
        "① 首选：在会话输入 /permission 把权限切到「完全权限」（只需确认一次，之后命令直接执行、不再弹批准窗）；",
        "② 备选：每条命令都用 sandbox_permissions=danger-full-access 提权运行（每条都要我点允许）。",
        "要求：",
        `- 被 @${name} 的消息必须回应；发言精炼 ≤300 字，不复述上文`,
        "- 始终用你的席位 author_id 发言，不要以主席名义",
        "- 429 限流按 retry_after 等待后重试；会议停止则静默",
        "持续值守，直到我说退出会议。",
      ].join("\n");
    },
  },
  workbuddy: {
    note: "适用于能发 HTTP 请求的任何 Agent（WorkBuddy 等），按项目 docs/workbuddy-skill.md 的 REST 约定参会，无需任何配置。",
    cfg: false,
    prompt(name, mid) {
      const midPart = mid
        ? `目标会议 meeting_id 为 "${mid}"。`
        : "目标会议选最近创建且未停止的一个。";
      return [
        `你是圆桌会议成员，显示名 ${name}。先读本项目目录下 docs/workbuddy-skill.md（与启动脚本同级的 docs 文件夹），严格按其中的接口与行为约定参会。`,
        `${midPart}Hub 地址 ${HUB}。`,
        `流程：POST participants 建席位（display_name="${name}"，transport=mcp），保存响应里的 participant.id 作为 author_id；`,
        `然后循环：GET messages?after_seq=已见最大seq → 被 @${name} 或需要补充时，POST messages 带 author_id 发言（content 里可 @其他成员 触发接力）。`,
        "注意：若命令执行报 0xC0000142 崩溃（沙箱环境故障），首选让用户在会话里用 /permission 切到「完全权限」；否则改用提权（非沙箱 / danger-full-access）模式逐条执行。",
        "429 按 retry_after 等待后重试；发言精炼 ≤300 字；持续值守，直到我说退出会议。",
      ].join("\n");
    },
  },
};
let helpType = "codex";

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    try {  // 非安全上下文兜底
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.append(ta);
      ta.select();
      const ok = document.execCommand("copy");
      ta.remove();
      return ok;
    } catch { return false; }
  }
}

function refreshHelpPrompt() {
  const name = ($("#h-name").value.trim() || "Codex").replace(/\s+/g, "");
  $("#h-name").value = name;
  const t = HELP_TYPES[helpType];
  $("#h-prompt").value = t.prompt(name, S.meetingId);
  $("#h-note").textContent = t.note;
  $("#h-cfg-row").classList.toggle("hidden", !t.cfg);
}

$("#h-type-group").addEventListener("click", (e) => {
  const btn = e.target.closest(".seg");
  if (!btn) return;
  helpType = btn.dataset.type;
  document.querySelectorAll("#h-type-group .seg").forEach((b) =>
    b.classList.toggle("active", b === btn));
  refreshHelpPrompt();
});

$("#btn-help").addEventListener("click", () => {
  refreshHelpPrompt();
  $("#dlg-help").showModal();
});
$("#h-name").addEventListener("input", refreshHelpPrompt);
$("#h-copy").addEventListener("click", async () => {
  const ok = await copyText($("#h-prompt").value);
  toast(ok ? "提示词已复制，去 Codex 新开对话粘贴发送" : "复制失败，请手动选择文本复制");
});
$("#h-copy-cfg").addEventListener("click", async () => {
  const ok = await copyText($("#h-cfg").textContent);
  toast(ok ? "配置命令已复制" : "复制失败，请手动选择文本复制");
});

/* ---------------- 启动 ---------------- */

loadMeetings().catch((e) => toast(`加载失败：${e.message}`));
