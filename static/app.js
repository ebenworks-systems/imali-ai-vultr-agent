/* Imali AI month-end agent: the page. No framework; every list is paged and its state is in
   the address, so the phone's back button works. */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const PAGE = 30;

const S = {
  state: null,
  accounts: {},
  closeId: null,
  postId: null,
  closeStatus: null,
  stream: null,
  lastEvent: {},
  found: [],
  owes: [],
  review: { offset: 0, total: 0 },
  book: { offset: 0, total: 0 },
  timer: null,
  recording: null,
};

/* ---------- helpers ---------- */

function el(tag, attrs = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") n.className = v;
    else if (k === "text") n.textContent = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v === true ? "" : v);
  }
  for (const k of kids.flat()) if (k !== null && k !== undefined && k !== false) n.append(k.nodeType ? k : document.createTextNode(String(k)));
  return n;
}

function svg(path, cls = "") {
  const s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  s.setAttribute("viewBox", "0 0 24 24");
  s.setAttribute("aria-hidden", "true");
  if (cls) s.setAttribute("class", cls);
  for (const d of path.split("|")) {
    const p = document.createElementNS("http://www.w3.org/2000/svg", "path");
    p.setAttribute("d", d);
    s.append(p);
  }
  return s;
}
const ICON = {
  chev: "M9 6l6 6-6 6",
  tick: "M5 12.5 10 17 19 7",
  dot: "M12 8v5M12 16.5v.5|M4 12a8 8 0 1 0 16 0 8 8 0 1 0-16 0",
  mic: "M12 3a3 3 0 0 0-3 3v5a3 3 0 0 0 6 0V6a3 3 0 0 0-3-3z|M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21",
  copy: "M8 8h11v11H8z|M5 15V5h10",
};

function rand(cents, signed = false) {
  const n = Number(cents || 0);
  const neg = n < 0;
  const abs = Math.abs(n);
  const whole = Math.floor(abs / 100).toString().replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  const s = `R${whole}.${String(abs % 100).padStart(2, "0")}`;
  if (neg) return `-${s}`;
  return signed && n > 0 ? `+${s}` : s;
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const MONTHS_LONG = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
function day(iso) {
  if (!iso) return "";
  const [y, m, d] = iso.slice(0, 10).split("-").map(Number);
  return `${d} ${MONTHS[m - 1]}`;
}
function dayLong(iso) {
  const [y, m, d] = iso.slice(0, 10).split("-").map(Number);
  return `${d} ${MONTHS_LONG[m - 1]} ${y}`;
}
function clock(iso) {
  const t = new Date(iso);
  return `${day(iso)} ${String(t.getHours()).padStart(2, "0")}:${String(t.getMinutes()).padStart(2, "0")}`;
}

let toastTimer;
function toast(text) {
  const t = $("[data-toast]");
  t.textContent = text;
  t.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("show"), 3200);
}

class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

async function api(path, { method = "GET", body, form } = {}) {
  const opts = { method, headers: {}, credentials: "same-origin" };
  if (method !== "GET") opts.headers["X-Requested-With"] = "imali";
  if (form) opts.body = form;
  else if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  let r;
  try {
    r = await fetch(path, opts);
  } catch {
    throw new ApiError(0, "The connection dropped. Please try again.");
  }
  let data = {};
  try { data = await r.json(); } catch { /* an empty or non-JSON answer */ }
  if (!r.ok) {
    if (r.status === 401) copyEnded();
    throw new ApiError(r.status, data.error || "Something went wrong. Please try again.");
  }
  return data;
}

/* ---------- the address ---------- */

function params() { return new URLSearchParams(location.search); }
function go(changes, { replace = false } = {}) {
  const p = params();
  for (const [k, v] of Object.entries(changes)) {
    if (v === null || v === "" || v === undefined) p.delete(k); else p.set(k, v);
  }
  const url = `${location.pathname}?${p.toString()}`;
  if (replace) history.replaceState(null, "", url); else history.pushState(null, "", url);
  route();
}

function route() {
  const p = params();
  const view = ["agent", "close", "books"].includes(p.get("v")) ? p.get("v") : "agent";
  for (const panel of $$("[data-panel]")) panel.hidden = panel.dataset.panel !== view;
  for (const a of $$("[data-view]")) {
    if (a.classList.contains("island-logo")) continue;
    if (a.dataset.view === view) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  }
  if (view === "close") renderReview();
  if (view === "books") renderBook();
}

/* ---------- the copy ---------- */

async function loadState({ quiet = false } = {}) {
  const s = await api("/api/state");
  if (!s.open) {
    if (!quiet) copyEnded();
    return null;
  }
  S.state = s;
  S.accounts = s.accounts || {};
  $$("[data-shop]").forEach((n) => (n.textContent = s.shop));
  $('[data-fig="bank"]').textContent = rand(s.bank);
  $('[data-fig="cash"]').textContent = rand(s.cash);
  $('[data-fig="debtors"]').textContent = rand(s.debtors);
  $('[data-fig="creditors"]').textContent = rand(s.creditors);
  const waiting = Object.entries(s.pending || {}).reduce((n, [k, v]) => (k === "followup" ? n : n + v), 0);
  $('[data-fig="pending"]').textContent = `${waiting} ${waiting === 1 ? "item" : "items"}`;
  $('[data-fig-note="bank"]').textContent = s.post && s.post.status === "done"
    ? "October's statement is posted and reconciled."
    : "October's bank lines are not in the books until you close the month.";
  if (s.statement) {
    $("[data-statement-name]").textContent = s.statement.file_name;
    $("[data-statement-meta]").textContent =
      `${s.statement.lines} lines from ${day(s.statement.period_start)} to ${day(s.statement.period_end)} ${s.statement.period_end.slice(0, 4)}.`;
  }
  const pend = (s.pending.match || 0) + (s.pending.flag || 0);
  $("[data-close-dot]").hidden = !(s.close && s.close.status === "waiting" && pend > 0);
  startTimer(s.expires_at);
  return s;
}

async function openCopy() {
  await api("/api/copy", { method: "POST" });
}

