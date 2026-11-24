"use strict";
const TOKEN = "__WATCHPOINT_TOKEN__";
const AUTO_SECONDS = 60;  // how often the page looks for targets whose interval has passed (not how often each is checked)
const $ = (id) => document.getElementById(id);
const state = {
  overview: null, selected: decodeURIComponent(location.hash.slice(1)) || null, detail: null,
  tags: new Set(), job: null, polling: false, known: null, tab: "targets", autoRun: false,
  starting: null, jobStart: 0,  // the names asked for, until the answer to /api/check arrives; when it was pressed
  picked: new Set(), lastPick: null, order: [],
  config: { hash: null, saved: "", targets: [] },
};

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v !== false && v != null) node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) if (c != null && c !== false) node.append(c instanceof Node ? c : String(c));
  return node;
}

function store(key, value) {
  if (PREFS) {  // the window keeps no browser storage; Python keeps these few settings beside the config
    if (value === undefined) return PREFS[key] ?? null;
    PREFS[key] = value;
    bridge.then(() => window.pywebview.api.set_pref(key, value)).catch(() => {});
    return null;
  }
  try {
    if (value === undefined) return localStorage.getItem(key);
    localStorage.setItem(key, value);
  } catch (e) { /* storage can be blocked; the page works without it */ }
  return null;
}

// In the watchpoint window there is no server: pywebview hands the same calls to Python directly.
const DESKTOP = "__WATCHPOINT_DESKTOP__" === "1";
const PREFS = /*prefs*/null/*prefs*/;
const bridge = DESKTOP && new Promise((ready) => {
  if (window.pywebview && window.pywebview.api && window.pywebview.api.call) ready();
  else window.addEventListener("pywebviewready", () => ready(), { once: true });
});

async function api(path, body) {
  if (DESKTOP) {
    await bridge;
    let r;
    try { r = await window.pywebview.api.call(path, body === undefined ? null : body); } catch (e) {
      throw new Error(`watchpoint の処理を呼べませんでした: ${(e && e.message) || e}`);
    }
    if (r.status >= 400) throw new Error((r.data && r.data.error) || `エラー ${r.status}`);
    return r.data;
  }
  const opts = { headers: { "X-Watchpoint-Token": TOKEN } };
  if (body !== undefined) {
    opts.method = "POST";
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  let res;
  try { res = await fetch(path, opts); } catch (e) {
    throw new Error("watchpoint gui につながりません（起動したターミナルで止まっていないか確かめてください）");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `エラー ${res.status}`);
  return data;
}

let toastTimer;
function toast(msg, isError = false) {
  const t = $("toast");
  t.textContent = msg;
  t.className = isError ? "err" : "";
  t.style.display = "block";
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.style.display = "none"), isError ? 9000 : 4000);
}

async function copyText(text) {
  if (DESKTOP) {
    await bridge;
    return window.pywebview.api.copy(text).catch(() => false);
  }
  try { await navigator.clipboard.writeText(text); return true; } catch (e) {
    const ta = el("textarea", { style: "position:fixed;opacity:0" });
    ta.value = text;
    document.body.append(ta);
    ta.select();
    const ok = document.execCommand("copy");
    ta.remove();
    return ok;
  }
}

function badgeClass(status) {
  return { "変更あり": "changed", "取得失敗": "failed", "一時的に取得できず": "retrying", "変更なし": "ok",
           "条件外の変化": "ok", "初回": "first", "チェック中": "running" }[status] || "";
}
const short = (stamp) => (stamp ? stamp.slice(5, 16).replace("-", "/").replace("T", " ") : "-");
// "11/23 15:25" -> "15:25" when it is today: the list is read many times a day
const todayMD = () => { const d = new Date(); return `${String(d.getMonth() + 1).padStart(2, "0")}/${String(d.getDate()).padStart(2, "0")}`; };
const brief = (s) => (s && s.startsWith(`${todayMD()} `) ? s.slice(6) : s);
const needsLook = (t) => t.pending || t.status === "取得失敗" || (t.alerts && t.alerts.length > 0);
const jobActive = () => !!(state.starting || (state.job && state.job.active));
const runningNow = () => (state.job && state.job.active ? state.job.running : []);  // none while starting

// ------------------------------------------------------------------ overview and notifications

async function loadOverview() {
  try {
    state.overview = await api("/api/overview");
  } catch (e) {
    if (/設定ファイル/.test(e.message)) renderBrokenConfig(e.message);
    else toast(e.message, true);
    return;
  }
  state.job = state.overview.job;
  notifyNew();
  renderCounts();
  renderTags();
  renderList();
  renderProgress();
  if (jobActive()) pollJob();
}

// A config file with a mistake must not leave a blank page: say what is wrong and where to fix it.
function renderBrokenConfig(message) {
  $("list").replaceChildren();
  $("detail").replaceChildren(el("div", { class: "welcome" },
    el("h2", { text: "設定ファイルに誤りがあります" }),
    el("div", { class: "error", text: message }),
    el("button", { class: "primary big", text: "設定を開いて直す", onclick: () => showTab("config") })));
}

// The first visit: nothing is watched yet. One clear way forward instead of an empty list.
function renderWelcome() {
  $("detail").replaceChildren(el("div", { class: "welcome" },
    el("h2", { text: "まだ何も見張っていません" }),
    el("p", { text: "共有フォルダの Excel や Word、Web ページなどを見張り、前に見たときから変わったところだけを知らせます。" }),
    el("ol", {},
      el("li", { text: "見張るファイルやページの場所を入れる（「ファイル…」で選んでもよい）" }),
      el("li", { text: "中身を見ながら、見張る列やセルをクリックで選ぶ" }),
      el("li", { text: "「見張りを始める」で、いまの内容を基準にする。これから変わったところが、ここと通知に出る" })),
    el("button", { class: "primary big", text: "最初の見張りを追加", onclick: openAddTarget }),
    el("p", { class: "sub" }, "設定ファイルを直接書くときは ", el("a", { href: "#", onclick: (e) => { e.preventDefault(); showTab("config"); }, text: "設定" }), " から。")));
}

function notifyNew() {
  // Remember which copy each pending change was seen for; tell the user about the ones that are new.
  const now = new Map(state.overview.targets.filter(needsLook).map((t) => [t.name, `${t.status}:${t.snapshot}:${(t.alerts || []).join("|")}`]));
  if (state.known) {
    const fresh = [...now].filter(([n, v]) => state.known.get(n) !== v).map(([n]) => n);
    if (fresh.length && $("auto").checked && DESKTOP) {
      const items = state.overview.targets.filter((t) => fresh.includes(t.name));
      bridge.then(() => window.pywebview.api.notify(`watchpoint: ${fresh.length} 件`,
        items.map((t) => `${t.name}: ${t.status}`).join("\n"), fresh[0])).catch(() => {});
    } else if (fresh.length && $("auto").checked && "Notification" in window && Notification.permission === "granted") {
      const items = state.overview.targets.filter((t) => fresh.includes(t.name));
      const n = new Notification(`watchpoint: ${fresh.length} 件`, {
        body: items.map((t) => `${t.name}: ${t.status}`).join("\n"), tag: "watchpoint",
      });
      n.onclick = () => { window.focus(); showTab("targets"); select(fresh[0]); n.close(); };
    }
  }
  state.known = now;
}

// The unattended runs (Task Scheduler): quiet when fine, loud when they have stopped.
function scheduleBadge(s) {
  if (!s || s.state === "none") return null;
  if (s.state === "ok") return el("span", { class: "badge", title: s.text, text: `自動チェック ${s.every} 分おき` });
  return el("span", { class: "badge failed", title: s.text, text: s.state === "late" ? "自動チェックが止まっています" : "自動チェックが失敗" });
}

function hoursBadge(h) {
  if (!h || h.now) return null;
  return el("span", { class: "badge", title: `自動チェックは ${h.label} の間だけ。手で押すチェックはいつでもできる`,
    text: `時間外（次は ${h.next} から）` });
}

function renderCounts() {
  const ts = state.overview.targets;
  const pending = ts.filter((t) => t.pending).length;
  const failed = ts.filter((t) => t.status === "取得失敗").length;
  const alerted = ts.filter((t) => t.alerts && t.alerts.length).length;
  $("counts").replaceChildren(...[
    pending ? el("span", { class: "badge changed", text: `未確認の変更 ${pending}` }) : null,
    failed ? el("span", { class: "badge failed", text: `取得失敗 ${failed}` }) : null,
    alerted ? el("span", { class: "badge alert", title: "決めたルール（更新の期限・値の決まり）に合わない見張り", text: `要注意 ${alerted}` }) : null,
    !pending && !failed && !alerted && ts.length ? el("span", { class: "badge ok", text: "すべて確認済み" }) : null,
    scheduleBadge(state.overview.schedule),
    hoursBadge(state.overview.check_hours),
  ].filter(Boolean));
  document.title = (pending + failed ? `(${pending + failed}) ` : "") + "watchpoint";
}

function renderTags() {
  const all = [...new Set(state.overview.targets.flatMap((t) => t.tags))].sort();
  $("tags").replaceChildren(...all.map((tag) => el("button", {
    class: "chip", "aria-pressed": String(state.tags.has(tag)), text: tag,
    onclick: () => { state.tags.has(tag) ? state.tags.delete(tag) : state.tags.add(tag); renderTags(); renderList(); },
  })));
}

function shownTargets() {
  const q = $("search").value.trim();
  const only = $("pending-only").checked;
  return state.overview.targets.filter((t) =>
    (!q || t.name.includes(q)) &&
    (!state.tags.size || t.tags.some((x) => state.tags.has(x))) &&
    (!only || needsLook(t)));
}

function renderList() {
  if (!state.overview) return;
  const shown = shownTargets();
  const running = new Set(runningNow());
  const list = $("list");
  if (!shown.length) {
    list.replaceChildren(el("li", { class: "empty", text: state.overview.targets.length ? "当てはまる見張りがありません" : "まだ見張りがありません" }));
  } else {
    const ordered = [...shown.filter((t) => t.enabled), ...shown.filter((t) => !t.enabled)];
    state.order = ordered.map((t) => t.name);
    list.replaceChildren(...ordered.map((t) => {
      const status = running.has(t.name) ? "チェック中" : t.status;
      const picked = state.picked.has(t.name);
      const box = el("input", {
        type: "checkbox", class: "pick", "aria-label": `「${t.name}」を選ぶ`, disabled: !t.enabled,
        title: t.enabled ? "選ぶ（Ctrl+クリック、Shift+クリックで範囲、x キーでも）" : "停止中の見張りは選べません",
      });
      box.checked = picked;
      box.addEventListener("click", (e) => { e.stopPropagation(); togglePick(t.name, e.shiftKey); });
      const onclick = (e) => {
        if ((e.ctrlKey || e.metaKey || e.shiftKey) && t.enabled) { e.preventDefault(); togglePick(t.name, e.shiftKey); return; }
        select(t.name);
      };
      const current = t.name === (state.selected ?? ordered[0].name);
      const onkeydown = (e) => { if (e.key === "Enter") { e.preventDefault(); select(t.name); } };
      // a plain list (each row holds a checkbox, which an ARIA option may not): the selected row is the
      // one tab stop in it and says so with aria-current
      return el("li", { "aria-current": String(t.name === state.selected),
        tabindex: current ? "0" : "-1", class: [t.enabled ? "" : "disabled", picked ? "picked" : ""].join(" ").trim() || null, onclick, onkeydown },
        box,
        el("div", { class: "item" },
          el("div", { class: "row1" },
            t.pending && status !== "変更あり" ? el("span", { class: "dot", role: "img", title: "前に取った未確認の変更あり", "aria-label": "未確認の変更あり" }) : null,
            el("span", { class: "name", text: t.name + (t.enabled ? "" : "（停止中）") }),
            t.alerts && t.alerts.length ? el("span", { class: "badge alert", title: t.alerts.join("\n"), text: `要注意 ${t.alerts.length}` }) : null,
            el("span", { class: `badge ${badgeClass(status)}`, text: status })),
          el("div", { class: "row2" },
            el("span", { text: t.kind }),
            el("span", { title: `最終チェック ${short(t.last_checked)}`, text: t.last_checked ? `チェック ${brief(short(t.last_checked))}` : "未チェック" }),
            t.next ? el("span", { title: `次回 ${t.next}`, text: `次 ${brief(t.next)}` }) : null)));
    }));
  }
  renderCheckButtons(shown);
  renderSelBar(shown);
  const pendingShown = shown.filter((t) => t.pending);
  const btn = $("ack-shown");
  btn.hidden = pendingShown.length < 2 || state.picked.size > 0;  // the selection bar has its own
  btn.textContent = `表示中の未確認 ${pendingShown.length} 件を確認済みに`;
  btn.onclick = () => {
    if (confirm(`次の ${pendingShown.length} 件を確認済みにします。\n\n${pendingShown.map((t) => t.name).join("\n")}`)) {
      runAck(pendingShown.map((t) => t.name), "");
    }
  };
}

// ------------------------------------------------------------------ picking several

// Picked with the box on each row (or Ctrl+click, Shift+click for a range, x for the open one).
// Picks survive narrowing the list, so a few from different tags can be gathered; the bar says how
// many of them are out of view. Stopped targets cannot be picked.
function togglePick(name, range = false) {
  const enabled = new Set(state.overview.targets.filter((t) => t.enabled).map((t) => t.name));
  if (!enabled.has(name)) return;
  if (range && state.lastPick && state.order.includes(state.lastPick)) {
    const [a, b] = [state.order.indexOf(state.lastPick), state.order.indexOf(name)].sort((x, y) => x - y);
    for (const n of state.order.slice(a, b + 1)) if (enabled.has(n)) state.picked.add(n);
  } else if (state.picked.has(name)) {
    state.picked.delete(name);
  } else {
    state.picked.add(name);
  }
  state.lastPick = name;
  renderList();
}

function clearPicks() {
  state.picked.clear();
  state.lastPick = null;
  renderList();
}

function pickedTargets() {
  return state.overview.targets.filter((t) => state.picked.has(t.name));
}

function renderSelBar(shown) {
  const bar = $("selbar");
  // a target removed from the config, or stopped since, drops out of the selection
  for (const n of [...state.picked]) {
    const t = state.overview.targets.find((x) => x.name === n);
    if (!t || !t.enabled) state.picked.delete(n);
  }
  const picked = pickedTargets();
  bar.hidden = !picked.length;
  if (!picked.length) { bar.replaceChildren(); return; }
  const names = picked.map((t) => t.name);
  const hidden = names.filter((n) => !shown.some((t) => t.name === n)).length;
  const pending = picked.filter((t) => t.pending);
  bar.replaceChildren(...[
    el("div", { class: "count" },
      el("span", {}, `${names.length} 件を選択中`, hidden ? el("small", { text: `（うち ${hidden} 件は一覧の表示外）` }) : null),
      el("button", { class: "mini", text: "選択を解除", title: "Esc でも解除できる", onclick: clearPicks })),
    el("button", { class: "primary", text: "チェック", disabled: jobActive(), onclick: () => runCheck(names) }),
    pending.length ? el("button", {
      text: `確認済みにする（${pending.length}）`,
      onclick: () => {
        if (confirm(`次の ${pending.length} 件を確認済みにします。\n\n${pending.map((t) => t.name).join("\n")}`)) {
          runAck(pending.map((t) => t.name), "");
        }
      },
    }) : null,
    el("button", { text: "AI 用にコピー", onclick: () => copyForAI(names) }),
  ].filter(Boolean));  // replaceChildren would show a null as the text "null"
}

// The header's buttons work on the picked targets, else on what the narrowed list shows, else on all.
// Stopped targets are never checked.
function filtering() {
  return !!($("search").value.trim() || state.tags.size || $("pending-only").checked);
}

function checkScope() {
  if (!state.overview) return null;
  if (state.picked.size) return [...state.picked];
  if (!filtering()) return null;
  return shownTargets().filter((t) => t.enabled).map((t) => t.name);
}

function renderCheckButtons(shown) {
  const all = $("check-all"), due = $("check-due");
  if (state.picked.size) {
    all.textContent = `選択した ${state.picked.size} 件をチェック`;
    all.title = "選んだ見張りだけをチェックする";
    due.title = "選んだ見張りのうち、チェックの間隔が過ぎたものだけをチェックする";
    $("copy-ai").title = "選んだ見張りの、未確認の変更と取得失敗を、AI に貼れる形でコピーする";
    all.disabled = due.disabled = false;
    return;
  }
  if (!filtering()) {
    all.textContent = "すべてチェック";
    all.title = "停止中のものを除いて、すべての見張りをチェックする";
    due.title = "チェックの間隔が過ぎた見張りだけをチェックする";
    all.disabled = due.disabled = false;
    $("copy-ai").title = "未確認の変更と取得失敗を、Copilot などの AI に貼れる形でコピーする（check -f md -C と同じ）";
    return;
  }
  const n = shown.filter((t) => t.enabled).length;
  all.textContent = `表示中の ${n} 件をチェック`;
  all.title = "左の一覧に出ている見張りだけをチェックする（停止中は除く）";
  due.title = "左の一覧に出ている見張りのうち、チェックの間隔が過ぎたものだけをチェックする";
  all.disabled = due.disabled = n === 0;
  $("copy-ai").title = "左の一覧に出ている見張りの、未確認の変更と取得失敗を、AI に貼れる形でコピーする";
}