function startTimer(expires) {
  clearInterval(S.timer);
  const end = new Date(expires).getTime();
  const tick = () => {
    const left = Math.max(0, end - Date.now());
    const m = Math.ceil(left / 60000);
    const t = $("[data-timer]");
    t.textContent = left <= 0 ? "Copy ended" : m >= 60 ? `${Math.floor(m / 60)} h ${m % 60} min left` : `${m} min left`;
    if (left <= 0) { clearInterval(S.timer); copyEnded(); }
  };
  tick();
  S.timer = setInterval(tick, 20000);
}

function copyEnded() {
  const b = $("[data-banner]");
  b.replaceChildren(
    el("span", { text: "Your copy of the books has ended. A fresh copy starts with October's books as they were." }),
    el("button", { class: "btn btn-primary btn-sm", type: "button", onclick: () => startOver(false) }, "Open a fresh copy"),
  );
  b.hidden = false;
}

async function startOver(ask = true) {
  if (ask && !confirm("Start over with a fresh copy of the books? What you did in this copy will be gone.")) return;
  try {
    if (S.stream) S.stream.close();
    await openCopy();
    location.href = `${location.pathname}?v=${params().get("v") || "agent"}`;
  } catch (e) {
    toast(e.message);
  }
}

/* ---------- your agent ---------- */

function bubble(role, text, { voice = false } = {}) {
  const li = el("li", { class: `msg msg-${role}` },
    el("p", { class: "msg-who", text: role === "owner" ? "You" : "Your agent" }));
  const b = el("div", { class: "bubble" });
  if (voice) b.append(el("span", { class: "voice-tag" }, svg(ICON.mic), "Voice note"), el("br"));
  if (role === "agent") b.append(...formatted(text)); else b.append(text);
  li.append(b);
  return li;
}

/* The agent's answer as nodes, never as HTML: paragraphs, "- " lines as a list, **bold**. */
function inline(text) {
  return text.split(/(\*\*[^*]+\*\*)/).filter(Boolean).map((part) =>
    part.startsWith("**") && part.endsWith("**") ? el("strong", { text: part.slice(2, -2) }) : document.createTextNode(part));
}
function formatted(text) {
  const out = [];
  let list = null;
  for (const raw of String(text).split("\n")) {
    const line = raw.trim();
    if (!line) continue;
    const item = line.match(/^[-*•]\s+(.*)$/);
    if (item) {
      if (!list) { list = el("ul", { class: "say-list" }); out.push(list); }
      list.append(el("li", {}, ...inline(item[1])));
    } else {
      list = null;
      out.push(el("p", { class: "say-p" }, ...inline(line)));
    }
  }
  return out.length ? out : [document.createTextNode(String(text))];
}

function entryTable(entry) {
  const t = el("table", { class: "entry" },
    el("thead", {}, el("tr", {}, el("th", { text: "Account" }), el("th", { text: "Debit" }), el("th", { text: "Credit" }))));
  const tb = el("tbody");
  for (const e of entry) {
    tb.append(el("tr", {},
      el("td", { text: S.accounts[e.account] || e.account }),
      el("td", { text: e.debit ? rand(e.debit) : "" }),
      el("td", { text: e.credit ? rand(e.credit) : "" })));
  }
  t.append(tb);
  return t;
}

const STATUS_BADGE = {
  pending: ["Waiting for you", "badge-gold"],
  approved: ["Approved, posts in step 5", "badge-ok"],
  posted: ["Posted to the books", "badge-ok"],
  rejected: ["Rejected", "badge-off"],
};

function recordCard(p) {
  const card = el("div", { class: "card", "data-pid": p.id });
  card.append(
    el("div", { class: "card-top" }, el("p", { class: "card-title", text: p.title })),
    el("p", { class: "card-meta", text: [day(p.entry_date), p.detail].filter(Boolean).join(" · ") }),
  );
  if (p.note) card.append(el("p", { class: "card-reason", text: p.note }));
  if (p.entry && p.entry.length) card.append(entryTable(p.entry));
  card.append(decisionBar(p, card, async () => { await loadState(); }));
  return card;
}

function decisionBar(p, card, after, picker = null) {
  const bar = el("div", { class: "card-actions" });
  if (p.status !== "pending") {
    const [label, cls] = STATUS_BADGE[p.status] || [p.status, ""];
    bar.append(el("span", { class: `badge ${cls}` }, label));
    return bar;
  }
  const approve = el("button", { class: "btn btn-primary btn-sm", type: "button" }, "Approve");
  const reject = el("button", { class: "btn btn-quiet btn-sm", type: "button" }, "Reject");
  const busy = (on) => { approve.disabled = reject.disabled = on; };
  approve.addEventListener("click", async () => {
    busy(true);
    try {
      if (picker && !picker.value) { toast("Pick the account this belongs to first."); busy(false); return; }
      const body = picker && picker.value !== picker.dataset.initial ? { account: picker.value } : {};
      const r = await api(`/api/proposals/${p.id}/approve`, { method: "POST", body });
      p.status = r.status;
      bar.replaceWith(decisionBar(p, card, after));
      if (after) await after(r);
    } catch (e) { toast(e.message); busy(false); }
  });
  reject.addEventListener("click", async () => {
    busy(true);
    try {
      const r = await api(`/api/proposals/${p.id}/reject`, { method: "POST", body: {} });
      p.status = r.status;
      bar.replaceWith(decisionBar(p, card, after));
      if (after) await after(r);
    } catch (e) { toast(e.message); busy(false); }
  });
  if (picker) bar.append(picker);
  bar.append(approve, reject);
  return bar;
}

function scrollChat() {
  const m = $("[data-messages]");
  requestAnimationFrame(() => (m.scrollTop = m.scrollHeight));
}

function emptyChat() {
  return el("li", { class: "empty-chat" },
    el("strong", { text: `Hello ${S.state ? S.state.owner : ""}.` }),
    "Tell me what you sold or paid for today, or ask who owes you money. When October is ready to close, say so.");
}

async function loadChat() {
  const list = $("[data-messages]");
  const [{ messages }, records] = await Promise.all([
    api("/api/messages"),
    api("/api/proposals?kind=record&status=all&order=newest&limit=12"),
  ]);
  list.replaceChildren();
  if (!messages.length) list.append(emptyChat());
  for (const m of messages) list.append(bubble(m.role, m.body, { voice: m.via === "voice" }));
  const cards = records.items.reverse();
  if (cards.length) {
    const li = el("li", { class: "msg msg-agent" }, el("p", { class: "msg-who", text: "Your drafts" }));
    for (const p of cards) li.append(recordCard(p));
    list.append(li);
  }
  scrollChat();
}

function typing() {
  return el("li", { class: "msg msg-agent", "data-typing": true },
    el("div", { class: "bubble typing", "aria-label": "Your agent is writing" }, el("i"), el("i"), el("i")));
}

function showAnswer(out) {
  const list = $("[data-messages]");
  $$("[data-typing]", list).forEach((n) => n.remove());
  const li = bubble("agent", out.reply);
  for (const p of out.drafted || []) li.append(recordCard(p));
  if (out.job_id) {
    li.append(el("div", { class: "card" },
      el("p", { class: "card-title", text: "The month-end close has started." }),
      el("p", { class: "card-meta", text: "Each step shows what it found as it runs on the server." }),
      el("div", { class: "card-actions" },
        el("button", { class: "btn btn-primary btn-sm", type: "button", onclick: () => { go({ v: "close" }); watchClose(out.job_id); } }, "Watch the close"))));
  }
  list.append(li);
  scrollChat();
}

async function say(text) {
  const list = $("[data-messages]");
  $(".empty-chat", list)?.remove();
  list.append(bubble("owner", text), typing());
  scrollChat();
  const send = $(".send");
  send.disabled = true;
  try {
    showAnswer(await api("/api/messages", { method: "POST", body: { text } }));
    await loadState();
  } catch (e) {
    $$("[data-typing]", list).forEach((n) => n.remove());
    list.append(bubble("agent", e.message));
    scrollChat();
  } finally {
    send.disabled = false;
  }
}

function setupComposer() {
  const form = $("[data-composer]");
  const ta = $("#say");
  const grow = () => { ta.style.height = "auto"; ta.style.height = `${Math.min(ta.scrollHeight, 140)}px`; };
  ta.addEventListener("input", grow);
  ta.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); form.requestSubmit(); }
  });
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const text = ta.value.trim();
    if (!text) return;
    ta.value = "";
    grow();
    say(text);
  });
  $$("[data-suggest] .chip").forEach((c) => c.addEventListener("click", () => say(c.textContent.trim())));
  setupMic();
}

function setupMic() {
  const btn = $("[data-mic]");
  const time = $("[data-mic-time]");
  if (!navigator.mediaDevices || !window.MediaRecorder) {
    btn.disabled = true;
    btn.title = "This browser cannot record a voice note.";
    return;
  }
  const types = ["audio/webm;codecs=opus", "audio/ogg;codecs=opus", "audio/webm", "audio/mp4"];
  btn.addEventListener("click", async () => {
    if (S.recording) { S.recording.stop(); return; }
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch {
      toast("The microphone is not available. You can type instead.");
      return;
    }
    const type = types.find((t) => MediaRecorder.isTypeSupported(t)) || "";
    const rec = new MediaRecorder(stream, type ? { mimeType: type, audioBitsPerSecond: 32000 } : {});
    const chunks = [];
    const started = Date.now();
    rec.ondataavailable = (e) => e.data.size && chunks.push(e.data);
    const tick = setInterval(() => {
      const s = Math.floor((Date.now() - started) / 1000);
      time.textContent = `0:${String(Math.min(30, s)).padStart(2, "0")}`;
      if (s >= 30) rec.stop();
    }, 250);
    rec.onstop = async () => {
      clearInterval(tick);
      stream.getTracks().forEach((t) => t.stop());
      S.recording = null;
      btn.classList.remove("recording");
      btn.setAttribute("aria-label", "Record a voice note of up to 30 seconds");
      time.textContent = "";
      if (Date.now() - started < 700) return;
      const blob = new Blob(chunks, { type: rec.mimeType || "audio/webm" });
      await sendVoice(blob);
    };
    S.recording = rec;
    btn.classList.add("recording");
    btn.setAttribute("aria-label", "Stop and send the voice note");
    rec.start(250);
  });
}

async function sendVoice(blob) {
  const list = $("[data-messages]");
  $(".empty-chat", list)?.remove();
  const pending = bubble("owner", "Turning your voice note into words…", { voice: true });
  list.append(pending, typing());
  scrollChat();
  const ext = blob.type.includes("ogg") ? "ogg" : blob.type.includes("mp4") ? "m4a" : "webm";
  const form = new FormData();
  form.append("file", blob, `note.${ext}`);
  try {
    const out = await api("/api/voice", { method: "POST", form });
    pending.replaceWith(bubble("owner", out.transcript || "(no words heard)", { voice: true }));
    showAnswer(out);
    await loadState();
  } catch (e) {
    pending.remove();
    $$("[data-typing]", list).forEach((n) => n.remove());
    list.append(bubble("agent", e.message));
    scrollChat();
  }
}

/* ---------- close my month: the five steps ---------- */

function stepNode(n) { return $(`.step[data-step="${n}"]`); }
function setStep(n, cls) {
  const s = stepNode(n);
  if (!s) return;
  s.classList.remove("is-running", "is-done", "is-waiting", "is-failed");
  if (cls) s.classList.add(cls);
}
function resetSteps() {
  for (let n = 1; n <= 5; n++) { setStep(n, null); $(`[data-feed="${n}"]`).replaceChildren(); }
  S.found = [];
  S.owes = [];
  S.lastEvent = {};
}

function modelChips(list) {
  if (!list || !list.length) return null;
  return el("div", { class: "models" },
    list.map((m) => el("span", { class: "model", text: `Vultr ${m.model}, ${m.calls} ${m.calls === 1 ? "call" : "calls"}` })));
}