// ------------------------------------------------------------------ check with progress

async function runCheck(names, due = false, via = "page") {
  if (jobActive()) { toast("いまチェック中です"); return; }
  // Answer the click at once: the buttons and the panel change before the request goes out.
  state.starting = names || (state.overview ? state.overview.targets.map((t) => t.name) : []);
  state.jobStart = Date.now();
  renderProgress();
  try {
    state.job = await api("/api/check", { names, due, via });
  } catch (e) {
    state.starting = null;
    renderProgress();
    state.autoRun = false; toast(e.message, true); return;
  }
  state.starting = null;
  renderProgress();
  renderList();
  pollJob();
}

async function pollJob() {
  if (state.polling) return;
  state.polling = true;
  if (!state.jobStart) state.jobStart = Date.now();  // a run already going when the page opened
  const ticker = setInterval(renderProgress, 1000);  // the seconds keep moving while nothing else does
  let wait = 150;  // a quick check ends before the first look; a slow one is looked at less often
  try {
    while (true) {
      await new Promise((r) => setTimeout(r, wait));
      wait = Math.min(wait * 2, 600);
      let r;
      try { r = await api("/api/job"); } catch (e) { toast(e.message, true); break; }
      state.job = r.job;
      renderProgress();
      renderList();
      if (!jobActive()) break;
    }
  } finally {
    clearInterval(ticker);
    state.polling = false;
    state.jobStart = 0;
  }
  await finishJob();
}

async function finishJob() {
  const j = state.job;
  const quiet = state.autoRun;
  state.autoRun = false;
  await loadOverview();
  if (state.selected && state.tab === "targets") await select(state.selected, true);
  if (!j) return;
  if (j.error) { toast(j.error, true); return; }
  if (quiet) return;
  // the first check of a target just added: its own panel says how it went
  if (j.results.length === 1 && j.results[0].name === state.justAdded && j.results[0].status !== "取得失敗") return;
  const failed = j.results.filter((x) => x.status === "取得失敗");
  let msg = `チェックしました: 変更あり ${j.changed} 件 / 取得失敗 ${j.failed} 件 / 全 ${j.results.length} 件`;
  if (j.due && j.results.every((x) => x.status === "まだ時間前")) {
    const h = state.overview && state.overview.check_hours;
    msg = h && !h.now ? `チェックする時間帯（${h.label}）の外なので、チェックしませんでした。「すべてチェック」ならいつでもできます`
      : "時間が来た見張りはありませんでした";
  }
  if (failed.length) msg += "\n" + failed.map((x) => `${x.name}: ${x.message}`).join("\n");
  toast(msg, failed.length > 0);
}

function renderProgress() {
  const p = $("progress");
  const j = state.starting ? null : state.job;
  const active = jobActive();
  for (const b of document.querySelectorAll("button.check, button[data-check]")) b.disabled = active;
  p.classList.toggle("on", active);
  renderCheckState();
  if (!active) { p.textContent = ""; p.title = ""; return; }
  const secs = elapsed();
  if (!j) { p.textContent = "チェックを始めています…"; p.title = ""; return; }
  const now = j.running.length ? `: ${j.running.join("、")}` : "";
  p.textContent = `チェック中 ${j.results.length}/${j.total}${secs}${now}`;
  p.title = p.textContent;
}

const elapsed = () => {
  const s = state.jobStart ? Math.floor((Date.now() - state.jobStart) / 1000) : 0;
  return s >= 1 ? `（${s} 秒）` : "";
};

// Where the person pressed: the open target says whether it is waiting, being fetched, and for how long.
function renderCheckState() {
  const box = document.getElementById("check-state");
  const d = state.detail;
  if (!box || !d) return;
  const j = state.starting ? null : state.job;
  const names = state.starting || (jobActive() && j ? j.names || [] : []);
  const mine = jobActive() && names.includes(d.name) && !(j && j.results.some((r) => r.name === d.name));
  const btn = document.querySelector("button[data-check]");
  if (btn) btn.textContent = mine ? "チェック中…" : "今すぐチェック";
  box.hidden = !mine;
  if (!mine) return;
  const running = j && j.running.includes(d.name);
  const office = /Office|SharePoint|Excel・セル/.test(d.kind || "") && /^https?:/.test(d.source || "");
  const slow = Date.now() - state.jobStart > 3000;
  // waiting only while others are being fetched; before anything runs, the check is just starting
  const waiting = j && !running && j.running.length > 0;
  box.textContent = running ? `取ってきています${elapsed()}` + (office && slow ? "。見えない Excel でファイルを開いています（SharePoint は十数秒かかることがあります）" : "")
    : waiting ? `順番を待っています${elapsed()}（ほかの見張りをチェックしています）`
    : `チェックを始めています…${elapsed()}`;
}

function setupAuto() {
  const box = $("auto");
  box.checked = store("wp-auto") === "1";
  box.addEventListener("change", async () => {
    store("wp-auto", box.checked ? "1" : "0");
    if (box.checked && !DESKTOP && "Notification" in window && Notification.permission === "default") {
      await Notification.requestPermission();
    }
    if (box.checked) toast(`開いている間、各見張りをそれぞれの間隔（既定 ${everyLabel(defaultEvery()).replace("ごと", "")}）でチェックします`);
  });
  setInterval(() => {
    if (!box.checked || jobActive()) return;
    state.autoRun = true;
    runCheck(null, true, "page-auto");
  }, AUTO_SECONDS * 1000);
}

// ------------------------------------------------------------------ detail

// Changes per record as a table, laid out from the data compare returns (never parsed back out of the
// text lines, where a value holding " / " or " → " cannot be told from the separators). Text diffs keep
// their lines, with the words that changed inside a line marked, as delta does.
const TABLE_STEP = 300;  // table rows drawn at first; the rest on asking
const CELL_WORDS_FROM = 8;  // characters a cell's value needs before its changed words are marked

function renderChanges(ch) {
  const out = ch.keyed && ch.entries && ch.entries.length ? renderRecordTable(ch) : [renderDiffLines(ch)];
  if (ch.truncated && !(ch.keyed && ch.entries && ch.entries.length)) {
    out.push(el("p", { class: "sub", text: "差分が長いので途中まで。全部はターミナルの watchpoint diff で。" }));
  }
  return out;
}

function renderDiffLines(ch) {
  const pre = el("pre", { class: "changes" });
  const lines = ch.text ? ch.text.split("\n") : [];
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (ch.keyed) {
      const cls = line.startsWith("## ") ? "l-head" : line.startsWith("追加 ") ? "l-add" : line.startsWith("削除 ") ? "l-del"
        : line.startsWith("変更 ") ? "l-mod" : line.startsWith("列　") ? "l-col" : "";
      pre.append(el("span", { class: cls, text: line || " " }));
      continue;
    }
    if (line.startsWith("+++") || line.startsWith("---")) { pre.append(el("span", { class: "l-file", text: line })); continue; }
    if (line.startsWith("@@")) { pre.append(el("span", { class: "l-hunk", text: line })); continue; }
    if (line.startsWith("-")) {
      // a run of removed lines and the run of added lines after it: pair them up and mark the words
      let j = i;
      while (j < lines.length && lines[j].startsWith("-") && !lines[j].startsWith("---")) j++;
      let k = j;
      while (k < lines.length && lines[k].startsWith("+") && !lines[k].startsWith("+++")) k++;
      const removed = lines.slice(i, j), added = lines.slice(j, k);
      removed.forEach((r, n) => pre.append(diffLine("l-del", r, added[n], "del")));
      added.forEach((a, n) => pre.append(diffLine("l-add", a, removed[n], "add")));
      i = k - 1;
      continue;
    }
    pre.append(el("span", { class: line.startsWith("+") ? "l-add" : "", text: line || " " }));
  }
  return pre;
}

// One diff line with the words that differ from its partner (the line it replaced, or that replaced it) marked.
function diffLine(cls, line, partner, side) {
  const span = el("span", { class: cls });
  if (partner === undefined) { span.textContent = line; return span; }
  const marks = wordChanges(line.slice(1), partner.slice(1));
  if (!marks) { span.textContent = line; return span; }
  span.append(line[0]);
  for (const [text, changed] of marks) span.append(changed ? el("mark", { class: `w-${side}`, text }) : text);
  return span;
}

const WORD_TOKEN = /\d+(?:[.,:/]\d+)*|[A-Za-z_][A-Za-z0-9_]*|\s+|[\s\S]/gu;