function evRow(date, title, sub, amount, cls = "") {
  return el("li", { class: `ev-row ${cls}` },
    el("span", { class: "ev-date", text: day(date) }),
    el("span", { class: "ev-what" }, el("b", { text: title }), sub ? el("span", { text: sub }) : null),
    el("span", { class: `ev-amt ${amount > 0 ? "amount-in" : ""}`, text: amount === null ? "" : rand(amount, true) }));
}

function renderFound(feed) {
  let box = $("[data-found]", feed);
  if (!box) {
    box = el("div", { "data-found": true });
    const info = $(".ev-done", feed);
    if (info) feed.insertBefore(box, info); else feed.append(box);
  }
  const shown = S.found.slice(0, 5);
  const rows = el("ul", { class: "ev-rows" }, shown.map((e) =>
    evRow(e.data.date, e.message, e.data.accounts ? e.data.accounts.join(" and ") : "", e.data.amount_cents)));
  box.replaceChildren(rows);
  if (S.found.length > 5) {
    box.append(el("p", { class: "ev-more" }, `and ${S.found.length - 5} more lines. `,
      el("button", { type: "button", onclick: () => { go({ f: "matched" }); $("[data-review]").scrollIntoView({ block: "start" }); } }, "See them all")));
  }
}

function renderOwes(feed) {
  let box = $("[data-owes]", feed);
  if (!box) { box = el("div", { "data-owes": true }); feed.append(box); }
  const rows = el("ul", { class: "ev-rows" }, S.owes.slice(0, 5).map((e) =>
    el("li", { class: "ev-row" },
      el("span", { class: "ev-date", text: e.data.late_cents ? `${e.data.oldest_days_late} days` : "on time" }),
      el("span", { class: "ev-what" }, el("b", { text: e.message }),
        el("span", { text: e.data.late_cents ? `${rand(e.data.late_cents)} past its due date` : "Nothing late yet" })),
      el("span", { class: "ev-amt", text: rand(e.data.open_cents) }))));
  box.replaceChildren(rows);
  if (S.owes.length > 5) {
    box.append(el("p", { class: "ev-more" }, `and ${S.owes.length - 5} more contractors. `,
      el("button", { type: "button", onclick: () => go({ v: "books", b: "owes" }) }, "See who owes")));
  }
}

function onEvent(e) {
  const feed = $(`[data-feed="${e.step || 1}"]`);
  if (!feed) return;
  const key = e.job_id || "x";
  if (e.id && S.lastEvent[key] && e.id <= S.lastEvent[key]) return;
  if (e.id) S.lastEvent[key] = e.id;
  const d = e.data || {};
  switch (e.kind) {
    case "start":
      setStep(e.step, "is-running");
      break;
    case "info":
      feed.append(el("p", { class: "ev", text: e.message }));
      if (d.service && d.service.startsWith("Vultr")) feed.append(el("div", { class: "models" }, el("span", { class: "model", text: d.service })));
      break;
    case "found":
      S.found.push(e);
      renderFound(feed);
      break;
    case "flag": {
      let rows = $(".ev-rows.flags", feed);
      if (!rows) { rows = el("ul", { class: "ev-rows flags" }); feed.append(rows); }
      rows.append(evRow(d.date, e.message, d.reason || "", d.amount_cents ?? null, "flag"));
      break;
    }
    case "owes":
      S.owes.push(e);
      renderOwes(feed);
      break;
    case "done":
      feed.append(el("p", { class: "ev ev-done", text: e.message }));
      { const chips = modelChips(d.vultr); if (chips) feed.append(chips); }
      setStep(e.step, "is-done");
      if (e.step === 5 && d.summary) renderSummary(d.summary);
      break;
    case "waiting":
      feed.append(el("p", { class: "ev ev-wait", text: e.message }));
      setStep(5, "is-waiting");
      break;
    case "error":
      feed.append(el("p", { class: "ev ev-error", text: e.message }));
      setStep(e.step || 1, "is-failed");
      break;
    default:
      break;
  }
}

async function replay(jobId) {
  const j = await api(`/api/jobs/${jobId}`);
  for (const e of j.events) onEvent({ ...e, job_id: jobId });
  return j;
}

function stream(jobId, onEnd) {
  if (S.stream) S.stream.close();
  const after = S.lastEvent[jobId] || 0;
  const es = new EventSource(`/api/jobs/${jobId}/stream?after=${after}`);
  S.stream = es;
  es.addEventListener("step", (m) => onEvent({ ...JSON.parse(m.data), job_id: jobId }));
  es.addEventListener("end", (m) => { es.close(); S.stream = null; onEnd(JSON.parse(m.data).status); });
  es.addEventListener("gone", () => { es.close(); S.stream = null; });
  es.onerror = () => {
    // The connection dropped (a phone went to sleep, say): read what was missed, then carry on.
    es.close();
    S.stream = null;
    setTimeout(async () => {
      try {
        const j = await replay(jobId);
        if (["queued", "running"].includes(j.job.status)) stream(jobId, onEnd); else onEnd(j.job.status);
      } catch { /* the copy ended */ }
    }, 1500);
  };
}

const CLOSE_LABEL = {
  idle: "Close my month", running: "Closing the month", waiting: "Waiting for your decisions",
  posting: "Posting your entries", done: "The month is closed", failed: "Try the close again",
};
function closeButton(state) {
  const b = $("[data-start-close]");
  b.querySelector("span").textContent = CLOSE_LABEL[state];
  b.disabled = !["idle", "failed"].includes(state);
}

async function watchClose(jobId, fresh = true) {
  S.closeId = jobId;
  closeButton("running");
  if (fresh) resetSteps();
  const j = await replay(jobId);
  S.closeStatus = j.job.status;
  const finish = async (status) => {
    S.closeStatus = status;
    await loadState();
    closeButton(status === "waiting" ? "waiting" : status === "failed" ? "failed" : status === "done" ? "done" : "running");
    if (status === "waiting") { $("[data-review]").hidden = false; renderReview(); }
    if (status === "done" && S.state.post) await watchPost(S.state.post.id);
  };
  if (["queued", "running"].includes(j.job.status)) {
    setStep(1, "is-running");
    stream(jobId, finish);
  } else {
    await finish(j.job.status);
  }
}

async function watchPost(postId) {
  S.postId = postId;
  closeButton("posting");
  const j = await replay(postId);
  const finish = async (status) => {
    closeButton(status === "done" ? "done" : "waiting");
    await loadState();
    if (status === "done") {
      $("[data-review]").hidden = true;
      if (j.job.summary && j.job.summary.paragraph) renderSummary(j.job.summary);
      else { const k = await api(`/api/jobs/${postId}`); if (k.job.summary) renderSummary(k.job.summary); }
    }
    if (status === "failed") { $("[data-post]").disabled = false; }
  };
  if (["queued", "running"].includes(j.job.status)) stream(postId, finish); else await finish(j.job.status);
}

function setupClose() {
  $("[data-start-close]").addEventListener("click", async (ev) => {
    const b = ev.currentTarget;
    b.disabled = true;
    try {
      const r = await api("/api/close", { method: "POST" });
      $("[data-summary]").hidden = true;
      await watchClose(r.job_id, !r.existing);
    } catch (e) { toast(e.message); b.disabled = false; }
  });
  $("[data-upload]").addEventListener("change", async (ev) => {
    const f = ev.target.files[0];
    ev.target.value = "";
    if (!f) return;
    if (f.size > 20 * 1024) { toast("The statement can be up to 20 KB."); return; }
    const form = new FormData();
    form.append("file", f, f.name);
    try {
      const r = await api("/api/statement", { method: "POST", form });
      if (r.same) { toast("That statement is already on file."); return; }
      toast(`Read ${r.lines} lines${r.balance_checked ? ", and the running balance adds up" : ""}.`);
      await loadState();
      resetSteps();
      $("[data-review]").hidden = true;
      $("[data-summary]").hidden = true;
      closeButton("idle");
    } catch (e) { toast(e.message); }
  });
  $("[data-post]").addEventListener("click", async (ev) => {
    const b = ev.currentTarget;
    b.disabled = true;
    try {
      const r = await api(`/api/jobs/${S.closeId}/post`, { method: "POST" });
      await watchPost(r.job_id);
      $("[data-steps]").scrollIntoView({ block: "start" });
    } catch (e) { toast(e.message); b.disabled = false; }
  });
  $$("[data-filters] [data-filter]").forEach((b) => b.addEventListener("click", () => go({ f: b.dataset.filter }, { replace: true })));
  $("[data-order]").addEventListener("change", (e) => go({ o: e.target.value }, { replace: true }));
  $("[data-more]").addEventListener("click", () => loadReview(true));
}

/* ---------- close my month: your decisions ---------- */

async function refreshCounts() {
  if (!S.closeId) return null;
  const c = await api(`/api/jobs/${S.closeId}/counts`);
  for (const k of ["flag", "matched", "suggested", "followup"]) $(`[data-count="${k}"]`).textContent = c[k];
  $('[data-count="decided"]').textContent = c.approved + c.rejected + c.posted;
  const waiting = c.flag + c.matched + c.suggested;
  $("[data-post-note]").textContent = c.approved
    ? `${c.approved} approved and ready to post.${waiting ? ` ${waiting} still waiting, and they stay out of the books.` : ""}`
    : "Approve at least one item to post it.";
  $("[data-post]").disabled = !c.approved || S.closeStatus !== "waiting";
  return c;
}

function renderReview() {
  if (!S.closeId || S.closeStatus !== "waiting") return;
  $("[data-review]").hidden = false;
  const p = params();
  const f = ["flag", "matched", "suggested", "followup", "decided"].includes(p.get("f")) ? p.get("f") : "flag";
  for (const b of $$("[data-filters] [data-filter]")) b.setAttribute("aria-selected", String(b.dataset.filter === f));
  $("[data-order]").value = p.get("o") === "amount" ? "amount" : "date";
  loadReview(false);
}

function reviewQuery(offset) {
  const p = params();
  const f = p.get("f") || "flag";
  const q = new URLSearchParams({ job_id: S.closeId, offset, limit: PAGE, order: p.get("o") === "amount" ? "amount" : "date" });
  if (f === "flag") { q.set("kind", "flag"); q.set("status", "pending"); }
  else if (f === "followup") { q.set("kind", "followup"); q.set("status", "pending"); }
  else if (f === "decided") { q.set("kind", "match,flag"); q.set("status", "decided"); }
  else { q.set("kind", "match"); q.set("status", "pending"); q.set("which", f); }
  return { f, q };
}

async function loadReview(more) {
  const list = $("[data-items]");
  const offset = more ? S.review.offset : 0;
  const { f, q } = reviewQuery(offset);
  let data;
  try {
    [data] = await Promise.all([api(`/api/proposals?${q}`), more ? null : refreshCounts()]);
  } catch (e) { toast(e.message); return; }
  if (!more) list.replaceChildren();
  S.accounts = { ...S.accounts, ...data.accounts };
  for (const p of data.items) list.append(el("li", {}, f === "followup" ? followupCard(p) : itemCard(p)));
  if (!data.items.length && !more) {
    const empty = {
      flag: "Nothing needs you. Every flagged line has been decided.",
      matched: "Every matched line has been decided.",
      suggested: "Every suggested account has been decided.",
      followup: "No contractor has a late invoice, so there is nothing to follow up.",
      decided: "Nothing decided yet.",
    }[f];
    list.append(el("li", { class: "items-empty", text: empty }));
  }
  S.review.offset = offset + data.items.length;
  S.review.total = data.total;
  $("[data-more]").hidden = S.review.offset >= data.total;
  $("[data-more]").textContent = `Show more (${data.total - S.review.offset} left)`;
  const bulk = $("[data-bulk]");
  if ((f === "matched" || f === "suggested") && data.total > 1) {
    bulk.hidden = false;
    bulk.textContent = f === "matched" ? `Approve all ${data.total} matched lines` : `Approve all ${data.total} suggested accounts`;
    bulk.onclick = async () => {
      bulk.disabled = true;
      try {
        const r = await api("/api/proposals/approve-all", { method: "POST", body: { job_id: S.closeId, which: f } });
        toast(`Approved ${r.approved}.`);
        loadReview(false);
      } catch (e) { toast(e.message); } finally { bulk.disabled = false; }
    };
  } else bulk.hidden = true;
}