// [text, changed] pieces of a, against b, by the longest common run of tokens; null when the lines share
// too little to say which words changed (then the whole line reads as changed, as before).
function wordChanges(a, b) {
  const x = a.match(WORD_TOKEN) || [], y = b.match(WORD_TOKEN) || [];
  if (x.length * y.length > 250000) return null;  // long lines: not worth the time
  const dp = Array.from({ length: x.length + 1 }, () => new Uint16Array(y.length + 1));
  for (let i = x.length - 1; i >= 0; i--) {
    for (let j = y.length - 1; j >= 0; j--) dp[i][j] = x[i] === y[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
  }
  const same = dp[0][0];
  if (same * 2 < Math.max(x.length, y.length) * 0.6) return null;
  const out = [];
  let i = 0, j = 0;
  const push = (t, c) => { if (out.length && out[out.length - 1][1] === c) out[out.length - 1][0] += t; else out.push([t, c]); };
  while (i < x.length) {
    if (j < y.length && x[i] === y[j]) { push(x[i], false); i++; j++; }
    else if (j < y.length && dp[i][j + 1] >= dp[i + 1][j]) j++;
    else { push(x[i], true); i++; }
  }
  return out;
}

function renderRecordTable(ch) {
  const entries = ch.entries;
  const sections = new Set(entries.map((e) => e.section));
  const counts = { added: 0, changed: 0, removed: 0, columns: 0 };
  for (const e of entries) counts[e.kind]++;
  const fields = [...new Set(entries.flatMap((e) => (e.cells || []).map((c) => c[0])))];
  // the sheet's own layout needs each changed row whole; changes saved before rows were kept show as a list
  const sheetable = entries.some((e) => e.kind === "changed" && e.row);
  const view = { kind: "all", field: "", shown: TABLE_STEP, allColumns: false,
    layout: sheetable && store("wp-chg-layout") !== "list" ? "sheet" : "list" };
  const KIND_WORD = { added: "追加", changed: "変更", removed: "削除", columns: "列" };

  const bar = el("div", { class: "chg-bar", role: "toolbar", "aria-label": "変更の絞り込み" });
  const tableWrap = el("div", { class: "table-wrap" });
  const more = el("div");
  const note = el("p", { class: "sheet-note" });
  const shown =(e) => (view.kind === "all" || e.kind === view.kind)
    && (!view.field || (e.kind === "changed" && e.cells.some((c) => c[0] === view.field)));
  const draw = () => {
    for (const b of bar.querySelectorAll("button[data-kind]")) b.setAttribute("aria-pressed", String(b.dataset.kind === view.kind));
    for (const b of bar.querySelectorAll("button[data-layout]")) b.setAttribute("aria-pressed", String(b.dataset.layout === view.layout));
    let total = 0;
    const extra = [];
    if (view.layout === "sheet") {
      const drawn = sheetTables(entries.filter(shown), view, sections.size > 1);
      tableWrap.replaceChildren(...drawn.nodes);
      total = drawn.total;
      // said above the table, where a reader looks for a column, not below hundreds of rows
      if (drawn.hidden) {
        extra.push(el("span", { class: "sub", text: view.allColumns ? "すべての列を出しています。" : `変わっていない ${drawn.hidden} 列を畳んでいます。` }), " ",
          el("button", { class: "mini", text: view.allColumns ? "畳む" : "すべての列を出す",
            onclick: () => { view.allColumns = !view.allColumns; draw(); } }));
      }
    } else {
      const rows = [];
      for (const e of entries) {
        if (!shown(e)) continue;
        if (e.kind === "changed") {
          const cells = e.cells.filter((c) => !view.field || c[0] === view.field);
          cells.forEach((c, n) => rows.push(recordRow(e, n === 0 ? cells.length : 0, c[0], c[1], c[2], sections.size > 1)));
        } else {
          rows.push(recordRow(e, 1, null, null, null, sections.size > 1));
        }
      }
      const head = [sections.size > 1 ? "区画" : null, "行", "項目", "前", "後"].filter(Boolean);
      tableWrap.replaceChildren(el("table", { class: "chg" },
        el("thead", {}, el("tr", {}, head.map((h) => el("th", { text: h })))),
        el("tbody", {}, rows.slice(0, view.shown))));
      total = rows.length;
    }
    // nothing to add when every row is shown: replaceChildren(null) would write the word "null"
    const below = total > view.shown
      ? [el("button", { class: "mini", text: `すべて表示（残り ${total - view.shown} 行）`, onclick: () => { view.shown = total; draw(); } })]
      : total ? [] : [el("p", { class: "sub", text: "この絞り込みに当たる変更はありません。" })];
    note.replaceChildren(...extra);
    note.hidden = !extra.length;
    more.replaceChildren(...below);
  };
  if (sheetable) {
    const layout = el("span", { class: "seg", role: "group", "aria-label": "表の見せ方" },
      [["sheet", "元の表の形"], ["list", "変わった項目だけ"]].map(([key, text]) => el("button", { class: "chip", "data-layout": key,
        "aria-pressed": "false", text, onclick: () => { view.layout = key; view.shown = TABLE_STEP; store("wp-chg-layout", key); draw(); } })));
    bar.append(layout);
  }
  bar.append(
    ...[["all", `すべて ${entries.length}`], ...Object.entries(counts).filter(([, n]) => n).map(([k, n]) => [k, `${KIND_WORD[k]} ${n}`])]
      .map(([kind, text]) => el("button", { class: "chip", "data-kind": kind, "aria-pressed": "false", text,
        onclick: () => { view.kind = kind; view.shown = TABLE_STEP; draw(); } })));
  if (fields.length > 1) {
    const pick = el("select", { "aria-label": "項目で絞る" }, el("option", { value: "", text: "すべての項目" }),
      fields.map((f) => el("option", { value: f, text: f })));
    pick.addEventListener("change", () => { view.field = pick.value; view.shown = TABLE_STEP; draw(); });
    bar.append(pick);
  }
  const asText = el("details", { class: "advanced" }, el("summary", { text: "テキストで見る" }), renderDiffLines(ch));
  draw();
  const out = [bar, note, tableWrap, more, asText];
  if (ch.entries_total > entries.length) {
    out.push(el("p", { class: "sub", text: `変わった行が ${ch.entries_total} 行あり、最初の ${entries.length} 行まで出しています。全部はターミナルの watchpoint diff で。` }));
  }
  return out;
}

// The changed rows laid out as the sheet has them: its key first, then its columns in their order, each
// changed cell holding what it was over what it is, the cells around it there to say what changed (which
// question got this answer). Only rows that changed are drawn; a wide sheet keeps the columns that changed
// and the few on its left that usually name the row, the rest one click away.
const SHEET_ALL_COLUMNS = 10;  // up to this many columns, all are shown
const SHEET_LEFT_COLUMNS = 3;  // beyond it, the unchanged columns kept on the left
// A value this short (a date, a name, a status) is kept on one line: squeezed by many columns it would
// otherwise break at its hyphen or between characters; the table scrolls sideways instead.
const SHORT_VALUE = 16;
const KEY_OF = (e) => { const i = (e.key || "").indexOf("="); return i < 0 ? ["", e.key || ""] : [e.key.slice(0, i), e.key.slice(i + 1)]; };

function sheetTables(entries, view, withSection) {
  const groups = new Map();
  for (const e of entries) {
    if (!groups.has(e.section)) groups.set(e.section, []);
    groups.get(e.section).push(e);
  }
  const nodes = [];
  let total = 0, hidden = 0;
  for (const [section, list] of groups) {
    if (withSection) nodes.push(el("h4", { class: "sheet-name", text: section || "（区画なし）" }));
    for (const e of list.filter((x) => x.kind === "columns")) {
      const parts = [e.added && e.added.length ? `追加 ${e.added.join("、")}` : "", e.removed && e.removed.length ? `削除 ${e.removed.join("、")}` : ""];
      nodes.push(el("p", { class: "sheet-note" }, el("span", { class: "badge columns", text: "列" }), " ", parts.filter(Boolean).join(" / ")));
    }
    const rows = list.filter((x) => x.kind !== "columns");
    if (!rows.length) continue;
    // the sheet's column order, merged from the rows (a changed row carries all of them; an added or
    // removed one only those holding a value)
    const cols = [];
    for (const e of rows) {
      let at = 0;
      for (const [name] of e.row || e.values || []) {
        const i = cols.indexOf(name);
        if (i < 0) cols.splice(at++, 0, name); else at = i + 1;
      }
    }
    const changedCols = new Set(rows.flatMap((e) => (e.kind === "changed" ? e.cells.map((c) => c[0]) : [])));
    for (const name of changedCols) if (!cols.includes(name)) cols.push(name);
    // the columns asked to be read beside a change (show) are never folded away
    const besides = new Set(rows.flatMap((e) => (e.context || []).map((c) => c[0])));
    let visible = cols;
    if (cols.length > SHEET_ALL_COLUMNS) {
      const left = new Set(cols.filter((c) => !changedCols.has(c)).slice(0, SHEET_LEFT_COLUMNS));
      const narrow = cols.filter((c) => changedCols.has(c) || left.has(c) || besides.has(c));
      hidden = Math.max(hidden, cols.length - narrow.length);
      if (!view.allColumns) visible = narrow;
    }
    const keyName = KEY_OF(rows[0])[0] || "行";
    const body = [];
    for (const e of rows) {
      if (total++ >= view.shown) continue;
      body.push(sheetRow(e, visible));
    }
    if (!body.length) continue;
    nodes.push(el("table", { class: "chg sheet" },
      el("thead", {}, el("tr", {}, el("th", { class: "key", text: keyName }),
        visible.map((c) => el("th", { class: changedCols.has(c) ? "col-changed" : null, text: c })))),
      el("tbody", {}, body)));
  }
  return { nodes, total, hidden };
}

function sheetRow(e, cols) {
  const tr = el("tr", { class: `k-${e.kind}` });
  tr.append(el("th", { class: "key", scope: "row" },
    el("span", { class: `badge ${e.kind}`, text: { added: "追加", changed: "変更", removed: "削除" }[e.kind] }), " ", KEY_OF(e)[1]));
  const now = new Map(e.row || e.values || []);
  const changed = new Map((e.cells || []).map(([f, before, after]) => [f, [before, after]]));
  for (const c of cols) {
    if (changed.has(c)) { tr.append(changedCell(...changed.get(c))); continue; }
    const v = now.get(c) || "";
    // an unchanged cell is there to say what the row is: a few lines of it, the rest on pointing at it
    const cls = ["ctx", e.kind === "changed" ? "" : e.kind, v.length <= SHORT_VALUE ? "short" : ""].filter(Boolean).join(" ");
    tr.append(el("td", { class: cls, title: v.length > 60 ? v : null },
      v ? el("div", { class: "clamp", text: v }) : null));
  }
  return tr;
}

// What a cell was, struck out, over what it is now; a longer text marks only the words that changed.
function changedCell(before, after) {
  const long = before && after && before.length >= CELL_WORDS_FROM && after.length >= CELL_WORDS_FROM;
  const side = (v, cls, marks, mark) => el("div", { class: !v ? `${cls} empty-value` : marks ? `${cls} words` : cls },
    !v ? "(空)" : marks ? marks.map(([text, changed]) => (changed ? el("mark", { class: `w-${mark}`, text }) : text)) : v);
  const marks = long ? [wordChanges(before, after), wordChanges(after, before)] : [null, null];
  const short = (before || "").length <= SHORT_VALUE && (after || "").length <= SHORT_VALUE;
  return el("td", { class: short ? "cell-changed short" : "cell-changed" },
    side(before, "was", marks[0], "del"), side(after, "now", marks[1], "add"));
}

function recordRow(e, span, field, before, after, withSection) {
  const tr = el("tr", { class: `k-${e.kind}` });
  if (span) {
    const rs = span > 1 ? String(span) : null;
    if (withSection) tr.append(el("td", { class: "sub", rowspan: rs, text: e.section }));
    tr.append(el("td", { class: "nowrap", rowspan: rs }, el("span", { class: `badge ${e.kind}`, text: { added: "追加", changed: "変更", removed: "削除", columns: "列" }[e.kind] }),
      " ", e.key || "",
      // the show columns: read beside the change (who answers it, by when), not compared
      e.context && e.context.length ? el("div", { class: "beside", text: e.context.map(([f, v]) => `${f}: ${v}`).join("　") }) : null));
  }
  const value = (v, cls) => v ? el("td", { class: cls, text: v }) : el("td", { class: `${cls} empty-value`, text: "(空)" });
  if (e.kind === "changed") {
    // both sides hold a longer text: mark only the words that changed, as in a text diff. A short value
    // (未回答 → 回答済) reads better whole than as single marked characters.
    const long = before && after && before.length >= CELL_WORDS_FROM && after.length >= CELL_WORDS_FROM;
    const marked = long ? [wordChanges(before, after), wordChanges(after, before)] : [null, null];
    const cell = (v, cls, marks, side) => marks
      ? el("td", { class: `${cls} words` }, marks.map(([text, changed]) => (changed ? el("mark", { class: `w-${side}`, text }) : text)))
      : value(v, cls);
    tr.append(el("td", { class: "nowrap", text: field }), cell(before, "before", marked[0], "del"), cell(after, "after", marked[1], "add"));
  } else if (e.kind === "columns") {
    tr.append(el("td", { text: "列" }), el("td", { class: "before", text: (e.removed || []).join("、") }),
      el("td", { class: "after", text: (e.added || []).join("、") }));
  } else {
    const summary = (e.values || []).filter((v) => v[1]).map((v) => `${v[0]}: ${v[1]}`).join("　");
    tr.append(el("td", { class: "sub", text: "（行全体）" }),
      e.kind === "removed" ? el("td", { class: "before", text: summary }) : el("td", {}),
      e.kind === "added" ? el("td", { class: "after", text: summary }) : el("td", {}));
  }
  return tr;
}

async function select(name, keepScroll = false) {
  if (state.justAdded && state.justAdded !== name) state.justAdded = null;
  state.selected = name;
  history.replaceState(null, "", "#" + encodeURIComponent(name));
  renderList();
  const cur = document.querySelector('#list li[aria-current="true"]');
  if (cur && !keepScroll) cur.scrollIntoView({ block: "nearest" });
  let d;
  try { d = await api(`/api/target?name=${encodeURIComponent(name)}`); } catch (e) {
    $("detail").replaceChildren(el("p", { class: "empty", text: e.message }));
    return;
  }
  if (state.selected !== name) return;
  const scroll = $("detail").scrollTop;
  const draft = ($("ack-note") || {}).value;
  renderDetail(d);
  if (keepScroll) $("detail").scrollTop = scroll;
  if (keepScroll && draft && $("ack-note")) $("ack-note").value = draft;
}

// Right after "見張りを始める": what happened, and how it keeps going without the person.
function renderAdded(d) {
  const sched = state.overview && state.overview.schedule;
  const unattended = sched && sched.state === "ok";
  const checking = runningNow().includes(d.name);
  return el("div", { class: "just-added" },
    el("p", { text: checking ? "見張りを始めました。いまの内容を取ってきています…" : d.status === "取得失敗"
      ? "設定は保存しましたが、取ってこられませんでした。上のエラーを見て、場所やアクセス権を確かめてください。"
      : "見張りを始めました。いまの内容を基準にしたので、これから変わったところを知らせます。" }),
    unattended ? el("p", { class: "sub", text: `自動チェック（${sched.every} 分おき）で、これからこの見張りも見ます。` })
      : el("div", { class: "actions" },
          el("span", { text: "自動でチェックするには:" }),
          $("auto").checked ? el("span", { text: "この画面を開いている間は、自動でチェックしています。" })
            : el("button", { class: "mini", text: "開いている間、自動でチェックする", onclick: () => { $("auto").checked = true; $("auto").dispatchEvent(new Event("change")); renderDetail(d); } }),
          el("span", { class: "sub", text: "閉じていても見張るなら、ターミナルで watchpoint schedule install" })));
}

// A command runs only once the person has read it and allowed it (it runs with their rights).
function allowBox(d) {
  const allowIt = async () => {
    const ok = confirm(`次のコマンドを、あなたの権限で動かします。\n\n${d.source}\n\n`
      + "自分で書いた（または中身の分かる）コマンドだけを許可してください。許可したあとでコマンドが1文字でも変わると、もう一度聞きます。");
    if (!ok) return;
    try {
      await api("/api/commands/allow", { name: d.name, command: d.command });
      toast("許可しました。チェックします");
      await runCheck([d.name]);
    } catch (e) { toast(e.message, true); }
  };
  return el("div", { class: "error" },
    el("p", { style: "margin:0 0 8px", text: "この見張りは、上のコマンドを動かして、その出力を見張ります。まだ許可していないので、動かしていません。" }),
    el("button", { class: "primary", text: "このコマンドを許可する", onclick: allowIt }));
}

// ---- rules and deadlines: warnings the person sets up, decided by watchpoint on every check

const DEADLINES = [["", "なし"], ["平日 9:00", "平日 9:00 までに"], ["毎日 9:00", "毎日 9:00 までに"],
  ["毎週金 17:00", "毎週金曜 17:00 までに"], ["毎週月 12:00", "毎週月曜 12:00 までに"],
  ["毎月25日 17:00", "毎月25日 17:00 までに"], ["3d", "3 日に一度は"], ["7d", "7 日に一度は"]];

function ruleWords(r) {
  const parts = [];
  if (r.required) parts.push("空にしない");
  if (r.one_of) parts.push(`${r.one_of.join("・")} のどれか`);
  if (r.pattern) parts.push(`形が ${r.pattern}`);
  const when = Object.entries(r.when || {}).map(([c, v]) => `${c} が ${Array.isArray(v) ? v.join("・") : v} の行で、`).join("");
  return `${when}${r.field} は ${parts.join("、")}` + (r.message ? `（警告:「${r.message}」）` : "");
}

async function saveRule(path, body, done) {
  try {
    await api(path, body);
    toast(done);
    await loadOverview();
    await select(state.selected, true);
  } catch (e) { toast(e.message, true); }
}

function renderRules(d) {
  const out = [el("h3", { text: "ルールと期限" }),
    el("p", { class: "sub", style: "margin-top:0", text: "決めておくと、チェックのたびに watchpoint が確かめて、合わなければ「要注意」として出します（AI は使いません）。" })];

  // the deadline: pick a usual one or write one
  const pick = el("select", { "aria-label": "更新の期限" }, DEADLINES.map(([v, t]) => el("option", { value: v, text: t })), el("option", { value: "*", text: "自分で書く…" }));
  const own = el("input", { type: "text", placeholder: "例: 毎週水 15:00 / 平日 18:00 / 毎月1日 12:00 / 2d", hidden: true, style: "width:22em" });
  const known = DEADLINES.some(([v]) => v === (d.expect_update || ""));
  pick.value = known ? (d.expect_update || "") : "*";
  if (!known) { own.hidden = false; own.value = d.expect_update || ""; }
  pick.addEventListener("change", () => { own.hidden = pick.value !== "*"; if (!own.hidden) own.focus(); });
  const saveDeadline = el("button", { class: "mini", text: "期限を保存", onclick: () => {
    const when = pick.value === "*" ? own.value.trim() : pick.value;
    saveRule("/api/rules/expect", { name: d.name, when: when || null }, when ? `更新の期限を「${when}」にしました` : "更新の期限をなくしました");
  } });
  out.push(el("div", { class: "rules" },
    el("div", { class: "row" }, el("b", { text: "更新の期限" }), pick, own, saveDeadline),
    el("p", { class: "stat", text: "この時刻までに変更が見つからなければ「更新されていません」と出します（チェックの間隔ごとに確かめるので、少し遅れて出ることがあります）。" })));

  if (!d.can_have_rules) {
    out.push(el("p", { class: "sub", text: "値のルールは、行ごとに見る見張り（Excel・CSV・JSON で行を見分ける列があるもの）と、決まったセルの見張りに付けられます。" }));
    return out;
  }
  // the rules already set
  const list = el("div", { class: "rules" }, el("b", { text: "値のルール" }),
    d.rules.length ? d.rules.map((r, i) => el("div", { class: "rule" }, el("span", { text: ruleWords(r) }),
      el("button", { class: "mini", text: "外す", onclick: () => {
        if (confirm(`このルールを外します。\n\n${ruleWords(r)}`)) saveRule("/api/rules/remove", { name: d.name, index: i }, "ルールを外しました");
      } })))
      : el("p", { class: "stat", text: "まだありません。" }));
  out.push(list);
  out.push(ruleForm(d));
  return out;
}

// Adding a rule: start from the data (how filled each column is, which values it takes), and show what
// the rule would flag right now before saving it.
function ruleForm(d) {
  const box = el("details", { class: "advanced rules" }, el("summary", { text: "＋ ルールを足す" }));
  let stats = null;
  const field = el("select", { "aria-label": "列・項目" });
  const fieldOwn = el("input", { type: "text", placeholder: "列・項目の名前", hidden: true });
  const stat = el("p", { class: "stat" });
  const kind = el("select", { "aria-label": "ルール" },
    el("option", { value: "required", text: "空にしない" }), el("option", { value: "one_of", text: "次のどれかにする" }),
    el("option", { value: "pattern", text: "決まった形にする（正規表現）" }));
  const values = el("input", { type: "text", placeholder: "カンマで区切る（例: 未回答, 回答済）", style: "width:24em" });
  const whenCol = el("select", { "aria-label": "この行だけ（列）" }, el("option", { value: "", text: "すべての行" }));
  const whenVal = el("input", { type: "text", placeholder: "値（例: 回答済）", hidden: true });
  const message = el("input", { type: "text", placeholder: "警告の文言（空なら自動。例: 承認漏れ）", style: "width:24em" });
  const tryOut = el("div", { class: "try sub" });
  const add = el("button", { class: "primary", text: "このルールを足す" });

  const fieldName = () => (field.value === "*" ? fieldOwn.value.trim() : field.value);
  const current = () => {
    const r = { field: fieldName() };
    if (kind.value === "required") r.required = true;
    if (kind.value === "one_of") r.one_of = values.value.split(/[,、]/).map((v) => v.trim()).filter(Boolean);
    if (kind.value === "pattern") r.pattern = values.value.trim();
    if (whenCol.value && whenVal.value.trim()) r.when = { [whenCol.value]: whenVal.value.trim() };
    if (message.value.trim()) r.message = message.value.trim();
    return r;
  };
  const describe = () => {
    const f = stats && stats.find((x) => x.field === fieldName());
    if (!f) { stat.textContent = fieldName() ? "（いまのデータにはない名前です。空のセルや、これから増える列ならこのままで構いません）" : ""; return; }
    const vals = f.values.map((v) => `${v.value}（${v.count}）`).join("・") + (f.distinct > f.values.length ? ` ほか ${f.distinct - f.values.length} 種類` : "");
    stat.textContent = `${f.rows} 行中 ${f.empty} 行が空${f.empty ? `（${f.empty_in.join("・")}${f.empty > f.empty_in.length ? " …" : ""}）` : ""}／値: ${vals || "なし"}`;
    if (kind.value === "one_of" && !values.value.trim() && f.distinct && f.distinct <= 8) values.value = f.values.map((v) => v.value).join(", ");
  };
  let timer = null;
  const preview = () => {
    clearTimeout(timer);
    values.hidden = kind.value === "required";
    whenVal.hidden = !whenCol.value;
    describe();
    timer = setTimeout(async () => {
      const r = current();
      if (!r.field) { tryOut.textContent = ""; return; }
      try {
        const res = await api("/api/rules/try", { name: d.name, rule: r });
        tryOut.replaceChildren(res.alerts.length
          ? el("span", {}, `このルールだと、いま ${res.alerts.length} 件が引っかかります: `, el("b", { text: res.alerts.slice(0, 3).join(" ／ ") }), res.alerts.length > 3 ? " …" : "")
          : el("span", { text: "このルールなら、いまは引っかかるものはありません。" }));
      } catch (e) { tryOut.textContent = e.message; }
    }, 350);
  };
  [field, kind, whenCol].forEach((x) => x.addEventListener("change", preview));
  [fieldOwn, values, whenVal, message].forEach((x) => x.addEventListener("input", preview));
  field.addEventListener("change", () => { fieldOwn.hidden = field.value !== "*"; if (kind.value === "one_of") values.value = ""; preview(); });
  add.addEventListener("click", () => saveRule("/api/rules/add", { name: d.name, rule: current() }, "ルールを足しました。次のチェックから確かめます"));

  box.addEventListener("toggle", async () => {
    if (!box.open || stats) return;
    try { stats = (await api(`/api/fields?name=${encodeURIComponent(d.name)}`)).fields; } catch (e) { stats = []; }
    field.replaceChildren(...stats.map((f) => el("option", { value: f.field, text: f.empty ? `${f.field}（空 ${f.empty}）` : f.field })),
      el("option", { value: "*", text: "名前を書く…" }));
    whenCol.replaceChildren(el("option", { value: "", text: "すべての行" }), ...stats.map((f) => el("option", { value: f.field, text: `${f.field} が…の行だけ` })));
    if (!stats.length) { field.value = "*"; fieldOwn.hidden = false; }
    // start where a rule is most likely wanted: a column with some empty rows
    const hint = stats.find((f) => f.empty > 0 && f.empty < f.rows);
    if (hint) field.value = hint.field;
    preview();
  });
  box.append(
    el("div", { class: "row" }, field, fieldOwn, kind, values),
    stat,
    el("div", { class: "row" }, whenCol, whenVal),
    el("div", { class: "row" }, message),
    tryOut,
    el("div", { class: "row" }, add));
  return box;
}

function everyRow(d) {
  const box = el("select", { class: "inline", "aria-label": "チェックの間隔" }, everyOptions(d.every_is_default ? "" : d.every));
  box.value = d.every_is_default ? "" : d.every;
  box.addEventListener("change", async () => {
    box.disabled = true;
    try {
      await api("/api/targets/every", { name: d.name, every: box.value || null });
      toast(`「${d.name}」の間隔を「${everyLabel(box.value || defaultEvery())}」にしました（設定ファイルに保存）`);
      await loadOverview();
      await select(d.name, true);
    } catch (e) {
      toast(e.message, true);
      box.value = d.every_is_default ? "" : d.every;
      box.disabled = false;
    }
  });
  return el("div", {}, "チェックの間隔: ", box,
    d.next ? `　次回 ${d.next}` : "",
    d.check_hours ? el("span", { class: "sub", text: `　チェックする時間帯: ${d.check_hours.label}` }) : null);
}

// A Confluence page can show an attached Excel inside it; its contents are not in the page's text, so a
// change in the table would go unseen. Offer to watch the files themselves, beside the page.
function embeddedHint(d) {
  const files = d.embedded;
  const name = uniqueTargetName(`${d.name}（埋め込みの表）`);
  const add = el("button", { class: "mini", text: "中身を見張る見張りを足す", onclick: async () => {
    add.disabled = true;
    try {
      await api("/api/targets/add", { item: { name, source: d.source, attachments: files } });
      toast(`「${name}」を足しました。初回のチェックで基準を取ります`);
      state.justAdded = name;
      await loadOverview();
      await select(name);
    } catch (e) { toast(e.message, true); add.disabled = false; }
  } });
  return el("div", { class: "notice" },
    el("p", { style: "margin:0 0 6px", text: `このページは添付ファイルを埋め込んで表示しています（${files.join("、")}）。埋め込みの中身はページの文字に含まれないので、この見張りでは表の中が変わっても分かりません。` }),
    el("div", { class: "row" }, add, el("span", { class: "sub", text: `「${name}」として、添付ファイルの中身を比べる見張りを足します（ページの見張りはそのまま）` })));
}

function uniqueTargetName(base) {
  const names = new Set(((state.overview && state.overview.targets) || []).map((t) => t.name));
  let n = base;
  for (let i = 2; names.has(n); i++) n = `${base} ${i}`;
  return n;
}

function renderDetail(d) {
  state.detail = d;
  const note = el("input", { type: "text", id: "ack-note", placeholder: "メモ（任意。ログに残る）", "aria-label": "確認のメモ" });
  note.addEventListener("keydown", (e) => { if (e.key === "Enter") runAck([d.name], note.value); });
  const meta = d.meta && d.meta.title
    ? el("div", { text: `ページ: ${d.meta.title}（版 ${d.meta.version ?? "-"}、${d.meta.by || "-"}、${d.meta.when || "-"}）` }) : null;
  // The name, the state and what to do with it stay at the top while a long change is read below.
  const head = el("div", { class: "detail-head" },
    el("div", { class: "title-row" },
      el("h2", { text: d.name }), el("span", { class: `badge ${badgeClass(d.status)}`, text: d.status })),
    el("div", { id: "check-state", class: "check-state", role: "status", hidden: true }),
    el("div", { class: "actions" },
      el("button", { "data-check": "1", onclick: () => runCheck([d.name]), text: "今すぐチェック" }),
      d.pending ? note : null,
      d.pending ? el("button", { class: "primary", text: "確認済みにする", onclick: () => runAck([d.name], note.value) }) : null));
  const parts = [
    head,
    el("div", { class: "meta" },
      el("div", {}, `${d.kind}　最終チェック ${short(d.last_checked)}`, d.tags.length ? `　タグ: ${d.tags.join(", ")}` : ""),
      el("div", {}, d.command ? "コマンド: " : "元の場所: ", el("code", { text: d.source }), " ",
        d.place ? el("button", { class: "mini", text: d.place.label, title: d.place.where, onclick: () => openPlace(d.name) })
          : d.command ? null : el("span", { class: "sub", title: "SharePoint のファイルを直接開くと、元のファイルを編集できる状態で開いてしまうため", text: "（開く場所は、設定の link: で決められます）" })),
      d.enabled ? everyRow(d) : null,
      meta),
    d.command && !d.command_allowed ? allowBox(d) : d.error ? el("div", { class: "error", text: d.error }) : null,
    d.alerts && d.alerts.length ? el("div", { class: "alerts" },
      el("b", { text: `要注意 ${d.alerts.length} 件（決めたルールに合わない）` }),
      el("ul", {}, d.alerts.map((a) => el("li", { text: a })))) : null,
    state.justAdded === d.name && !d.pending ? renderAdded(d) : null,
    d.embedded && d.embedded.length ? embeddedHint(d) : null,
  ];
  // what changed comes before how an AI should read it: it is what the person opened this for
  if (d.changes) {
    parts.push(el("div", { class: "actions" },
      el("h3", { text: d.changes.title, style: "margin:0" }), el("span", { class: "spacer" }),
      el("button", { class: "mini", text: "全画面で見る", title: "変更だけを窓いっぱいに出す（f キー）", onclick: () => openFocus(d) }),
      d.changes.pending ? el("button", { class: "mini", text: "この変更を AI 用にコピー", onclick: () => copyForAI([d.name]) }) : null));
    if (d.changes.message) parts.push(el("p", { class: "sub", text: d.changes.message }));
    parts.push(...renderChanges(d.changes));
  } else {
    const why = d.status === "初回" ? "いまの内容を基準にしました。これから変わったところが、ここに出ます。"
      : d.has_copy ? "まだ変更は見つかっていません。"
      : d.error ? "まだ一度も取得できていません。上のエラーを直してから「今すぐチェック」を押してください。"
      : "まだチェックしていません。「今すぐチェック」を押すと、いまの内容を基準にします。";
    parts.push(el("h3", { text: "変更" }), el("p", { class: "sub", text: why }));
  }
  if (d.points || d.notify_when) {
    parts.push(el("h3", { text: "AI への指示" }), el("div", { class: "card" },
      d.points ? el("div", { class: "points" }, el("b", { text: "読むポイント" }), d.points) : null,
      d.notify_when ? el("div", { class: "points" }, el("b", { text: "知らせる条件" }), d.notify_when) : null));
  }
  if (d.enabled) parts.push(...renderRules(d));
  parts.push(el("h3", { text: "履歴" }));
  if (d.history.length) {
    parts.push(el("div", { class: "table-wrap" }, el("table", {},
      el("thead", {}, el("tr", {}, ["#", "取得日時", "経路", "状態", "見つかった変更", "メモ", ""].map((h) => el("th", { text: h })))),
      el("tbody", {}, d.history.map((g, i) => el("tr", {},
        el("td", { class: "num", text: g.number }),
        el("td", { class: "nowrap", text: g.when }),
        el("td", { class: "nowrap sub", text: g.via || "-" }),
        el("td", { class: "nowrap", text: g.marks.join("・") }),
        el("td", { title: g.summary }, el("div", { class: "clamp2", text: g.summary })),
        el("td", { text: g.note }),
        el("td", { style: "white-space:nowrap" },
          i + 1 < d.history.length ? el("button", { class: "mini", text: "前と比べる",
            onclick: () => showDiff(d.name, g.number + 1, g.number) }) : null, " ",
          el("button", { class: "mini", text: "テキスト", onclick: () => showText(d.name, g.number, g.when) }))))))));
    if (d.history.length > 2) {
      const opts = () => d.history.map((g) => el("option", { value: g.number, text: `${g.number}（${g.when}）` }));
      const from = el("select", { "aria-label": "比べる元" }, opts());
      const to = el("select", { "aria-label": "比べる先" }, opts());
      from.value = String(d.history.length);
      to.value = "1";
      parts.push(el("div", { class: "compare" }, "履歴", from, "から", to, "へ",
        el("button", { class: "mini", text: "比べる", onclick: () => showDiff(d.name, +from.value, +to.value) })));
    }
  } else {
    parts.push(el("p", { class: "sub", text: "まだ履歴がありません。" }));
  }
  parts.push(el("h3", { text: "その他" }), el("div", { class: "actions" },
    el("button", { text: "いまの内容をテキストで見る", onclick: () => showPreview(d.name) }),
    d.has_copy ? el("button", { text: "保存したコピーを開く", title: "最後に取ってきたコピーを、読み取り専用で開きます。元のファイルは開きません", onclick: () => openCopy(d.name, false) }) : null,
    d.has_copy ? el("button", { text: "コピーのテキストを開く", onclick: () => openCopy(d.name, true) }) : null,
    el("button", { text: "設定ファイルで編集", onclick: () => openInConfig(d.name) }),
    el("span", { class: "spacer" }),
    el("button", { class: "danger", text: "この見張りを削除…", onclick: () => confirmRemove(d) })));
  $("detail").replaceChildren(...parts.filter(Boolean));
  renderProgress();
}

// Removing a target: the config keeps its previous version as .bak, and what was fetched stays unless
// the person asks for it to go too (a target added back under the same name picks it up again).
function confirmRemove(d) {
  const keep = el("input", { type: "radio", name: "remove-history", value: "keep", checked: true });
  const drop = el("input", { type: "radio", name: "remove-history", value: "drop" });
  const go = el("button", { class: "danger", text: "削除する", onclick: async () => {
    go.disabled = true;
    try {
      const r = await api("/api/targets/remove", { name: d.name, history: drop.checked ? "drop" : "keep" });
      closeDialog(true);
      toast(`「${d.name}」を削除しました（前の設定は .bak に残しています${r.history_dropped ? "。取った履歴も消しました" : ""}）`);
      state.selected = null;
      history.replaceState(null, "", "#");
      await loadOverview();
      const first = state.overview && state.overview.targets.find((t) => t.enabled);
      if (first) select(first.name); else renderWelcome();
    } catch (e) { toast(e.message, true); go.disabled = false; }
  } });
  openDialog(`「${d.name}」を削除`, [
    el("p", { style: "margin-top:0", text: "設定ファイルからこの見張りを消します。前の設定は .bak に残ります。" }),
    d.pending ? el("div", { class: "error", text: "まだ確認していない変更があります。" }) : null,
    el("label", { class: "choice" }, keep, " 取った履歴は残す（同じ名前で足し直すと、続きから見られる）"),
    el("label", { class: "choice" }, drop, ` 取った履歴も消す（${d.history.length} 件。元に戻せません）`),
  ].filter(Boolean), null, { actions: [go] });
}

// ------------------------------------------------------------------ dialogs and small actions

let dialogGuard = null;  // returns true while the dialog holds input that closing would throw away

function openDialog(title, body, copyable = null, opts = {}) {
  $("dialog-title").textContent = title;
  $("dialog-body").replaceChildren(...body);
  const copy = $("dialog-copy");
  copy.hidden = copyable == null;
  copy.onclick = async () => toast((await copyText(copyable)) ? "コピーしました" : "コピーできませんでした");
  $("dialog").classList.toggle("wide", !!opts.wide);
  $("dialog").classList.toggle("full", !!opts.full);
  $("dialog-actions").replaceChildren(...(opts.actions || []));
  dialogGuard = opts.guard || null;
  if (!$("dialog").open) $("dialog").showModal();
}

// A large change gets the whole window: the change alone, its filters, and acknowledging it.
function openFocus(d) {
  const ch = d.changes;
  if (!ch) return;
  const actions = ch.pending ? [el("button", { class: "primary", text: "確認済みにする", onclick: () => {
    closeDialog(true);
    runAck([d.name], ($("ack-note") || {}).value);
  } })] : [];
  const body = [ch.message ? el("p", { class: "sub", style: "margin-top:0", text: ch.message }) : null, ...renderChanges(ch)];
  openDialog(`${d.name}　${ch.title}`, body.filter(Boolean), ch.text, { full: true, actions });
  // the keyboard starts on the change, not on 確認済みにする: Enter right after opening must not
  // acknowledge what has not been read, and the arrow and page keys scroll the change from here
  const view = $("dialog-body");
  view.tabIndex = -1;
  view.focus({ preventScroll: true });
}

function closeDialog(force = false) {
  if (!force && dialogGuard && dialogGuard() && !confirm("入れた内容を捨てて閉じます")) return;
  dialogGuard = null;
  $("dialog").close();
}

async function showDiff(name, a, b) {
  try {
    const ch = await api(`/api/diff?name=${encodeURIComponent(name)}&from=${a}&to=${b}`);
    const full = el("button", { class: "mini", text: "全画面で見る", onclick: () => {
      const on = $("dialog").classList.toggle("full");
      full.textContent = on ? "元の大きさに戻す" : "全画面で見る";
    } });
    openDialog(`${name}　${ch.title}`, [el("p", { class: "sub", text: ch.message }),
      ...(ch.text ? renderChanges(ch) : [el("p", { text: "違いはありません。" })])], ch.text, { wide: true, actions: ch.text ? [full] : [] });
  } catch (e) { toast(e.message, true); }
}

async function showText(name, gen, when) {
  try {
    const r = await api(`/api/text?name=${encodeURIComponent(name)}&gen=${gen}`);
    openDialog(`${name}　履歴 #${gen}（${when}）のテキスト`, [el("pre", { class: "changes" }, el("span", { text: r.text || "(空)" }))], r.text);
  } catch (e) { toast(e.message, true); }
}

async function showPreview(name, draft) {
  const title = `${name}　いまの内容${draft ? "（保存前の設定）" : ""}`;
  openDialog(title, [el("p", { class: "sub", text: "取ってきています…（保存も比較もしません）" })]);
  try {
    const r = await api("/api/preview", draft ? { name, text: draft } : { name });
    const more = r.total > r.text.split("\n").length ? `（先頭だけ。全 ${r.total} 行）` : `（全 ${r.total} 行）`;
    openDialog(title, [el("p", { class: "sub", text: `比べ方: ${r.kind} ${more}` }),
      el("pre", { class: "changes" }, el("span", { text: r.text || "(空)" }))], r.text);
  } catch (e) {
    openDialog(title, [el("div", { class: "error", text: e.message })]);
  }
}

async function openCopy(name, text) {
  try {
    const r = await api("/api/open", { name, text });
    toast(r.shown_in_folder ? `開くと何かが動く形式なので、エクスプローラーで場所を示しました（読み取り専用のコピー）\n${r.opened}`
      : `読み取り専用のコピーを開きました（元のファイルではありません）\n${r.opened}`);
  }
  catch (e) { toast(e.message, true); }
}

// The person pastes this into an AI chat themselves. Show exactly what would leave this PC before it is
// copied, leave out what a summary does not need (where the sources are, local paths), and once the
// answer is read, acknowledge exactly the changes that were copied (not one that arrived since).
const AI_LONG = 20000;
// What started a check (as kept in the log), in words. The same list as VIA in check.py.
const VIA_LABEL = { terminal: "ターミナル", scheduler: "タスク スケジューラ", page: "画面で押した", "page-auto": "画面の自動", added: "追加したとき" };

async function openPlace(name) {
  try {
    const r = await api("/api/place", { name });
    toast(r.how === "select" ? "フォルダで開きました（ファイルは開いていません）" : r.how === "folder" ? "フォルダを開きました" : "ブラウザで開きました");
  } catch (e) { toast(e.message, true); }
}

async function copyForAI(names) {
  let places = false;
  const fetchText = async () => {
    try { return await api("/api/markdown", { names, places }); } catch (e) { toast(e.message, true); return null; }
  };
  let r = await fetchText();
  if (!r) return;
  if (!r.count) { toast("未確認の変更も取得失敗もありません"); return; }
  const body = el("div");
  const draw = () => {
    const box = el("input", { type: "checkbox", checked: places });
    box.addEventListener("change", async () => {
      places = box.checked;
      const again = await fetchText();
      if (again) { r = again; draw(); }
    });
    const read = Object.keys(r.seen);
    body.replaceChildren(...[
      el("p", { class: "sub", style: "margin-top:0", text: `未確認の変更 ${r.changed} 件・取得失敗 ${r.failed} 件・${r.text.length.toLocaleString()} 文字。下の文章をそのまま貼ります。中を確かめてからコピーしてください。` }),
      r.text.length > AI_LONG ? el("div", { class: "error", text: "長いので、AI によっては途中までしか読みません。一覧で見張りを選ぶか、絞り込むと短くなります。" }) : null,
      el("label", { style: "display:flex;gap:6px;align-items:center;margin:8px 0" }, box, "元のファイルの場所（共有フォルダのパスや URL）も入れる"),
      el("pre", { class: "changes", style: "max-height:48vh;overflow:auto" }, el("span", { text: r.text })),
      el("div", { class: "actions" },
        el("button", { class: "primary", text: "コピー", onclick: async () => {
          const ok = await copyText(r.text);
          toast(ok ? "コピーしました。AI に貼って、返ってきた要約を読んでください" : "コピーできませんでした", !ok);
        } }),
        el("span", { class: "spacer" }),
        read.length ? el("button", {
          text: `読み終えた ${read.length} 件を確認済みにする`,
          title: "ここに出した時点の変更だけを確認済みにする。そのあとに届いた変更は残る",
          onclick: async () => { closeDialog(true); await runAck(read, "AI で要約して確認", r.seen); },
        }) : null),
    ].filter(Boolean));
  };
  draw();
  openDialog("AI 用にコピー", [body], null, { wide: true });
}

// Only what is on screen is acknowledged: the copy shown in the detail panel, or for the list,
// the copies the list was drawn from. A newer copy that arrived meanwhile stays unacknowledged.
function seenSnapshots(names) {
  const seen = {};
  for (const t of state.overview ? state.overview.targets : []) if (names.includes(t.name) && t.snapshot) seen[t.name] = t.snapshot;
  if (state.detail && names.includes(state.detail.name) && state.detail.snapshot) seen[state.detail.name] = state.detail.snapshot;
  return seen;
}

async function runAck(names, note, seen = null) {
  try {
    const r = await api("/api/ack", { names, note: note || null, seen: seen || seenSnapshots(names) });
    const msgs = [];
    if (r.done.length) msgs.push(`確認済みにしました: ${r.done.join("、")}`);
    if (r.stale.length) msgs.push(`新しい変更が届いたので、確認済みにしませんでした: ${r.stale.join("、")}（表示を更新しました）`);
    toast(msgs.join("\n") || "確認済みにするものはありませんでした", r.stale.length > 0);
  } catch (e) { toast(e.message, true); }
  await loadOverview();
  if (state.selected) await select(state.selected, true);
}

// ------------------------------------------------------------------ config tab

const configDirty = () => $("config-text").value !== state.config.saved;

function renderConfigState() {
  const s = $("config-state");
  const dirty = configDirty();
  s.textContent = dirty ? "保存していない変更あり" : "保存済み";
  s.classList.toggle("dirty", dirty);
}

function renderConfigTargets(names) {
  const sel = $("config-target");
  const keep = sel.value || state.selected;
  sel.replaceChildren(...names.map((n) => el("option", { value: n, text: n })));
  if (names.includes(keep)) sel.value = keep;
  $("config-try").disabled = !names.length;
}

// The commands this Windows user has allowed: what runs, for which targets, and a way to take it back.
async function renderAllowed() {
  let r;
  try { r = await api("/api/commands"); } catch (e) { $("allowed").replaceChildren(el("div", { class: "error", text: e.message })); return; }
  const revoke = async (ids, what) => {
    if (!confirm(`${what}を取り消します。\n\nそのコマンドを使う見張りは、もう一度許可するまで動きません。`)) return;
    try {
      const res = await api("/api/commands/revoke", { ids });
      toast(`取り消しました（${res.revoked.length} 件）`);
      await renderAllowed();
      if (state.selected) await select(state.selected, true);
    } catch (e) { toast(e.message, true); }
  };
  const unused = r.entries.filter((e) => !e.used_by.length);
  const parts = [
    el("div", { class: "actions", style: "margin:18px 0 4px" },
      el("h3", { text: "許可したコマンド", style: "margin:0" }), el("span", { class: "spacer" }),
      unused.length ? el("button", { class: "mini", text: `使っていない ${unused.length} 件を取り消す`,
        onclick: () => revoke(unused.map((e) => e.id), `どの見張りも使っていない ${unused.length} 件の許可`) }) : null),
    el("p", { class: "sub", text: "コマンドの出力を見張る見張りは、ここにあるコマンドだけを動かします。許可はこの PC のこのユーザーで共通で、コマンドが1文字でも変わると、もう一度許可が要ります。" }),
  ];
  if (r.waiting.length) {
    parts.push(el("div", { class: "error" },
      el("b", { text: "まだ許可していないコマンド" }),
      el("ul", { style: "margin:6px 0 0" }, r.waiting.map((w) => el("li", {},
        el("a", { href: "#", text: w.name, onclick: (e) => { e.preventDefault(); showTab("targets"); select(w.name); } }),
        ": ", el("code", { text: w.line }))))));
  }
  if (!r.entries.length) {
    parts.push(el("p", { class: "sub", style: "margin-top:12px", text: "許可したコマンドは、まだありません。" }));
  } else {
    parts.push(el("div", { class: "table-wrap" }, el("table", {},
      el("thead", {}, el("tr", {}, ["コマンド", "使っている見張り", "許可した日時", ""].map((h) => el("th", { text: h })))),
      el("tbody", {}, r.entries.map((e) => el("tr", {},
        el("td", {}, el("code", { text: e.line }), e.read_only ? null
          : el("div", {}, el("span", { class: "badge failed", title: "安全リストの外。ターミナルの watchpoint allow --unsafe で許可したもの", text: "読むだけと分かっていない（ターミナルで許可）" }))),
        el("td", { text: e.used_by.length ? e.used_by.join("、") : "（どの見張りも使っていない）", class: e.used_by.length ? null : "sub" }),
        el("td", { text: e.allowed }),
        el("td", {}, el("button", { class: "mini", text: "取り消す", onclick: () => revoke([e.id], "このコマンドの許可") }))))))));
  }
  $("allowed").replaceChildren(...parts.filter(Boolean));
}

async function loadConfig(force = false) {
  if (!force && state.config.hash && configDirty()) return;
  try {
    const r = await api("/api/config");
    state.config.hash = r.hash;
    state.config.saved = r.text;
    $("config-text").value = r.text;
    $("config-path").textContent = r.path;
    $("config-result").replaceChildren();
    renderConfigState();
    await validateConfig(true);
  } catch (e) { $("config-result").replaceChildren(el("div", { class: "error", text: e.message })); }
}

// Settings that work but fit their use badly (a column of a table watched by its cells): said, not refused.
const adviceBox = (advice) => (advice && advice.length
  ? el("div", { class: "notice advice" }, el("b", { text: "書き方の助言" }), advice.map((a) => el("p", { text: a })))
  : null);

async function validateConfig(quiet = false) {
  const r = await api("/api/config/validate", { text: $("config-text").value });
  if (r.ok) renderConfigTargets(r.targets);  // while the text has an error, keep the last good list to try with
  const tip = r.ok ? adviceBox(r.advice) : null;
  if (!r.ok) $("config-result").replaceChildren(el("div", { class: "error", text: r.error }));
  else if (!quiet) $("config-result").replaceChildren(...[el("div", { class: "okbox", text: `誤りはありません（見張り ${r.targets.length} 件）` }), tip].filter(Boolean));
  else $("config-result").replaceChildren(...[tip].filter(Boolean));
  return r.ok;
}

async function saveConfig() {
  try {
    const r = await api("/api/config/save", { text: $("config-text").value, hash: state.config.hash });
    state.config.hash = r.hash;
    state.config.saved = $("config-text").value;
    renderConfigState();
    renderConfigTargets(r.targets);
    $("config-result").replaceChildren(...[el("div", { class: "okbox", text: `保存しました（見張り ${r.targets.length} 件。前の内容は .bak に）` }), adviceBox(r.advice)].filter(Boolean));
    await loadOverview();
  } catch (e) { $("config-result").replaceChildren(el("div", { class: "error", text: e.message })); }
}

async function openInConfig(name) {
  await showTab("config");
  const ta = $("config-text");
  const lines = ta.value.split("\n");
  const i = lines.findIndex((l) => /^\s*-?\s*name:/.test(l) && l.includes(name));
  if (i < 0) return;
  const start = lines.slice(0, i).join("\n").length + (i ? 1 : 0);
  ta.focus();
  ta.setSelectionRange(start, start + lines[i].length);
  ta.scrollTop = Math.max(0, (i - 3) * parseFloat(getComputedStyle(ta).lineHeight));
  if ([...$("config-target").options].some((o) => o.value === name)) $("config-target").value = name;
}

function setupConfig() {
  const ta = $("config-text");
  let timer;
  ta.addEventListener("input", () => {
    renderConfigState();
    clearTimeout(timer);  // check the text shortly after typing stops
    timer = setTimeout(() => validateConfig(true).catch(() => {}), 700);
  });
  ta.addEventListener("keydown", (e) => {
    if (e.key === "Tab" && !e.shiftKey) {
      e.preventDefault();
      ta.setRangeText("  ", ta.selectionStart, ta.selectionEnd, "end");
      renderConfigState();
    }
  });
  $("config-add").addEventListener("click", openAddTarget);
  $("config-validate").addEventListener("click", () => validateConfig().catch((e) => toast(e.message, true)));
  $("config-save").addEventListener("click", saveConfig);
  $("config-reload").addEventListener("click", () => {
    if (!configDirty() || confirm("保存していない変更を捨てて、ファイルから読み直します")) loadConfig(true);
  });
  $("config-try").addEventListener("click", async () => {
    const name = $("config-target").value;
    if (!name) return;
    if (await validateConfig(true).catch(() => false)) showPreview(name, $("config-text").value);
  });
  window.addEventListener("beforeunload", (e) => { if (state.config.hash && configDirty()) e.preventDefault(); });
}

// ------------------------------------------------------------------ add a target

// The form is a guided look at the source: give a place, see what is in it, click what matters,
// and start watching. It writes the YAML a person would write, saves it, and runs the first check,
// which takes the baseline; the next change is then the first thing reported.

// How often: "" follows the config's every (30 minutes unless changed there). Nothing shorter than
// 15 minutes is offered; the config refuses anything under 5.
const EVERY_CHOICES = ["15m", "30m", "1h", "2h", "12h", "1d"];
const defaultEvery = () => (state.overview && state.overview.every_default) || "30m";
function everyLabel(v) {
  const m = /^\s*(\d+)\s*(m|min|h|d)\s*$/.exec(v || "");
  if (!m) return v || "";
  const n = Number(m[1]);
  if (m[2] === "d") return `${n} 日ごと`;
  if (m[2] === "h") return n === 12 ? "半日ごと" : `${n} 時間ごと`;
  return `${n} 分ごと`;
}
function everyOptions(current) {
  const values = current && !EVERY_CHOICES.includes(current) ? [...EVERY_CHOICES, current] : EVERY_CHOICES;
  return [el("option", { value: "", text: `既定（${everyLabel(defaultEvery())}）` }),
    ...values.map((v) => el("option", { value: v, text: everyLabel(v) }))];
}
const hoursNote = (h) => (h ? `（${h.label} の間だけ）` : "");
const REPORTS = [["added", "追加"], ["changed", "変更"], ["removed", "削除"]];
const MODES = [
  ["content", "中身の変化", "何が変わったかを出す。読むポイントに沿って AI に要約させられる"],
  ["cells", "決まったセルだけ", "合計・状態・承認欄など、選んだセルの値が変わったときだけ"],
  ["timestamp", "保存されたかだけ", "更新日時とサイズだけを見る。中身は開かない"],
];
const GRID_ROWS = 100;
const RECORD_LINE = /^\[([^=\]]+)=(.*?)\](?: (.*))?$/;