function accountPicker(p) {
  const sel = el("select", { "aria-label": "Account for this line" });
  const isExpense = p.outcome === "expense" && p.entry && p.entry.length;
  const current = isExpense ? (p.entry.find((e) => e.account !== "1000") || {}).account : "";
  if (!current) sel.append(el("option", { value: "", text: "Pick the account" }));
  const seen = new Set();
  const add = (code) => {
    if (!code || seen.has(code) || code === "1000" || !S.accounts[code]) return;
    seen.add(code);
    sel.append(el("option", { value: code, text: `${S.accounts[code]}` }));
  };
  (p.choices || []).forEach(add);
  if (current) add(current);
  Object.keys(S.accounts).forEach(add);
  sel.value = current || "";
  sel.dataset.initial = current || "";
  return sel;
}

function itemCard(p) {
  const line = p.line || {};
  const card = el("div", { class: `card ${p.kind === "flag" ? "card-flag" : ""}` });
  const amt = p.amount_cents || 0;
  card.append(
    el("div", { class: "card-top" },
      el("p", { class: "card-title", text: p.title }),
      el("p", { class: `card-amount ${amt > 0 ? "amount-in" : ""}`, text: rand(amt, true) })),
    el("p", { class: "card-meta", text: `${day(line.date)} · ${line.description || ""}${line.reference ? ` · ${line.reference}` : ""}` }),
  );
  if (p.detail) card.append(el("p", { class: "card-reason", text: p.detail }));
  if (p.entry && p.entry.length) card.append(entryTable(p.entry));
  const canPick = p.status === "pending" && !(p.applies && p.applies.length) && (!p.entry || !p.entry.length || p.outcome === "expense");
  card.append(decisionBar(p, card, () => afterDecision(card), canPick ? accountPicker(p) : null));
  return card;
}

async function afterDecision(card) {
  await refreshCounts();
  const f = params().get("f") || "flag";
  if (f !== "decided") {
    const li = card.closest("li");
    setTimeout(() => {
      li.remove();
      S.review.offset = Math.max(0, S.review.offset - 1);
      if (!$("[data-items]").children.length) loadReview(false);
    }, 700);
  }
}

function followupCard(p) {
  const card = el("div", { class: "card" });
  const copy = el("button", { class: "btn btn-gold btn-sm", type: "button" }, svg(ICON.copy), "Copy the message");
  copy.querySelector("svg").setAttribute("width", "16");
  copy.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(p.text); toast("Copied. Paste it into your own chat."); }
    catch { toast("Your browser did not allow copying. Select the text and copy it."); }
  });
  card.append(
    el("div", { class: "card-top" },
      el("p", { class: "card-title", text: p.party }),
      el("p", { class: "card-amount", text: rand(p.late_cents || 0) })),
    el("p", { class: "card-meta", text: `Past its due date. The agent sends nothing: copy this into your own chat with ${p.contact || "them"}.` }),
    el("p", { class: "followup-text", text: p.text }),
    el("div", { class: "card-actions" }, copy),
  );
  return card;
}

/* ---------- the one-page summary ---------- */

function bars(items, cls = "") {
  const max = Math.max(1, ...items.map((i) => i.cents));
  const ul = el("ul", { class: "bars" });
  for (const i of items) {
    const fill = el("div", { class: `bar-fill ${cls}` });
    ul.append(el("li", { class: "bar" }, el("span", { text: i.label }), el("b", { text: rand(i.cents) }),
      el("div", { class: "bar-track" }, fill)));
    requestAnimationFrame(() => requestAnimationFrame(() => fill.style.setProperty("transform", `scaleX(${Math.max(0.02, i.cents / max)})`)));
  }
  return ul;
}

function renderSummary(s) {
  const box = $("[data-summary]");
  const f = s.figures;
  const fig = (label, cents) => el("div", { class: "sum-fig" }, el("p", { text: label }), el("strong", { text: rand(cents) }));
  const figs = [
    fig("Money in through the bank", f.money_in),
    fig("Money out through the bank", f.money_out),
    fig("Sales recorded", f.sales),
    fig("Contractors paid", f.collected),
    fig("Contractors owe", f.debtors),
    fig("Past its due date", f.debtors_late),
    fig("Owed to suppliers", f.creditors),
    fig(f.refunds_due ? "Refunds due to the shop" : "Bank account in the books", f.refunds_due || f.bank_books),
  ];
  const rec = s.reconciliation;
  const tick = (ok, text) => el("li", { class: ok ? "" : "open" }, svg(ok ? ICON.tick : ICON.dot), el("span", { text }));
  const checks = el("ul", { class: "checks" },
    tick(true, `The trial balance agrees: debits and credits are both ${rand(s.trial_balance.debit)}.`),
    rec.difference_cents === 0 && rec.unposted_lines === 0
      ? tick(true, `The bank account in the books matches the statement at ${rand(rec.statement_cents)}.`)
      : tick(false, `The books show ${rand(rec.books_cents)} in the bank against the statement's ${rand(rec.statement_cents)}, and ${rec.unposted_lines} ${rec.unposted_lines === 1 ? "line is" : "lines are"} not posted.`),
    tick(true, `${s.posted} entries were posted, each one balanced.`),
    ...(s.open_items || []).map((o) => tick(false, `${o.title}: ${o.status === "rejected" ? "set aside" : "still waiting"}.`)),
  );
  const paper = el("div", { class: "core sum-paper" },
    el("div", { class: "sum-head" },
      el("div", {},
        el("p", { class: "eyebrow" }, el("span", { class: "eyebrow-dot" }), `One-page summary, as at ${dayLong(s.as_at)}`),
        el("h3", { id: "summary-title", text: `${s.month} at ${s.shop}` })),
      el("div", { class: "sum-actions card-actions" },
        el("button", { class: "btn btn-quiet btn-sm", type: "button", onclick: () => window.print() }, "Print this page"),
        el("button", { class: "btn btn-quiet btn-sm", type: "button", onclick: () => go({ v: "books", b: "journal" }) }, "Open the journal"))),
    el("p", { class: "sum-para", text: s.paragraph }),
    el("div", { class: "sum-grid" }, figs),
    el("div", { class: "sum-cols" },
      el("section", {}, el("h4", { text: "Costs for the month" }),
        bars((s.costs || []).slice(0, 6).map((c) => ({ label: c.name, cents: c.cents })))),
      el("section", {}, el("h4", { text: "What contractors owe, by age" }),
        bars((s.ageing || []).map((a) => ({ label: a.bucket, cents: a.cents })), "gold")),
      el("section", {}, el("h4", { text: "Checks" }), checks)),
  );
  box.replaceChildren(el("div", { class: "shell" }, paper));
  box.hidden = false;
}

/* ---------- books ---------- */

const BOOKS = ["journal", "trial", "owes", "bank", "audit"];

function renderBook() {
  const p = params();
  const b = BOOKS.includes(p.get("b")) ? p.get("b") : "journal";
  for (const t of $$("[data-book-tabs] [data-book]")) t.setAttribute("aria-selected", String(t.dataset.book === b));
  const tools = $("[data-book-tools]");
  tools.replaceChildren();
  const select = (label, key, options) => {
    const s = el("select", { onchange: (e) => go({ [key]: e.target.value }, { replace: true }) },
      options.map(([v, t]) => el("option", { value: v, text: t })));
    s.value = p.get(key) || options[0][0];
    return el("label", { class: "sort" }, label, s);
  };
  if (b === "journal") {
    tools.append(
      select("Show", "src", [["", "Every entry"], ["close", "From the month-end close"], ["agent", "From your agent"], ["books", "October's invoices and bills"], ["opening", "Opening balances"]]),
      select("Order", "o", [["newest", "Newest first"], ["oldest", "Oldest first"]]));
  } else if (b === "bank") {
    tools.append(
      select("Show", "st", [["", "Every line"], ["in", "Money in"], ["out", "Money out"], ["flagged", "Flagged"], ["matched", "Matched"], ["suggested", "Account suggested"], ["posted", "Posted"], ["set_aside", "Set aside"], ["new", "Not yet read"]]),
      select("Order", "o", [["date", "By date"], ["amount", "Largest first"]]));
  }
  loadBook(false);
}

async function loadBook(more) {
  const p = params();
  const b = BOOKS.includes(p.get("b")) ? p.get("b") : "journal";
  const body = $("[data-book-body]");
  const offset = more ? S.book.offset : 0;
  let total = 0;
  let count = 0;
  try {
    if (b === "journal") {
      const q = new URLSearchParams({ offset, limit: PAGE, order: p.get("o") === "oldest" ? "oldest" : "newest" });
      if (p.get("src")) q.set("source", p.get("src"));
      const d = await api(`/api/journal?${q}`);
      if (!more) body.replaceChildren(el("p", { class: "hint", text: `Debits and credits across the books are both ${rand(d.trial_balance.debit)}.` }));
      for (const e of d.items) body.append(...journalRow(e));
      total = d.total; count = d.items.length;
    } else if (b === "trial") {
      const d = await api("/api/journal?limit=1");
      body.replaceChildren(trialBalance(d.trial_balance));
    } else if (b === "owes") {
      const d = await api("/api/debtors");
      body.replaceChildren(
        el("div", { class: "shell" }, el("div", { class: "core owes-card" },
          el("p", { class: "card-title", text: `Contractors owe ${rand(d.total)} on ${dayLong(d.as_at)}, and ${rand(d.late)} is past its due date.` }),
          bars(d.ageing.map((a) => ({ label: a.bucket, cents: a.cents })), "gold"))),
        ...d.customers.map(owesCard));
    } else if (b === "bank") {
      const q = new URLSearchParams({ offset, limit: PAGE, order: p.get("o") === "amount" ? "amount" : "date" });
      if (p.get("st")) q.set("status", p.get("st"));
      const d = await api(`/api/statement/lines?${q}`);
      if (!more) body.replaceChildren();
      for (const l of d.items) body.append(...bankRow(l));
      if (!d.items.length && !more) body.append(el("p", { class: "items-empty", text: "No lines to show." }));
      total = d.total; count = d.items.length;
    } else if (b === "audit") {
      const d = await api(`/api/audit?offset=${offset}&limit=${PAGE}`);
      if (!more) body.replaceChildren();
      for (const a of d.items) body.append(...auditRow(a));
      total = d.total; count = d.items.length;
    }
  } catch (e) { toast(e.message); return; }
  S.book.offset = offset + count;
  S.book.total = total;
  const m = $("[data-book-more]");
  m.hidden = !["journal", "bank", "audit"].includes(b) || S.book.offset >= total;
  m.textContent = `Show more (${total - S.book.offset} left)`;
}

function expandable(head, detail) {
  const btn = el("button", { class: "row-card", type: "button", "aria-expanded": "false" }, ...head, svg(ICON.chev, "chev"));
  const box = el("div", { class: "row-detail", hidden: true }, detail);
  btn.addEventListener("click", () => {
    const open = btn.getAttribute("aria-expanded") !== "true";
    btn.setAttribute("aria-expanded", String(open));
    box.hidden = !open;
  });
  return [btn, box];
}

const SOURCE = { opening: "Opening balances", books: "Invoices and bills", agent: "Your agent", close: "Month-end close" };

function journalRow(e) {
  const total = e.lines.reduce((s, l) => s + l.debit, 0);
  return expandable([
    el("span", { class: "when", text: day(e.entry_date) }),
    el("span", { class: "what" }, el("b", { text: e.memo }), el("span", { text: SOURCE[e.source] || e.source })),
    el("span", { class: "amt", text: rand(total) }),
  ], entryTable(e.lines.map((l) => ({ account: l.account, debit: l.debit, credit: l.credit }))));
}