const splitList = (s) => s.split(/[,、\n]/).map((x) => x.trim()).filter(Boolean);
const splitLines = (s) => s.split("\n").map((x) => x.trim()).filter(Boolean);
const colName = (n) => { let s = ""; for (; n > 0; n = Math.floor((n - 1) / 26)) s = String.fromCharCode(65 + ((n - 1) % 26)) + s; return s; };
const quoteSheet = (name) => (/[\s'!]/.test(name) ? `'${name.replace(/'/g, "''")}'` : name);
const cleanPath = (s) => s.trim().replace(/^"(.*)"$/, "$1").trim();
const fmtSize = (n) => (n >= 1 << 20 ? `${(n / (1 << 20)).toFixed(1)} MB` : n >= 1024 ? `${Math.round(n / 1024)} KB` : `${n} バイト`);

function formField(label, input, help, required = false) {
  return el("label", { class: "field" },
    el("span", { class: "flabel" }, label, required ? el("span", { class: "req", text: "必須" }) : null),
    input, help ? el("small", { class: "help", text: help }) : null);
}

function parseWhere(text) {
  const where = {};
  for (const line of splitLines(text)) {
    const m = line.match(/^([^:：]+)[:：](.*)$/);
    if (!m) throw new Error(`「この行だけ」は「列: 値」の形で書いてください: ${line}`);
    const values = splitList(m[2]);
    if (!values.length) throw new Error(`「この行だけ」の値がありません: ${line}`);
    where[m[1].trim()] = values.length === 1 ? values[0] : values;
  }
  return where;
}

// "集計!F20" lines stay a list; once one line is named ("合計 = 集計!F21") they become a mapping.
function parseCells(text) {
  const rows = splitLines(text).map((line) => {
    const i = line.indexOf("=");
    return i < 0 ? [null, line] : [line.slice(0, i).trim(), line.slice(i + 1).trim()];
  });
  if (!rows.some(([label]) => label)) return rows.map(([, ref]) => ref);
  const named = {};
  for (const [label, ref] of rows) named[label || ref] = ref;
  return named;
}

function suggestName(source, peek) {
  if (/^https?:\/\//i.test(source)) {
    try {
      const u = new URL(source);
      const last = decodeURIComponent(u.pathname.split("/").filter(Boolean).pop() || "");
      return last ? `${u.hostname} ${last.replace(/\.[a-z0-9]+$/i, "")}` : u.hostname;
    } catch (e) { return ""; }
  }
  const parts = source.replace(/[\\/]+$/, "").split(/[\\/]/).filter((p) => p && !/[*?]/.test(p));
  const base = (peek && peek.file) || parts.pop() || "";
  return base.replace(/\.[^.]+$/, "");
}

function uniqueName(base) {
  const taken = new Set(state.overview ? state.overview.targets.map((t) => t.name) : []);
  if (!base || !taken.has(base)) return base;
  for (let i = 2; ; i++) if (!taken.has(`${base}（${i}）`)) return `${base}（${i}）`;
}

// The label a person would give a cell: the text to its left on the same row, else the one above it.
function cellLabel(rows, r, c) {
  const isLabel = (v) => v && !/^[\d\s.,:/\-+%¥$]+$/.test(v);
  for (let j = c - 2; j >= 0 && j >= c - 4; j--) { const v = (rows[r - 1] || [])[j]; if (isLabel(v)) return v; }
  for (let i = r - 2; i >= 0 && i >= r - 3; i--) { const v = (rows[i] || [])[c - 1]; if (isLabel(v)) return v; }
  return "";
}

function sampleNotice(name, text, keyedRows) {
  const lines = text.split("\n").filter((l) => l && !l.startsWith("## ") && !l.startsWith("@列 "));
  const rec = lines.map((l) => l.match(RECORD_LINE)).find(Boolean);
  if (rec) {
    const fields = (rec[3] || "").split(" | ").filter(Boolean).map((p) => [p.slice(0, p.indexOf("=")), p.slice(p.indexOf("=") + 1)]);
    const [field, value] = fields.length ? fields[fields.length - 1] : ["値", ""];
    const out = [["l-mod", `変更 [${rec[1]}=${rec[2]}] ${field}: ${value || "(空)"} → （変わった値）`]];
    if (keyedRows) out.push(["l-add", `追加 [${rec[1]}=…] （新しく足された行）`]);
    return { title: `${name}: 変更 1 件${keyedRows ? " / 追加 1 件" : ""}`, lines: out };
  }
  const first = lines[0] || "";
  return { title: `${name}: +1 / -1 行`, lines: [["l-del", `-${first}`], ["l-add", "+（書き換えられた行）"]] };
}

function openAddTarget() {
  const editorDirty = !!(state.config.hash && configDirty());
  const w = {
    source: "", info: null, peek: null, loading: false, nameAuto: true, mode: "content",
    sheet: 0, header: null, key: "", cols: null, whereCol: "", whereVals: new Set(), cells: [],
    report: new Set(REPORTS.map(([k]) => k)), preview: null,
  };

  // ---- the inputs that live for the whole form (never re-created, so typing keeps its focus)
  const f = {
    source: el("input", { type: "text", class: "big", placeholder: "\\\\server\\share\\質問表.xlsx　や　https://…", "aria-label": "見張る場所", autocomplete: "off" }),
    name: el("input", { type: "text", "aria-label": "名前" }),
    points: el("textarea", { rows: 3 }),
    notify: el("textarea", { rows: 2, placeholder: "自分宛ての新しい質問があるとき" }),
    every: el("select", { "aria-label": "チェックの間隔" }, everyOptions()),
    tags: el("input", { type: "text", placeholder: "〇〇案件, 毎週" }),
    link: el("input", { type: "text", placeholder: "https://… や \\\\server\\share\\フォルダ" }),
    selector: el("input", { type: "text", placeholder: "main article（空ならページ全体）" }),
    attachments: el("input", { type: "text", placeholder: "*.xlsx（空ならページ）" }),
    mKey: el("input", { type: "text", placeholder: "No." }),
    mColumns: el("input", { type: "text", placeholder: "質問, 回答者, 状態（空ならすべて）" }),
    mWhere: el("textarea", { rows: 2, placeholder: "回答者: ${me}" }),
    mHeader: el("input", { type: "text", placeholder: "空なら自動で探す" }),
    mSheets: el("input", { type: "text", placeholder: "質問一覧（空ならすべて）" }),
    mCells: el("textarea", { rows: 3, placeholder: "集計!F20\n合計 = 集計!F21\nB3:D5" }),
    addCell: el("input", { type: "text", placeholder: "集計!F20 や B3:D5", "aria-label": "セル番地" }),
  };
  f.every.value = "";  // follow the config's every, so changing that later changes this one too
  const reportBoxes = Object.fromEntries(REPORTS.map(([k]) => [k, el("input", { type: "checkbox", checked: true })]));

  const area = {
    found: el("div", { class: "found", "aria-live": "polite" }),
    modes: el("div", { class: "modes", role: "radiogroup", "aria-label": "見張り方" }),
    picker: el("div", { class: "picker" }),
    summary: el("div", { class: "summary", "aria-live": "polite" }),
    result: el("div", { "aria-live": "polite" }),
  };
  const later = [];  // the steps shown once the place is known
  let pointsStep = null;
  const step = (n, title, ...body) => {
    const s = el("section", { class: "step" }, el("h4", {}, el("span", { class: "num", text: n }), title), ...body);
    if (n !== "1") later.push(s);
    return s;
  };

  // ---- reading the place

  let lookTimer = null;
  let looking = Promise.resolve();
  let lookSeq = 0;
  const schedule = () => { clearTimeout(lookTimer); lookTimer = setTimeout(() => { lookTimer = null; looking = look(); }, 450); };
  const settled = () => { if (lookTimer) { clearTimeout(lookTimer); lookTimer = null; looking = look(); } return looking; };

  async function look() {
    const source = cleanPath(f.source.value);
    if (f.source.value !== source) f.source.value = source;  // Explorer's "copy as path" adds quotes
    if (source === w.source) return;
    const seq = ++lookSeq;
    Object.assign(w, { source, info: null, peek: null, preview: null, header: null, key: "", cols: null, whereCol: "", cells: [], sheet: 0 });
    w.whereVals = new Set();
    area.result.replaceChildren();
    if (!source) { renderAll(); return; }
    w.loading = true;
    renderAll();
    let info, peek;
    try { info = await api("/api/config/describe", { source }); } catch (e) { info = { kind: null }; }
    if (seq !== lookSeq) return;
    w.info = info;
    if (info.link) { w.loading = false; renderAll(); return; }  // a SharePoint link for a browser: nothing to read
    renderAll();
    try { peek = await api("/api/peek", { source }); } catch (e) { peek = { error: e.message }; }
    if (seq !== lookSeq) return;
    w.loading = false;
    applyPeek(peek);
    if (w.nameAuto) f.name.value = uniqueName(suggestName(source, peek));
    renderAll();
  }

  // What was read about the source: start on the sheet with the most data, with its header and key guessed.
  function applyPeek(peek) {
    w.peek = peek;
    const sheets = peek.sheets || [];
    const size = (s) => (s.header_row ? s.rows.slice(s.header_row).filter((r) => r.some(Boolean)).length : -1);
    w.sheet = sheets.reduce((best, s, i) => (size(s) > size(sheets[best]) ? i : best), 0);
    pickSheetDefaults();
    if (!modeAllowed(w.mode)) w.mode = "content";
  }

  // An Excel file on SharePoint cannot be read directly. To click on its table, either a hidden Excel
  // saves a copy (the same way every check fetches it), or the person picks a copy they downloaded.
  // Only the URL goes into the config either way.
  // A table file behind a web address (a Confluence attachment) is the same: it is downloaded once to show it.
  const isOfficeExcel = () => !!(w.info && ((w.info.kind === "office" && w.info.excel) || (w.info.kind === "download" && w.info.table)));
  const isDownload = () => !!(w.info && w.info.kind === "download");

  async function fetchOffice() {
    const source = w.source;
    w.officeBusy = true;
    renderAll();
    let peek;
    try { peek = await api("/api/peek/office", { source }); } catch (e) { peek = { error: e.message }; }
    w.officeBusy = false;
    if (source !== w.source) return;  // the place was changed meanwhile
    applyPeek(peek);
    renderAll();
  }

  async function pickSample() {
    let r;
    try { r = await api("/api/browse", { folder: false, start: "~/Downloads" }); } catch (e) { toast(e.message, true); return; }
    if (!r.path) return;
    const source = w.source;
    let peek;
    try { peek = await api("/api/peek/sample", { path: r.path }); } catch (e) { peek = { error: e.message }; }
    if (source !== w.source) return;
    applyPeek(peek);
    renderAll();
  }

  function officeBox() {
    const p = w.peek || {};
    const web = isDownload();
    const again = [
      el("button", { type: "button", class: "mini", text: web ? "取り直す" : "Excel で取り直す", disabled: !!w.officeBusy, onclick: fetchOffice }),
      el("button", { type: "button", class: "mini", text: "別のファイルを選ぶ…", onclick: pickSample }),
    ];
    if (hasGrid()) {
      return el("div", { class: "actions", style: "margin:0 0 8px" },
        el("span", { class: "help", style: "margin:0", text: p.sample_file
          ? `手で落とした「${p.sample_file.split(/[\\/]/).pop()}」の表です。見張るのは URL のファイルです（「試しに取り出す」は URL から取ってきます）。`
          : web ? "URL から取ってきた表です。" : "Excel で取ってきた表です。" }), ...again);
    }
    return el("div", { class: "card" },
      el("p", { class: "lead", style: "margin-top:0", text: web
        ? "Web 上のファイルです（Confluence の添付など）。表を見ながら選ぶには、一度ダウンロードします。"
        : "SharePoint の Excel です。表を見ながら選ぶには、中を一度取ってきます。" }),
      el("div", { class: "actions", style: "margin:0" },
        el("button", { type: "button", class: "primary", disabled: !!w.officeBusy, onclick: fetchOffice,
          text: w.officeBusy ? (web ? "取ってきています…" : "Excel で取ってきています…（十数秒かかります）")
            : web ? "取ってきて表を出す" : "Excel で取ってきて表を出す" }),
        el("span", { class: "sub", style: "margin:0", text: "または" }),
        el("button", { type: "button", text: "ダウンロードしたファイルを選ぶ…", onclick: pickSample }),
        el("a", { href: w.source, target: "_blank", rel: "noopener noreferrer", text: "ブラウザで開く（手でダウンロードするとき）" })),
      p.error ? el("div", { class: "error", text: p.error }) : null,
      el("p", { class: "help", text: web
        ? "取ってきたものは表を出すためだけに使い、設定には URL だけを書きます。ログインが要るときは、足したあとに「設定」で auth を書くか、手で落としたファイルを選んでください。列やセルは下で手で入れることもできます。"
        : "Excel は、ふだんの Office のサインインで、読み取り専用で開きます（元のファイルは書き換えない）。取ってきたものは表を出すためだけに使い、設定には URL だけを書きます。列やセルは下で手で入れることもできます。" }));
  }

  function pickSheetDefaults() {
    const s = sheetNow();
    w.header = s ? s.header_row : null;
    const keys = s && w.header ? keysFor(s, w.header) : [];
    w.key = keys.length ? keys[0] : "";
    w.cols = null;
    w.show = new Set();
    w.whereCol = "";
    w.whereVals = new Set();
  }

  const sheetNow = () => (w.peek && w.peek.sheets ? w.peek.sheets[w.sheet] : null);
  const headersOf = (s) => (s && w.header ? s.rows[w.header - 1] : []);
  const isExcel = () => !!(w.info && w.info.excel);
  const isTable = () => !!(w.info && w.info.table);
  const hasGrid = () => !!sheetNow();
  const modeAllowed = (m) => m === "content" || (m === "cells" && isExcel()) || (m === "timestamp" && !!(w.info && w.info.timestamp));

  // ---- step 2: how to watch

  function renderModes() {
    area.modes.replaceChildren(...MODES.map(([value, title, help]) => {
      const ok = modeAllowed(value);
      const r = el("input", { type: "radio", name: "wp-mode", value, checked: w.mode === value, disabled: !ok });
      r.addEventListener("change", () => { w.mode = value; w.preview = null; area.result.replaceChildren(); renderAll(); });
      const why = !ok ? (value === "cells" ? "Excel のときに選べる" : "1つのファイルのときに選べる") : help;
      return el("label", { class: "choice" }, r, el("span", { class: "ctitle", text: title }), el("small", { text: why }));
    }));
  }

  // ---- step 3: where to look (the grid, or what fits the kind of place)

  function renderSheetTabs() {
    const sheets = w.peek.sheets;
    if (sheets.length < 2) return null;
    return el("div", { class: "sheet-tabs", role: "tablist", "aria-label": "シート" }, sheets.map((s, i) => el("button", {
      type: "button", class: "chip", role: "tab", "aria-pressed": String(i === w.sheet), text: s.name,
      onclick: () => { w.sheet = i; if (w.mode === "content") pickSheetDefaults(); renderAll(); },
    })));
  }

  function whereMatch(row, headers) {
    if (!w.whereCol || !w.whereVals.size) return true;
    const j = headers.indexOf(w.whereCol);
    const v = j >= 0 ? row[j] || "" : "";
    return [...w.whereVals].some((x) => v.includes(x));
  }

  function renderGrid(s) {
    const cellsMode = w.mode === "cells";
    const headers = headersOf(s);
    const keyIdx = w.key ? headers.indexOf(w.key) : -1;
    const width = s.rows.reduce((m, r) => Math.max(m, r.length), 0);
    const table = el("table", { class: `grid${cellsMode ? " cells" : ""}` });
    table.append(el("thead", {}, el("tr", {}, el("th", { text: "" }),
      Array.from({ length: width }, (_, j) => el("th", { text: colName(j + 1) })))));
    const body = el("tbody");
    const selected = selectedSet(s.name);
    s.rows.slice(0, GRID_ROWS).forEach((row, i) => {
      const r = i + 1;
      const isHdr = !cellsMode && r === w.header;
      const cls = cellsMode ? "" : isHdr ? "hdr" : w.header && r < w.header ? "above" : w.header && !whereMatch(row, headers) ? "out" : "";
      const tr = el("tr", { class: cls });
      tr.append(el("th", cellsMode ? { text: r } : {
        text: r, title: "この行を見出しにする", tabindex: "0",
        onclick: () => { w.header = r; w.key = ""; w.cols = null; w.show = new Set(); w.whereCol = ""; w.whereVals = new Set(); const k = keysFor(s, r); if (k.length) w.key = k[0]; renderAll(); },
      }));
      for (let j = 0; j < width; j++) {
        const v = row[j] || "";
        const h = headers[j];
        const shownOnly = !cellsMode && w.header && r >= w.header && h && showsOnly(h);
        const off = !cellsMode && w.header && r >= w.header && w.cols && h && !w.cols.has(h) && j !== keyIdx && !shownOnly;
        const td = el("td", {
          text: v, title: v.length > 12 ? v : null, "data-r": r, "data-c": j + 1,
          class: [off ? "off" : "", shownOnly ? "show-only" : "", j === keyIdx && w.header && r >= w.header ? "keycol" : "", selected.has(`${r}:${j + 1}`) ? "sel" : ""].filter(Boolean).join(" ") || null,
        });
        if (isHdr && h) {
          td.tabIndex = 0;
          td.title = j === keyIdx ? "行を見分ける列" : columnTitle(h);
          td.addEventListener("click", () => toggleColumn(s, h));
          td.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggleColumn(s, h); } });
        }
        tr.append(td);
      }
      body.append(tr);
    });
    table.append(body);
    if (cellsMode) wireDrag(table, s);
    const more = s.max_row > GRID_ROWS ? el("p", { class: "help", text: `先頭 ${GRID_ROWS} 行を表示（シートは ${s.max_row} 行）` }) : null;
    return [el("div", { class: "grid-wrap" }, table), more];
  }

  // Columns whose values tell the rows apart (all filled, none repeated); ones named like an ID first.
  const KEY_NAME = /^(no\.?|ｎｏ．?|#|id|番号|通番|連番|管理番号|項番|.*(id|ＩＤ|番号|no\.?))$/i;
  function keysFor(s, header) {
    const hs = s.rows[header - 1] || [];
    const data = s.rows.slice(header).filter((r) => r.some(Boolean));
    if (data.length < 2) return [];
    return hs.map((h, j) => [h, j]).filter(([h, j]) => h && data.every((r) => r[j]) && new Set(data.map((r) => r[j])).size === data.length)
      .sort((a, b) => (KEY_NAME.test(b[0].trim()) - KEY_NAME.test(a[0].trim())) || a[1] - b[1])
      .map(([h]) => h);
  }

  // For each column, the values below the header with how often they appear: the choices for "この行だけ".
  function valuesFor(s, header) {
    const hs = s.rows[header - 1] || [];
    const data = s.rows.slice(header).filter((r) => r.some(Boolean));
    const out = {};
    hs.forEach((h, j) => {
      if (!h) return;
      const counts = new Map();
      for (const r of data) if (r[j]) counts.set(r[j], (counts.get(r[j]) || 0) + 1);
      if ((counts.size > 1 && counts.size <= 50) || (counts.size === 1 && data.length > 1)) {
        out[h] = [...counts].sort((a, b) => b[1] - a[1]).slice(0, 30);
      }
    });
    return out;
  }

  // A column is watched (a change in it is told), read only (shown beside a changed row, never told on its
  // own; needs a key), or left out. A click moves it on to the next of these.
  const showsOnly = (h) => !!(w.show && w.show.has(h));
  const watched = (h) => !showsOnly(h) && (!w.cols || w.cols.has(h));
  function columnTitle(h) {
    if (watched(h)) return w.key ? "見る列（変わったら知らせる）。クリックで「見るだけ」にする" : "見る列。クリックで外す";
    if (showsOnly(h)) return "見るだけの列（変わった行と一緒に出す。この列だけの変化は知らせない）。クリックで外す";
    return "見ない列。クリックで見る列に戻す";
  }
  function toggleColumn(s, h) {
    if (h === w.key) return;
    const all = headersOf(s).filter((x) => x && x !== w.key);
    const cols = w.cols ? new Set(w.cols) : new Set(all.filter((x) => !showsOnly(x)));
    if (!w.show) w.show = new Set();
    if (watched(h)) {
      cols.delete(h);
      if (w.key) w.show.add(h);  // read only: shown beside a change, which needs rows told apart by a key
    } else if (showsOnly(h)) {
      w.show.delete(h);
    } else {
      cols.add(h);
    }
    const everything = all.filter((x) => !w.show.has(x));
    w.cols = everything.every((x) => cols.has(x)) && cols.size === everything.length ? null : cols;
    renderAll();
  }

  // Cells: click one, or drag across a range.
  function selectedSet(sheet) {
    const out = new Set();
    for (const c of w.cells) if (c.sheet === sheet) for (let r = c.r1; r <= c.r2; r++) for (let k = c.c1; k <= c.c2; k++) out.add(`${r}:${k}`);
    return out;
  }

  function wireDrag(table, s) {
    let start = null;
    let end = null;
    const mark = () => {
      for (const td of table.querySelectorAll("td.drag")) td.classList.remove("drag");
      if (!start) return;
      const [r1, r2] = [Math.min(start.r, end.r), Math.max(start.r, end.r)];
      const [c1, c2] = [Math.min(start.c, end.c), Math.max(start.c, end.c)];
      for (const td of table.querySelectorAll("td")) {
        const r = +td.dataset.r, c = +td.dataset.c;
        if (r >= r1 && r <= r2 && c >= c1 && c <= c2) td.classList.add("drag");
      }
    };
    const at = (e) => { const td = e.target.closest("td"); return td ? { r: +td.dataset.r, c: +td.dataset.c } : null; };
    table.addEventListener("mousedown", (e) => { const p = at(e); if (!p) return; e.preventDefault(); start = p; end = p; mark(); });
    table.addEventListener("mouseover", (e) => { if (!start) return; const p = at(e); if (p) { end = p; mark(); } });
    const finish = () => {
      if (!start) return;
      const r1 = Math.min(start.r, end.r), r2 = Math.max(start.r, end.r), c1 = Math.min(start.c, end.c), c2 = Math.max(start.c, end.c);
      start = null;
      const same = w.cells.findIndex((c) => c.sheet === s.name && c.r1 === r1 && c.r2 === r2 && c.c1 === c1 && c.c2 === c2);
      if (same >= 0) w.cells.splice(same, 1);
      else addCells(s.name, r1, c1, r2, c2);
      renderAll();
    };
    table.addEventListener("mouseup", finish);
    table.addEventListener("mouseleave", () => { if (start) finish(); });
  }

  function addCells(sheet, r1, c1, r2, c2) {
    const book = w.peek.sheets.find((x) => x.name === sheet);
    const single = r1 === r2 && c1 === c2;
    const value = single && book ? (book.rows[r1 - 1] || [])[c1 - 1] || "" : "";
    const name = single && book ? cellLabel(book.rows, r1, c1).replace(/[|=[\]:：]/g, "").slice(0, 20) : "";
    w.cells.push({ sheet, r1, c1, r2, c2, value, name });
  }

  const cellRef = (c) => `${colName(c.c1)}${c.r1}` + (c.r1 === c.r2 && c.c1 === c.c2 ? "" : `:${colName(c.c2)}${c.r2}`);

  function addTypedCell() {
    const m = f.addCell.value.trim().match(/^(?:(?:'((?:[^']|'')+)'|([^!]+))!)?\$?([A-Za-z]{1,3})\$?(\d+)(?::\$?([A-Za-z]{1,3})\$?(\d+))?$/);
    if (!m) { toast("セルは B3、集計!F20、B3:D5 のように書いてください", true); return; }
    const sheet = (m[1] && m[1].replace(/''/g, "'")) || m[2] || sheetNow().name;
    if (!w.peek.sheets.some((s) => s.name === sheet)) { toast(`シート「${sheet}」がありません`, true); return; }
    const num = (letters) => [...letters.toUpperCase()].reduce((n, ch) => n * 26 + ch.charCodeAt(0) - 64, 0);
    const c1 = num(m[3]), r1 = +m[4], c2 = m[5] ? num(m[5]) : c1, r2 = m[6] ? +m[6] : r1;
    addCells(sheet, Math.min(r1, r2), Math.min(c1, c2), Math.max(r1, r2), Math.max(c1, c2));
    f.addCell.value = "";
    renderAll();
  }
  f.addCell.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); addTypedCell(); } });

  function renderCellList() {
    if (!w.cells.length) return el("p", { class: "help", text: "表のセルをクリックして選ぶ（ドラッグで範囲）。もう一度クリックで外す" });
    return el("div", { class: "cell-list" }, w.cells.map((c, i) => {
      const name = el("input", { type: "text", value: c.name, placeholder: "名前（任意）", "aria-label": `${cellRef(c)} の名前` });
      name.addEventListener("input", () => { c.name = name.value.replace(/[|=[\]]/g, ""); renderSummary(); });
      return el("div", { class: "cellrow" },
        el("code", { text: `${c.sheet}!${cellRef(c)}` }),
        el("span", { class: "help", text: c.value ? `いまの値: ${c.value}` : c.r1 === c.r2 && c.c1 === c.c2 ? "（空）" : "範囲" }),
        name,
        el("button", { type: "button", class: "mini", text: "外す", onclick: () => { w.cells.splice(i, 1); renderAll(); } }));
    }));
  }

  function renderTableControls(s) {
    const headers = headersOf(s).filter(Boolean);
    if (!w.header) {
      return [el("p", { class: "warn", text: "見出しの行が見つかりませんでした。表の左の行番号をクリックして、見出しの行を選んでください（選ばなければ、行番号付きのテキストで比べる）" })];
    }
    const keys = keysFor(s, w.header);
    const keySel = el("select", { "aria-label": "行を見分ける列" },
      el("option", { value: "", text: "行番号で比べる（行を挿入すると、ずれて見える）" }),
      ...headers.map((h) => el("option", { value: h, text: keys.includes(h) ? `${h}${h === keys[0] ? "（おすすめ）" : ""}` : `${h}（重なる値あり）` })));
    keySel.value = w.key;
    keySel.addEventListener("change", () => {
      w.key = keySel.value;
      if (w.cols) w.cols.delete(w.key);
      if (w.show) { w.show.delete(w.key); if (!w.key) w.show = new Set(); }  // read-only columns need a key
      renderAll();
    });

    const all = headers.filter((h) => h !== w.key);
    const colChips = el("div", { class: "chips" },
      el("button", { type: "button", class: "chip", "aria-pressed": String(!w.cols && !(w.show && w.show.size)), text: "すべて", title: "あとで増えた列も見る", onclick: () => { w.cols = null; w.show = new Set(); renderAll(); } }),
      all.map((h) => el("button", {
        type: "button", class: showsOnly(h) ? "chip show-only" : "chip", "aria-pressed": String(watched(h)),
        text: showsOnly(h) ? `${h}（見るだけ）` : h, title: columnTitle(h), onclick: () => toggleColumn(s, h),
      })));

    const values = valuesFor(s, w.header);
    const vars = (w.peek && w.peek.vars) || {};
    const filterable = headers.filter((h) => values[h]);
    const whereSel = el("select", { "aria-label": "絞り込む列" }, el("option", { value: "", text: "絞り込まない" }), ...filterable.map((h) => el("option", { value: h, text: h })));
    whereSel.value = w.whereCol;
    whereSel.addEventListener("change", () => { w.whereCol = whereSel.value; w.whereVals = new Set(); renderAll(); });
    const varOf = (v) => Object.entries(vars).find(([, x]) => x && x === v);
    const valueChips = w.whereCol ? el("div", { class: "chips" }, (values[w.whereCol] || []).map(([v, n]) => {
      const vr = varOf(v);
      return el("button", {
        type: "button", class: "chip", "aria-pressed": String(w.whereVals.has(v)), title: `${n} 行`,
        text: vr ? `${v}（\${${vr[0]}}）` : v,
        onclick: () => { w.whereVals.has(v) ? w.whereVals.delete(v) : w.whereVals.add(v); renderAll(); },
      });
    })) : null;

    // "Only my rows": a column holding the value of a variable such as ${me}.
    let mine = null;
    if (!w.whereCol) {
      for (const h of filterable) {
        const hit = (values[h] || []).find(([v]) => varOf(v));
        if (hit) {
          const [name] = varOf(hit[0]);
          mine = el("div", { class: "suggest" }, `「${h}」列に「${hit[0]}」（\${${name}}）があります。自分の行だけを見張りますか？`,
            el("button", { type: "button", class: "mini primary", text: "そうする", onclick: () => { w.whereCol = h; w.whereVals = new Set([hit[0]]); renderAll(); } }));
          break;
        }
      }
    }
    return [
      el("div", { class: "controls" },
        el("div", { class: "ctl" }, el("span", { class: "flabel", text: "見出しの行" }), el("span", { text: `${w.header} 行目` }),
          el("small", { class: "help", text: "違うなら、表の左の行番号をクリック" })),
        el("div", { class: "ctl" }, el("span", { class: "flabel", text: "行を見分ける列" }), keySel,
          el("small", { class: "help", text: "番号や ID のように、行ごとに違って変わらない列。行の追加・削除・並べ替えを正しく出せる" }))),
      el("div", { class: "ctl" }, el("span", { class: "flabel", text: "見る列" }), colChips,
        el("small", { class: "help", text: "クリックするたびに 見る → 見るだけ → 見ない と切り替わる（表の見出しをクリックしても同じ）。見るだけの列は、ほかの列が変わった行と一緒に出すが、その列だけが変わっても知らせない（行を見分ける列が要る）" })),
      el("div", { class: "ctl" }, el("span", { class: "flabel", text: "この行だけ" }), filterable.length ? whereSel : el("small", { class: "help", text: "絞り込める列がありません" }), valueChips,
        mine),
    ];
  }

  function renderManual() {
    if (w.mode === "cells") {
      return [formField("見るセル", f.mCells, "1行に1つ。B3、集計!F20、範囲は B3:D5。「合計 = 集計!F21」と書くと、変化に「合計」と出る", true)];
    }
    return [
      el("div", { class: "cols2" },
        formField("行を見分ける列", f.mKey, "番号や ID など、行ごとに変わらない列の見出し"),
        formField("見出しの行", f.mHeader, "数字。空なら見出しのある行を探す")),
      formField("見る列", f.mColumns, "カンマで区切る。空ならすべて"),
      formField("この行だけ", f.mWhere, "1行に「列: 値」。その文字を含む行だけ"),
      isExcel() ? formField("シート", f.mSheets, "カンマで区切る。空ならすべて") : null,
    ];
  }

  function renderPicker() {
    const p = w.peek;
    const parts = [];
    const kind = w.info && w.info.kind;
    if (w.mode === "timestamp") {
      parts.push(el("p", { class: "help", text: "保存されると、更新日時とサイズの変化を知らせます。中身はコピーも開きもしないので、大きいファイルやテキストにできない形式でも軽く動きます。" }));
    } else if (w.mode === "cells") {
      if (isOfficeExcel()) parts.push(officeBox());
      if (hasGrid()) {
        parts.push(renderSheetTabs(), ...renderGrid(sheetNow()),
          el("div", { class: "addcell" }, f.addCell, el("button", { type: "button", class: "mini", text: "番地で足す", onclick: addTypedCell })),
          renderCellList());
      } else parts.push(...renderManual());
    } else if (kind === "folder") {
      parts.push(el("p", { class: "lead", text: p && p.files != null ? `ファイル ${p.files} 個` : "フォルダ" }),
        p && p.sample && p.sample.length ? el("p", { class: "help", text: `例: ${p.sample.slice(0, 5).join("、")}${p.files > 5 ? " …" : ""}` }) : null,
        el("p", { class: "help", text: "ファイルが増えた・消えた・更新されたを知らせます（サイズと更新日時で比べる。中身はコピーしない）。" }));
    } else if (isTable() && !isOfficeExcel()) {
      if (hasGrid()) parts.push(renderSheetTabs(), ...renderGrid(sheetNow()), ...renderTableControls(sheetNow()));
      else parts.push(...renderManual());
    } else if (kind === "web") {
      parts.push(formField("見る部分（CSS セレクター）", f.selector, "空ならページ全体。閲覧数など毎回変わる部分は、あとで設定ファイルの remove で外せる"));
    } else if (kind === "json" || kind === "confluence") {
      parts.push(el("p", { class: "help", text: kind === "json"
        ? "JSON をそのまま比べます。一覧を行ごとに比べるときや認証が要るときは、足したあとに「設定」で records・key・auth を書きます。"
        : "ページの本文と、版・最終更新者・添付ファイルの一覧を比べます（Server / Data Center では REST API を使いません）。認証が要るときは、足したあとに「設定」で auth を書きます（watchpoint secret set で資格情報を登録）。" }),
        kind === "confluence" ? formField("添付だけを見る", f.attachments,
          "*.xlsx のように書くと、ページの本文は見ずに、名前が当てはまる添付ファイルを中身で比べます（カンマで複数）。空ならページを見ます。行ごとに比べるときは、足したあとに「設定」で key を書きます") : null);
    } else if (isOfficeExcel()) {
      parts.push(officeBox());
      if (hasGrid()) parts.push(renderSheetTabs(), ...renderGrid(sheetNow()), ...renderTableControls(sheetNow()));
      else parts.push(...renderManual());
    } else if (kind === "download") {
      parts.push(el("p", { class: "help", text: "チェックのたびに URL のファイルをダウンロードし、共有フォルダのファイルと同じように中身で比べます。Confluence の添付なら、リンクに付いた版の指定は外して、いつも最新を取ります。" }));
    } else if (kind === "office") {
      parts.push(el("p", { class: "help", text: "Word に読み取り専用で開かせて取ってきます（Office のサインインを使う）。" }));
    } else if (p && p.file) {
      parts.push(el("p", { class: "help", text: w.info.text === false
        ? "この形式はテキストにできないので、中身が変わったかどうかだけを見ます（どこが変わったかは出せません）。保存されたかだけを見るなら、上の「保存されたかだけ」の方が軽く動きます。"
        : "見出し・本文・表をテキストにして、前回と行ごとに比べます。" }));
    }
    // Clicking a cell redraws the grid; keep it scrolled to where the person was looking.
    const old = area.picker.querySelector(".grid-wrap");
    const scroll = old ? [old.scrollTop, old.scrollLeft] : null;
    area.picker.replaceChildren(...parts.filter(Boolean));
    const now = area.picker.querySelector(".grid-wrap");
    if (scroll && now) [now.scrollTop, now.scrollLeft] = scroll;
  }

  // ---- step 1: what was found at the place

  function renderFound() {
    const p = w.peek;
    if (!w.source) {
      area.found.replaceChildren(el("p", { class: "help", text: "共有フォルダやパソコンのファイル・フォルダ、Web ページ、Confluence、SharePoint の Excel / Word を見張れます。エクスプローラーでファイルを Shift + 右クリック →「パスのコピー」で貼れます（前後の \" はそのままで構いません）。" }));
      return;
    }
    const link = w.info && w.info.link;
    if (link) {
      // The address bar or 「リンクをコピー」 gives a link for a browser; the config needs the file's own address.
      area.found.replaceChildren(...[el("p", { class: "warn", text: link.advice }),
        link.file_url ? el("button", { type: "button", class: "primary", text: "ファイルの URL に置き換える",
          onclick: () => { f.source.value = link.file_url; looking = look(); } }) : null].filter(Boolean));
      return;
    }
    const label = w.info && w.info.kind ? w.info.label : "";
    if (w.loading) {
      area.found.replaceChildren(el("p", { class: "loading", text: `${label ? label + "。" : ""}中を見ています…（コピーを取って読む。元のファイルは開きません）` }));
      return;
    }
    if (p && p.error) {
      area.found.replaceChildren(el("p", { class: "warn", text: p.error }));
      return;
    }
    const bits = [label];
    if (p && p.sheets && isExcel()) bits.push(`${p.sheets.length} シート`);
    if (p && p.size != null) bits.push(fmtSize(p.size), `最終更新 ${p.modified}`);
    area.found.replaceChildren(el("p", { class: "ok-line", text: bits.filter(Boolean).join("・") }));
  }

  // ---- the target as YAML fields

  function build() {
    const it = {
      name: f.name.value.trim(), source: w.source, tags: splitList(f.tags.value), every: f.every.value || null, link: f.link.value.trim() || null,
      points: f.points.value, notify_when: f.notify.value,
    };
    if (!it.source) throw new Error("見張る場所を入れてください");
    if (!it.name) throw new Error("名前を入れてください");
    const s = sheetNow();
    if (w.mode === "timestamp") it.watch = "timestamp";
    if (w.mode === "cells") {
      if (hasGrid()) {
        if (!w.cells.length) throw new Error("見るセルを選んでください");
        const refs = w.cells.map((c) => [c.name.trim(), `${quoteSheet(c.sheet)}!${cellRef(c)}`]);
        if (refs.some(([n]) => n)) {
          const named = {};
          for (const [n, ref] of refs) { let k = n || ref; for (let i = 2; k in named; i++) k = `${n || ref}（${i}）`; named[k] = ref; }
          it.cells = named;
        } else it.cells = refs.map(([, ref]) => ref);
      } else {
        it.cells = parseCells(f.mCells.value);
        if (!Object.keys(it.cells).length) throw new Error("見るセルを入れてください");
      }
    }
    if (w.mode === "content" && isTable()) {
      if (hasGrid()) {
        if (w.header) {
          const headers = headersOf(s);
          if (w.key) it.key = w.key;
          if (w.cols) {
            if (!w.cols.size) throw new Error("見る列を1つ以上選んでください");
            it.columns = headers.filter((h) => h && w.cols.has(h) && h !== w.key);
          }
          if (w.key && w.show && w.show.size) it.show = headers.filter((h) => h && w.show.has(h) && h !== w.key);
          if (w.whereCol && w.whereVals.size) {
            const vars = w.peek.vars || {};
            const vals = [...w.whereVals].map((v) => { const vr = Object.entries(vars).find(([, x]) => x === v); return vr ? `\${${vr[0]}}` : v; });
            it.where = { [w.whereCol]: vals.length === 1 ? vals[0] : vals };
          }
          // "auto" follows the table if rows are inserted above it; it finds the first row holding these names
          const wanted = it.key ? [it.key] : it.columns || [];
          const autoRow = wanted.length ? s.rows.slice(0, 30).findIndex((r) => wanted.every((x) => r.includes(x))) + 1 : 0;
          if (wanted.length && autoRow === w.header) it.header_row = "auto";
          else if (it.key || it.columns || it.where) it.header_row = w.header;
          if (isExcel() && w.peek.sheets.length > 1 && (it.key || it.columns || it.where)) it.sheets = [s.name];
        } else if (isExcel() && w.peek.sheets.length > 1) it.sheets = [s.name];
      } else {
        it.key = f.mKey.value;
        it.columns = splitList(f.mColumns.value);
        it.where = parseWhere(f.mWhere.value);
        if (isExcel()) it.sheets = splitList(f.mSheets.value);
        const hr = f.mHeader.value.trim();
        if (/^\d+$/.test(hr)) it.header_row = Number(hr);
        else if (hr && hr !== "auto") throw new Error("見出しの行は数字で入れてください（空なら自動で探す）");
        else if (it.key.trim() || it.columns.length) it.header_row = "auto";
      }
    }
    if (w.mode === "content" && w.info && w.info.kind === "web") it.selector = f.selector.value;
    if (w.mode === "content" && w.info && w.info.kind === "confluence") {
      const names = f.attachments.value.split(/[,、]/).map((s) => s.trim()).filter(Boolean);
      if (names.length) it.attachments = names;
    }
    if (w.mode === "content") {
      const report = REPORTS.map(([k]) => k).filter((k) => reportBoxes[k].checked);
      if (!report.length) throw new Error("知らせる変化を1つ以上選んでください（名前・間隔・知らせ方）");
      if (report.length < 3) it.report = report;
    }
    return it;
  }

  // ---- the summary in plain words, and what still blocks starting

  function describeChoice() {
    const s = sheetNow();
    const every = everyLabel(f.every.value || defaultEvery()) + hoursNote(state.overview && state.overview.check_hours);
    const kinds = w.mode === "cells" ? "値が変わったら" : w.mode === "timestamp" ? "保存されたら"
      : `${REPORTS.filter(([k]) => reportBoxes[k].checked).map(([, t]) => t).join("・") || "なし"}を`;
    let what = "";
    const block = [];
    if (!w.source) block.push("見張る場所を入れてください");
    else if (w.mode === "timestamp") what = `${w.peek && w.peek.file ? w.peek.file : "ファイル"} が保存されたか（更新日時とサイズ）を見張ります。`;
    else if (w.mode === "cells") {
      if (!hasGrid()) what = "書いたセルを見張ります。";
      else if (!w.cells.length) block.push("見るセルを表から選んでください");
      else {
        const names = w.cells.map((c) => c.name || cellRef(c));
        what = `${w.cells.length} か所のセル（${names.slice(0, 4).join("、")}${names.length > 4 ? " ほか" : ""}）を見張ります。`;
      }
    } else if (isTable() && hasGrid()) {
      const headers = headersOf(s);
      const rows = w.header ? s.rows.slice(w.header).filter((r) => r.some(Boolean) && whereMatch(r, headers)).length : s.rows.filter((r) => r.some(Boolean)).length;
      const sheetPart = isExcel() ? `「${s.name}」シートの` : "";
      const approx = s.max_row > s.rows.length ? "先頭の " : "";
      if (!w.header) what = `${sheetPart}表を、行番号付きのテキストで見張ります（${approx}${rows} 行）。`;
      else {
        what = `${sheetPart}${approx}${rows} 行を、${w.key ? `「${w.key}」で行を見分けて` : "行番号で"}見張ります`;
        if (w.cols) what += `（見る列: ${[...w.cols].join("・") || "なし"}）`;
        if (w.key && w.show && w.show.size) what += `（見るだけ: ${[...w.show].join("・")}）`;
        if (w.whereCol && w.whereVals.size) what += `。「${w.whereCol}」が ${[...w.whereVals].join("・")} の行だけ`;
        what += "。";
        if (w.cols && !w.cols.size) block.push("見る列を1つ以上選んでください");
      }
    } else if (w.info && w.info.kind) what = `${w.info.label} の中身を見張ります。`;
    if (!f.name.value.trim() && w.source) block.push("名前を入れてください（名前・間隔・知らせ方）");
    const when = !what ? "" : `${every}にチェックし、${kinds}知らせます。`;
    const name = f.name.value.trim();
    return { text: (what && name ? `「${name}」として、` : "") + what + when, block };
  }

  function renderSummary() {
    const d = describeChoice();
    const parts = w.source ? [d.text ? el("p", { text: d.text }) : null, ...d.block.map((b) => el("p", { class: "warn", text: b }))] : [];
    area.summary.replaceChildren(...parts.filter(Boolean));
    startBtn.disabled = d.block.length > 0 || w.loading;
  }

  function renderAll() {
    for (const s of later) s.hidden = !w.source || !(w.info && w.info.kind);
    renderFound();
    renderModes();
    renderPicker();
    renderSummary();
    reportField.hidden = w.mode !== "content";
    pointsStep.hidden = pointsStep.hidden || w.mode === "timestamp";  // nothing to read when the content is not opened
    f.points.placeholder = w.mode === "cells" ? "合計が変わったら、前の値と差を書く。承認欄が埋まったら知らせる。"
      : w.mode === "timestamp" ? "（中身は見ないので、空のままで構いません）"
      : "追加された行は新しい質問。それぞれの要旨を1行で。\n状態が変わった行は、何から何に変わったかを挙げる。";
  }

  // ---- trying it, and starting

  async function draftText(it) {
    const base = editorDirty ? $("config-text").value : (await api("/api/config")).text;
    return api("/api/config/add-target", { text: base, item: it });
  }

  async function tryIt() {
    await settled();
    area.result.replaceChildren(el("p", { class: "loading", text: "取ってきています…（保存も比較もしません）" }));
    try {
      const it = build();
      const r = await draftText(it);
      if (!r.ok) throw new Error(r.error);
      const p = await api("/api/preview", { name: r.name, text: r.text });
      const body = p.text.split("\n").filter((l) => l && !l.startsWith("## ") && !l.startsWith("@列 "));
      const keyedRows = !!it.key;
      const sample = sampleNotice(it.name, p.text, keyedRows);
      area.result.replaceChildren(el("div", { class: "tried" },
        el("p", { class: "lead", text: `取り出せました: ${p.total > p.text.split("\n").length ? `${body.length} 行以上` : `${body.length} ${w.mode === "cells" ? "か所" : "行"}`}（比べ方: ${p.kind}）` }),
        el("div", { class: "notice-sample" }, el("div", { class: "nhead" }, el("span", { class: "badge changed", text: "変更あり" }), el("b", { text: sample.title }), el("span", { class: "help", text: "変わったときの知らせの例" })),
          el("pre", { class: "changes" }, sample.lines.map(([cls, t]) => el("span", { class: cls, text: t })))),
        el("details", {}, el("summary", { text: "比べるテキストを見る" }), el("pre", { class: "changes" }, el("span", { text: p.text || "(空)" })))));
      area.result.scrollIntoView({ block: "nearest", behavior: "smooth" });
    } catch (e) {
      area.result.replaceChildren(el("div", { class: "error", text: e.message }));
      area.result.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
  }

  async function start() {
    await settled();
    let it;
    try { it = build(); } catch (e) { area.result.replaceChildren(el("div", { class: "error", text: e.message })); return; }
    startBtn.disabled = true;
    try {
      if (editorDirty) {  // the editor holds unsaved edits: add to them, and leave saving to the person
        const r = await draftText(it);
        $("config-text").value = r.text;
        renderConfigState();
        if (r.ok) renderConfigTargets(r.targets);
        closeDialog(true);
        await openInConfig(r.name);
        $("config-result").replaceChildren(el("div", { class: r.ok ? "okbox" : "error",
          text: r.ok ? `「${r.name}」を編集欄に足しました。編集中の内容と一緒に「保存」してください` : `「${r.name}」を足しましたが、ほかの所に誤りがあります\n${r.error}` }));
        return;
      }
      const r = await api("/api/targets/add", { item: it });
      state.config.hash = null;  // the editor reloads the saved file next time
      closeDialog(true);
      state.justAdded = r.name;
      const mine = (r.advice || []).filter((a) => a.startsWith(`「${r.name}」`));
      if (mine.length) toast(mine.join("\n"));
      await showTab("targets");
      await loadOverview();
      await select(r.name);
      runCheck([r.name], false, "added");
    } catch (e) {
      area.result.replaceChildren(el("div", { class: "error", text: e.message }));
      startBtn.disabled = false;
    }
  }

  // ---- assembling the form

  const browse = async (folder) => {
    try {
      const r = await api("/api/browse", { folder, start: cleanPath(f.source.value) || null });
      if (r.path) { f.source.value = r.path; looking = look(); }
    } catch (e) { toast(e.message, true); }
  };
  f.source.addEventListener("input", schedule);
  f.source.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); settled(); } });
  f.name.addEventListener("input", () => { w.nameAuto = !f.name.value.trim(); renderSummary(); });
  f.every.addEventListener("change", renderSummary);
  for (const b of Object.values(reportBoxes)) b.addEventListener("change", renderSummary);

  const reportField = el("div", { class: "field" }, el("span", { class: "flabel", text: "知らせる変化" }),
    el("div", { class: "checks" }, REPORTS.map(([k, t]) => el("label", {}, reportBoxes[k], t))),
    el("small", { class: "help", text: "外した種類の変化は数えず、自動で確認済みにする" }));
  const startBtn = el("button", { type: "submit", class: "primary", text: editorDirty ? "編集欄に足す" : "見張りを始める" });
  const form = el("form", { class: "form wizard", onsubmit: (e) => { e.preventDefault(); start(); } },
    step("1", "何を見張りますか",
      el("div", { class: "source-row" }, f.source,
        el("button", { type: "button", text: "ファイル…", title: "ファイルを選ぶ", onclick: () => browse(false) }),
        el("button", { type: "button", text: "フォルダ…", title: "フォルダを選ぶ", onclick: () => browse(true) })),
      area.found),
    step("2", "どう見張りますか", area.modes),
    step("3", "どこを見ますか", area.picker),
    pointsStep = step("4", "AI への指示（任意）", formField("読むポイント", f.points, "変わったとき、変更と一緒に出す。「AI 用にコピー」で貼ると、AI がこれに沿って要約する")),
    el("details", { class: "advanced" }, el("summary", { text: "名前・間隔・知らせ方" }),
      el("div", { class: "cols2" },
        formField("名前", f.name, "一覧や通知に出る。場所から自動で付ける", true),
        formField("チェックの間隔", f.every, "自動チェックと「時間が来たものだけ」で使う。短くしすぎると、取りに行く先（共有フォルダや Web）の負担になる")),
      el("div", { class: "cols2" },
        formField("タグ", f.tags, "カンマで区切る。一覧の絞り込みに使う"),
        reportField),
      formField("知らせる条件", f.notify, "AI が「知らせるべき変化か」を決める基準"),
      formField("開く場所（任意）", f.link, "画面の「フォルダで開く」などで開く先。空なら、ファイルはそのフォルダ、Web ページはそのページ。SharePoint の Excel なら、そのフォルダの URL を入れておくと、元のファイルを開かずに済む")),
    area.summary,
    area.result,
    el("div", { class: "form-actions" },
      editorDirty ? el("small", { class: "help", text: "設定の編集欄に保存していない変更があるので、そこに足します" }) : null,
      el("span", { class: "spacer" }),
      el("button", { type: "button", text: "試しに取り出す", title: "保存せずに取ってきて、比べる中身と知らせの例を見る", onclick: tryIt }),
      startBtn));
  later.forEach((s) => { s.hidden = true; });
  // the name and options belong with the later steps too
  const adv = form.querySelector("details.advanced");
  later.push(adv);

  renderAll();
  openDialog("見張りを追加", [form], null, { wide: true, guard: () => !!(w.source || f.points.value.trim()) });
  f.source.focus();
}

// ------------------------------------------------------------------ log tab

async function renderLog() {
  let r;
  try { r = await api("/api/log"); } catch (e) { $("logview").replaceChildren(el("p", { class: "empty", text: e.message })); return; }
  if (!r.entries.length) { $("logview").replaceChildren(el("p", { class: "empty", text: "まだログがありません。" })); return; }
  $("logview").replaceChildren(
    el("p", { class: "sub", text: `新しい順に ${r.entries.length} 件（チェックと確認済み）` }),
    el("div", { class: "table-wrap" }, el("table", {},
      el("thead", {}, el("tr", {}, ["時刻", "見張り", "経路", "結果", "内容"].map((h) => el("th", { text: h })))),
      el("tbody", {}, r.entries.slice().reverse().map((e) => el("tr", {},
        el("td", { style: "white-space:nowrap", text: short(e.time) }),
        el("td", {}, el("button", { class: "mini", text: e.name || "", onclick: () => { showTab("targets"); select(e.name); } })),
        el("td", { class: "nowrap sub", text: VIA_LABEL[e.via] || "" }),
        el("td", {}, el("span", { class: `badge ${badgeClass(e.status)}`, text: e.status || "" })),
        el("td", { text: e.message || "" })))))));
}

async function showTab(which) {
  state.tab = which;
  for (const b of document.querySelectorAll("nav.tabs button")) {
    b.setAttribute("aria-selected", String(b.dataset.tab === which));
    b.tabIndex = b.dataset.tab === which ? 0 : -1;
  }
  $("view-targets").hidden = which !== "targets";
  $("view-config").hidden = which !== "config";
  $("view-log").hidden = which !== "log";
  if (which === "log") await renderLog();
  if (which === "config") { await loadConfig(); await renderAllowed(); }
}

// ------------------------------------------------------------------ keyboard

function focusSelected() {
  const cur = document.querySelector('#list li[aria-current="true"]');
  if (cur) cur.focus({ preventScroll: true });
}

function onKey(e) {
  if (e.ctrlKey && e.key === "s" && state.tab === "config" && !$("dialog").open) { e.preventDefault(); saveConfig(); return; }
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);
  if (typing || e.ctrlKey || e.metaKey || e.altKey || $("dialog").open || state.tab !== "targets") return;
  const names = state.overview ? shownTargets().map((t) => t.name) : [];
  const i = names.indexOf(state.selected);
  const inList = $("list").contains(document.activeElement);
  const move = (name) => { select(name); if (inList) focusSelected(); };
  if (e.key === "j" || e.key === "ArrowDown") { e.preventDefault(); if (names.length) move(names[Math.min(names.length - 1, i + 1)]); }
  else if (e.key === "k" || e.key === "ArrowUp") { e.preventDefault(); if (names.length) move(names[Math.max(0, i - 1)]); }
  else if (e.key === "/") { e.preventDefault(); $("search").focus(); }
  else if (e.key === "x" && state.selected) togglePick(state.selected);
  else if (e.key === "Escape" && state.picked.size) clearPicks();
  else if (e.key === "c" && state.selected && !jobActive()) runCheck([state.selected]);
  else if (e.key === "f" && state.detail && state.detail.changes) { e.preventDefault(); openFocus(state.detail); }
  else if (e.key === "a" && state.detail && state.detail.pending) runAck([state.detail.name], ($("ack-note") || {}).value);
}

// ------------------------------------------------------------------ start

for (const b of document.querySelectorAll("nav.tabs button")) b.addEventListener("click", () => showTab(b.dataset.tab));
document.querySelector("nav.tabs").addEventListener("keydown", (e) => {
  const tabs = [...document.querySelectorAll("nav.tabs button")];
  const i = tabs.indexOf(document.activeElement);
  const to = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: tabs.length - 1 }[e.key];
  if (i < 0 || to === undefined) return;
  e.preventDefault();
  const next = tabs[(to + tabs.length) % tabs.length];
  next.focus();
  showTab(next.dataset.tab);
});
$("add-target").addEventListener("click", openAddTarget);
$("check-all").addEventListener("click", () => runCheck(checkScope()));
$("check-due").addEventListener("click", () => runCheck(checkScope(), true));
$("copy-ai").addEventListener("click", () =>
  copyForAI(state.picked.size ? [...state.picked] : filtering() ? shownTargets().map((t) => t.name) : null));
$("search").addEventListener("input", renderList);
$("search").addEventListener("keydown", (e) => { if (e.key === "Escape") { e.target.value = ""; renderList(); e.target.blur(); } });
$("pending-only").addEventListener("change", renderList);
$("dialog-close").addEventListener("click", () => closeDialog());
$("dialog").addEventListener("cancel", (e) => { e.preventDefault(); closeDialog(); });  // Esc asks too
document.addEventListener("keydown", onKey);
setupAuto();
setupConfig();

(async () => {
  await loadOverview();
  if (!state.overview) return;
  const names = state.overview.targets.map((t) => t.name);
  const active = state.overview.targets.filter((t) => t.enabled);
  if (!names.includes(state.selected)) state.selected = (active.find(needsLook) || active[0] || {}).name;
  if (state.selected) select(state.selected);
  else renderWelcome();
})();
setInterval(() => { if (!jobActive() && document.visibilityState === "visible") loadOverview(); }, 30000);