function bankRow(l) {
  const label = { new: "Not yet read", matched: "Matched", suggested: "Account suggested", flagged: "Flagged", posted: "Posted", set_aside: "Set aside" }[l.status];
  return expandable([
    el("span", { class: "when", text: day(l.line_date) }),
    el("span", { class: "what" }, el("b", { text: l.description }), el("span", { text: [label, l.reference].filter(Boolean).join(" · ") })),
    el("span", { class: `amt ${l.amount_cents > 0 ? "amount-in" : ""}`, text: rand(l.amount_cents, true) }),
  ], el("p", { class: "card-reason", text: l.note || `Balance after this line: ${rand(l.balance_cents)}.` }));
}

const ACTIONS = {
  copy_created: "Your copy of the books was opened", proposed_record: "Your agent drafted a record", proposed_match: "Matched a bank line",
  proposed_flag: "Flagged a bank line", proposed_followup: "Drafted a follow-up", approved: "You approved", rejected: "You rejected",
  posted: "Posted to the journal", started_close: "You started the close", asked_to_post: "You asked to post",
  uploaded_statement: "You added a statement",
};
const ACTORS = { owner: "You", agent: "Your agent", system: "The books" };

function auditRow(a) {
  const d = a.detail || {};
  return expandable([
    el("span", { class: "when", text: clock(a.at) }),
    el("span", { class: "what" }, el("b", { text: ACTIONS[a.action] || a.action.replaceAll("_", " ") }),
      el("span", { text: `${ACTORS[a.actor] || a.actor}${d.title ? ` · ${d.title}` : ""}` })),
    el("span", { class: "amt", text: a.object_type ? `${a.object_type.replaceAll("_", " ")} ${a.object_id}` : "" }),
  ], el("p", { class: "card-reason", text: Object.keys(d).length ? Object.entries(d).map(([k, v]) => `${k}: ${typeof v === "object" ? JSON.stringify(v) : v}`).join(" · ") : "No more detail." }));
}

function trialBalance(tb) {
  const rows = tb.accounts.map((a) => {
    const net = a.debit - a.credit;
    return el("tr", {}, el("td", { text: a.code }), el("td", { text: a.name }),
      el("td", { text: net > 0 ? rand(net) : "" }), el("td", { text: net < 0 ? rand(-net) : "" }));
  });
  const dr = tb.accounts.reduce((s, a) => s + Math.max(0, a.debit - a.credit), 0);
  const cr = tb.accounts.reduce((s, a) => s + Math.max(0, a.credit - a.debit), 0);
  return el("div", { class: "shell" }, el("div", { class: "core tb-wrap" },
    el("table", { class: "tb" },
      el("thead", {}, el("tr", {}, el("th", { text: "Code" }), el("th", { text: "Account" }), el("th", { text: "Debit" }), el("th", { text: "Credit" }))),
      el("tbody", {}, rows),
      el("tfoot", {}, el("tr", {}, el("td", {}), el("td", { text: dr === cr ? "Balanced" : "Out of balance" }), el("td", { text: rand(dr) }), el("td", { text: rand(cr) }))))));
}

function owesCard(c) {
  return el("div", { class: "shell" }, el("div", { class: "core owes-card" },
    el("div", { class: "card-top" },
      el("p", { class: "card-title", text: c.name }),
      el("p", { class: "card-amount", text: rand(c.open_cents) })),
    el("p", { class: "card-meta", text: c.late_cents ? `${rand(c.late_cents)} past its due date. Ask for ${c.contact}.` : `Nothing late. Ask for ${c.contact}.` }),
    el("ul", { class: "inv-list" }, c.invoices.map((i) => el("li", {},
      el("span", { text: i.number }),
      el("span", { class: i.days_late > 0 ? "late" : "", text: i.days_late > 0 ? `${i.days_late} days late` : `due ${day(i.due_on)}` }),
      el("b", { text: rand(i.open_cents) }))))));
}

/* ---------- page furniture ---------- */

function setupFurniture() {
  for (const a of $$("a[data-view]")) {
    a.addEventListener("click", (e) => {
      if (e.metaKey || e.ctrlKey) return;
      e.preventDefault();
      const v = a.dataset.view;
      go({ v, f: null, b: null, o: null, src: null, st: null });
      const panel = $(`[data-panel="${v}"]`);
      if (panel && window.scrollY > panel.offsetTop) panel.scrollIntoView({ block: "start" });
    });
  }
  $$("[data-book-tabs] [data-book]").forEach((b) => b.addEventListener("click", () => go({ b: b.dataset.book, o: null, src: null, st: null }, { replace: true })));
  $("[data-book-more]").addEventListener("click", () => loadBook(true));
  $("[data-start-over]").addEventListener("click", () => startOver(true));
  window.addEventListener("popstate", route);

  const top = $("[data-to-top]");
  top.addEventListener("click", () => window.scrollTo({ top: 0 }));
  new IntersectionObserver(([e]) => (top.hidden = e.isIntersecting), { rootMargin: "200px 0px 0px 0px" }).observe($(".intro"));

  const reveal = new IntersectionObserver((entries) => {
    for (const e of entries) if (e.isIntersecting) { e.target.classList.add("in"); reveal.unobserve(e.target); }
  }, { threshold: 0.08 });
  $$(".reveal").forEach((n) => reveal.observe(n));

  // A scroll cue on lists of filters that run off a phone's screen.
  const cue = () => $$(".filters").forEach((f) => f.classList.toggle("scrolls", f.scrollWidth > f.clientWidth + 4));
  window.addEventListener("resize", cue);
  setTimeout(cue, 300);
}

async function boot() {
  setupFurniture();
  setupComposer();
  setupClose();
  try {
    if (!(await loadState({ quiet: true }))) {
      await openCopy();
      await loadState();
    }
    $("[data-banner]").hidden = true;
  } catch (e) {
    const b = $("[data-banner]");
    b.replaceChildren(el("span", { text: e.message }));
    b.hidden = false;
    route();
    return;
  }
  route();
  await loadChat();
  const s = S.state;
  if (s.close) {
    await watchClose(s.close.id);
  }
}

document.addEventListener("DOMContentLoaded", boot);
