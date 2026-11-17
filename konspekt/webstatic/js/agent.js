/* ============================================================
   agent.js — панель агента: видно, ЧТО он делает и О ЧЁМ думает.

   Отличие от «чата с ИИ» (ai.js): там вопрос → ответ. Здесь агент
   работает руками — ходит в Moodle, читает файлы, запускает команды —
   и каждый шаг транслируется в панель по мере выполнения:
   размышления, карточки действий, результат, артефакты, ответ.

   Транспорт — SSE (/api/agent/stream): события идут в браузер сразу,
   как только агент что-то сделал. «Стоп» рвёт соединение и агент
   останавливается на середине.
   ============================================================ */
"use strict";

import { $, esc, toast, copyText, throttleRAF, prefersReduced, plural } from "./util.js";
import { state, notes } from "./store.js";
import { streamSSE } from "./api.js";
import { renderMD } from "./md.js";

const KEY = "konspect.agent.v1";
const MAX_RUNS = 60;
const MAX_CHARS = 60000;

/* иконка и цвет карточки действия — по группе инструмента */
const GROUP_ICON = {
  moodle: "ph-graduation-cap",
  conspect: "ph-book-open-text",
  study: "ph-pencil-simple",
  web: "ph-globe-hemisphere-west",
  system: "ph-terminal-window",
  remind: "ph-bell",
  misc: "ph-sparkle",
};

/* быстрые задачи агента (в отличие от вопросов ассистенту — это ДЕЙСТВИЯ) */
const QUICK = [
  { t: "Что горит по срокам?", i: "ph-fire" },
  { t: "Собери все дедлайны на две недели", i: "ph-calendar-dots" },
  { t: "Какие лекции ещё без конспекта?", i: "ph-book-open-text" },
  { t: "Проверь, жива ли сессия Moodle", i: "ph-heartbeat" },
  { t: "Сделай конспект последней записи", i: "ph-wand" },
  { t: "Что нового появилось на курсах?", i: "ph-sparkle" },
];

/* ---------- хранилище ---------- */
function lsGet(key, fb) {
  try {
    const raw = localStorage.getItem(key);
    return raw == null ? fb : JSON.parse(raw);
  } catch { return fb; }
}
function lsSet(key, val) {
  try { localStorage.setItem(key, JSON.stringify(val)); } catch { /* приватный режим */ }
}

const store = {
  runs: lsGet(KEY, {}),          // ctxKey -> [ {role, ...} ]
  get(key) { return this.runs[key] || []; },
  push(key, msg) {
    const list = this.runs[key] || [];
    list.push(msg);
    if (list.length > MAX_RUNS) list.splice(0, list.length - MAX_RUNS);
    let size = 0;
    for (const m of list) size += (m.answer || m.text || "").length + (m.reasoning || "").length;
    while (size > MAX_CHARS && list.length > 2) {
      const gone = list.shift();
      size -= (gone.answer || gone.text || "").length + (gone.reasoning || "").length;
    }
    this.runs[key] = list;
    lsSet(KEY, this.runs);
  },
  clear(key) { delete this.runs[key]; lsSet(KEY, this.runs); },
};

/* ---------- режим панели ---------- */
export const agentMode = () => state.ai.mode === "agent";

export function setMode(mode, { focus = false } = {}) {
  state.ai.mode = mode === "agent" ? "agent" : "chat";
  try { localStorage.setItem("konspect.ai.mode", state.ai.mode); } catch { /* noop */ }

  document.querySelectorAll("[data-ai-mode]").forEach((b) => {
    const on = b.dataset.aiMode === state.ai.mode;
    b.classList.toggle("is-active", on);
    b.setAttribute("aria-selected", String(on));
  });
  const chat = $("#ai-chat");
  const agent = $("#ai-agent");
  if (chat) chat.hidden = agentMode();
  if (agent) agent.hidden = !agentMode();
  const inp = $("#ai-input");
  if (inp) inp.placeholder = agentMode() ? "Что сделать?" : "Спроси что-нибудь…";
  if (focus) inp?.focus();
  if (agentMode()) renderAgent();
  window.dispatchEvent(new CustomEvent("ai-mode-changed", { detail: state.ai.mode }));
}

/* ---------- разметка ---------- */
function stepHTML(s) {
  const icon = GROUP_ICON[s.group] || GROUP_ICON.misc;
  const status = s.status === "running" ? "running" : s.status === "error" ? "error" : "ok";
  return `<li class="ag-step" data-status="${status}" data-group="${esc(s.group || "misc")}">
    <span class="ag-step-icon"><i class="ph ${icon}" aria-hidden="true"></i></span>
    <div class="ag-step-main">
      <div class="ag-step-title">
        <span class="ag-step-label">${esc(s.label || s.name)}</span>
        ${s.argsSummary ? `<span class="ag-step-args">${esc(s.argsSummary)}</span>` : ""}
      </div>
      ${s.preview ? `<div class="ag-step-preview">${esc(s.preview)}</div>` : ""}
    </div>
    <span class="ag-step-tail">${
      status === "running" ? `<span class="ag-spin" aria-label="выполняется"></span>`
        : status === "error" ? `<i class="ph ph-warning-circle" title="ошибка"></i>`
          : `<i class="ph ph-check" title="готово"></i>`
    }${s.ms ? `<span class="ag-step-ms">${fmtMs(s.ms)}</span>` : ""}</span>
  </li>`;
}

/* длинный список действий сворачиваем: 22 шага подряд читать невозможно */
const FOLD_AFTER = 8;

function stepsHTML(steps) {
  if (!steps.length) return "";
  const long = steps.length > FOLD_AFTER;
  return `<ol class="ag-steps${long ? " is-long" : ""}">${steps.map(stepHTML).join("")}</ol>`
    + (long ? moreBtnHTML(steps.length) : "");
}

function moreBtnHTML(total, open = false) {
  const hidden = total - FOLD_AFTER;
  return `<button class="ag-more" type="button">${open
    ? "свернуть" : `ещё ${hidden} ${plural(hidden, "действие", "действия", "действий")}`}</button>`;
}

/** Обновляет сворачивание по ходу живого прогона. */
function syncStepsFold(runEl, total) {
  const list = runEl.querySelector(".ag-steps");
  if (!list || total <= FOLD_AFTER) return;
  list.classList.add("is-long");
  let btn = runEl.querySelector(".ag-more");
  if (!btn) {
    const tmp = document.createElement("div");
    tmp.innerHTML = moreBtnHTML(total);
    btn = tmp.firstElementChild;
    list.after(btn);
  }
  const open = list.classList.contains("is-open");
  btn.textContent = moreBtnHTML(total, open).replace(/<[^>]+>/g, "");
}

function runHTML(r, i) {
  const steps = stepsHTML(r.steps || []);
  const arts = (r.artifacts || []).map(artifactHTML).join("");
  return `<div class="ag-run${r.error ? " is-error" : ""}" data-i="${i}">
    ${runHeadHTML(r)}
    ${steps}
    ${reasonHTML(r.reasoning)}
    ${r.draft ? `<div class="ag-draft">${renderMD(r.draft)}</div>` : ""}
    <div class="ag-answer">${r.answer ? renderMD(r.answer) : (r.error ? esc(r.error) : "")}</div>
    ${arts ? `<div class="ag-artifacts">${arts}</div>` : ""}
    ${r.answer && !r.error ? answerActionsHTML(i) : ""}
  </div>`;
}

function runHeadHTML(r) {
  const running = r.status === "running";
  const n = (r.steps || []).length;
  const meta = [];
  if (n) meta.push(`${n} ${plural(n, "действие", "действия", "действий")}`);
  if (r.ms) meta.push(fmtMs(r.ms));
  if (r.turns > 1) meta.push(`${r.turns} ${plural(r.turns, "шаг", "шага", "шагов")}`);
  return `<div class="ag-run-head">
    <span class="ag-run-status">${running
      ? `<span class="ag-pulse" aria-hidden="true"></span><span class="ag-run-word">работает</span>`
      : r.error ? `<i class="ph ph-warning-circle"></i><span class="ag-run-word">не вышло</span>`
        : `<i class="ph ph-check-circle"></i><span class="ag-run-word">готово</span>`}</span>
    <span class="ag-run-meta">${esc(meta.join(" · "))}</span>
  </div>`;
}

function reasonHTML(text) {
  // узлы создаём всегда, даже когда текста ещё нет: иначе в живой ленте
  // некуда писать первые же куски размышлений
  const body = text || "";
  const n = body.trim().length;
  return `<div class="ag-reason"${body ? "" : " hidden"}>
    <button class="ag-reason-toggle" type="button" aria-expanded="false">
      <i class="ph ph-brain" aria-hidden="true"></i>
      <span>Как я рассуждал</span>
      <span class="ag-reason-count">${n} зн.</span>
      <i class="ph ph-caret-down ag-caret" aria-hidden="true"></i>
    </button>
    <div class="ag-reason-body" hidden><div class="ag-reason-text">${esc(body)}</div></div>
  </div>`;
}

function artifactHTML(a) {
  if (a.kind === "photo") {
    // картинку показываем превью — так результат видно, не скачивая
    return `<a class="ag-art is-photo" href="${esc(a.url)}" target="_blank" rel="noopener">
      <img src="${esc(a.url)}" alt="${esc(a.name)}" loading="lazy">
      <span class="ag-art-name">${esc(a.name)}</span>
    </a>`;
  }
  return `<a class="ag-art" href="${esc(a.url)}" download>
    <i class="ph ph-file-arrow-down" aria-hidden="true"></i>
    <span class="ag-art-name">${esc(a.name)}</span>
    <span class="ag-art-cap">${esc(a.caption || "скачать")}</span>
  </a>`;
}

function answerActionsHTML(i) {
  return `<div class="ag-actions">
    <button class="ag-act" data-act="copy" data-i="${i}"><i class="ph ph-clipboard-text"></i>Копировать</button>
    <button class="ag-act" data-act="note" data-i="${i}"><i class="ph ph-notebook"></i>В заметки</button>
    <button class="ag-act" data-act="again" data-i="${i}"><i class="ph ph-arrow-clockwise"></i>Повторить</button>
  </div>`;
}

function userHTML(m) {
  return `<div class="msg user ag-user">${m.quote
    ? `<div class="msg-quote"><i class="ph ph-quotes" aria-hidden="true"></i><span>${esc(m.quote)}</span></div>`
    : ""}<div class="msg-text">${esc(m.text || "")}</div></div>`;
}

function emptyHTML() {
  return `<div class="ag-empty">
    <span class="ag-empty-mark"><i class="ph ph-robot" aria-hidden="true"></i></span>
    <p class="ag-empty-title">Агент работает руками, а не только отвечает</p>
    <p class="ag-empty-sub">Сам сходит в Moodle, соберёт дедлайны, прочитает файл,
       сделает конспект или лабу. Каждый шаг видно здесь — что делает и почему.</p>
    <div class="ag-caps">
      <span class="ag-cap"><i class="ph ph-graduation-cap"></i>Moodle</span>
      <span class="ag-cap"><i class="ph ph-book-open-text"></i>конспекты</span>
      <span class="ag-cap"><i class="ph ph-globe-hemisphere-west"></i>интернет</span>
      <span class="ag-cap"><i class="ph ph-terminal-window"></i>файлы и команды</span>
      <span class="ag-cap"><i class="ph ph-bell"></i>напоминания</span>
    </div>
    <p class="ag-empty-hint">Опасные действия (сдача работы в Moodle) из веба
       не выполняются — только через бота в Telegram.</p>
  </div>`;
}

function fmtMs(ms) {
  if (!ms) return "";
  if (ms < 1000) return `${ms} мс`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(s < 10 ? 1 : 0)} с`;
  const m = Math.floor(s / 60);
  return `${m} мин ${Math.round(s - m * 60)} с`;
}

/* ---------- отрисовка треда ---------- */
export function renderAgent({ keepScroll = false } = {}) {
  const host = $("#ai-agent");
  if (!host) return;
  const key = state.ai.ctxKey;
  const list = store.get(key);
  if (!list.length) {
    host.innerHTML = emptyHTML();
    renderQuick();
    return;
  }
  let html = "";
  list.forEach((m, i) => {
    html += m.role === "user" ? userHTML(m) : runHTML(m, i);
  });
  host.innerHTML = html;
  renderQuick();
  bind();
  if (!keepScroll) host.scrollTop = host.scrollHeight;
}

function renderQuick() {
  const host = $("#ai-quick");
  if (!host || !agentMode()) return;
  if (store.get(state.ai.ctxKey).length) { host.replaceChildren(); return; }
  host.innerHTML = QUICK.map((q) =>
    `<button type="button" class="ag-quick" data-q="${esc(q.t)}">
       <i class="ph ${q.i}" aria-hidden="true"></i><span>${esc(q.t)}</span></button>`).join("");
}

/* ---------- живой прогон ---------- */
function nearBottom(el) {
  return el.scrollHeight - el.scrollTop - el.clientHeight < 110;
}

function makeRunEl() {
  const wrap = document.createElement("div");
  wrap.innerHTML = runHTML({ status: "running", steps: [] }, -1);
  return wrap.firstElementChild;
}

function addStepEl(runEl, ev) {
  const list = runEl.querySelector(".ag-steps");
  const host = list || document.createElement("ol");
  if (!list) { host.className = "ag-steps"; runEl.querySelector(".ag-run-head").after(host); }
  const tmp = document.createElement("div");
  tmp.innerHTML = stepHTML({
    name: ev.name, label: ev.label, group: ev.group,
    argsSummary: ev.args_summary, status: "running",
  });
  const el = tmp.firstElementChild;
  // появление с задержкой от предыдущих — эффект «очереди»
  const n = host.children.length;
  el.style.animationDelay = prefersReduced() ? "0ms" : `${Math.min(n, 6) * 45}ms`;
  host.appendChild(el);
  return el;
}

function finishStepEl(el, ev) {
  el.dataset.status = ev.ok ? "ok" : "error";
  const main = el.querySelector(".ag-step-main");
  if (ev.preview && main && !main.querySelector(".ag-step-preview")) {
    const p = document.createElement("div");
    p.className = "ag-step-preview";
    p.textContent = ev.preview;
    main.appendChild(p);
  }
  const tail = el.querySelector(".ag-step-tail");
  if (tail) {
    tail.innerHTML = (ev.ok
      ? `<i class="ph ph-check" title="готово"></i>`
      : `<i class="ph ph-warning-circle" title="ошибка"></i>`)
      + (ev.ms ? `<span class="ag-step-ms">${fmtMs(ev.ms)}</span>` : "");
  }
  el.classList.add("is-done");
}

function updateHead(runEl, { steps = 0, ms = 0, running = true, error = false, turns = 0 }) {
  const head = runEl.querySelector(".ag-run-head");
  if (!head) return;
  const meta = [];
  if (steps) meta.push(`${steps} ${plural(steps, "действие", "действия", "действий")}`);
  if (ms) meta.push(fmtMs(ms));
  if (turns > 1) meta.push(`${turns} ${plural(turns, "шаг", "шага", "шагов")}`);
  head.querySelector(".ag-run-meta").textContent = meta.join(" · ");
  const st = head.querySelector(".ag-run-status");
  if (running) return;
  st.innerHTML = error
    ? `<i class="ph ph-warning-circle"></i><span class="ag-run-word">не вышло</span>`
    : `<i class="ph ph-check-circle"></i><span class="ag-run-word">готово</span>`;
  runEl.classList.remove("is-running");
}

function ensureReason(runEl) {
  let box = runEl.querySelector(".ag-reason");
  // разметка могла прийти пустой заготовкой — тогда подменяем на полную
  if (!box || !box.querySelector(".ag-reason-text")) {
    const tmp = document.createElement("div");
    tmp.innerHTML = reasonHTML("");
    const fresh = tmp.firstElementChild;
    if (box) box.replaceWith(fresh);
    else {
      const steps = runEl.querySelector(".ag-steps");
      if (steps) steps.after(fresh);
      else runEl.querySelector(".ag-run-head").after(fresh);
    }
    box = fresh;
  }
  box.hidden = false;
  return box;
}

function ensureAnswer(runEl) {
  let body = runEl.querySelector(".ag-answer");
  if (!body) {
    const tmp = document.createElement("div");
    tmp.innerHTML = `<div class="ag-answer"></div>`;
    body = tmp.firstElementChild;
    runEl.appendChild(body);
  }
  return body;
}

/** Запустить агента. */
export async function agentSend(text, { quote = null } = {}) {
  const question = String(text || "").trim();
  if (!question) return;
  if (state.ai.busy) { toast("Агент ещё работает — дождись или нажми «стоп»"); return; }

  const key = state.ai.ctxKey;
  const ctx = state.ai.ctx;
  const host = $("#ai-agent");
  if (!host) return;

  const wasEmpty = !store.get(key).length;
  if (wasEmpty) host.innerHTML = "";

  store.push(key, { role: "user", text: question, quote, ts: Date.now() });
  const u = document.createElement("div");
  u.innerHTML = userHTML({ text: question, quote });
  host.appendChild(u.firstElementChild);

  const runEl = makeRunEl();
  runEl.classList.add("is-running");
  host.appendChild(runEl);
  host.scrollTop = host.scrollHeight;
  $("#ai-quick")?.replaceChildren();

  const t0 = Date.now();
  let steps = 0;
  let turns = 0;
  let answer = "";
  let reasoning = "";
  let draft = "";
  let error = "";
  const stepEls = new Map();
  const artifacts = [];
  let reasonOpen = true;
  let pinned = true;

  const answerBody = ensureAnswer(runEl);
  const paintAnswer = throttleRAF(() => {
    answerBody.innerHTML = renderMD(answer);
    if (pinned) host.scrollTop = host.scrollHeight;
  });

  const draftBody = document.createElement("div");
  draftBody.className = "ag-draft";
  draftBody.hidden = true;
  runEl.querySelector(".ag-answer").before(draftBody);
  const paintDraft = throttleRAF(() => {
    draftBody.hidden = !draft.trim();
    draftBody.innerHTML = renderMD(draft);
    if (pinned) host.scrollTop = host.scrollHeight;
  });

  const tick = setInterval(() => {
    updateHead(runEl, { steps, ms: Date.now() - t0, running: true, turns });
    if (pinned) host.scrollTop = host.scrollHeight;
  }, 500);

  const ac = new AbortController();
  state.ai.abort = ac;
  state.ai.busy = true;
  setBusyUI(true);

  const hist = store.get(key).slice(0, -1)
    .filter((m) => m.role === "user" || m.answer)
    .slice(-6)
    .map((m) => ({ role: m.role === "user" ? "user" : "assistant", content: m.answer || m.text }));

  const onScroll = () => { pinned = nearBottom(host); };
  host.addEventListener("scroll", onScroll, { passive: true });

  try {
    await streamSSE("/api/agent/stream", {
      question,
      session: sessionId(),
      history: hist,
      context: { kind: ctx.kind, id: ctx.id, cmid: ctx.cmid, title: ctx.title },
    }, {
      signal: ac.signal,
      onEvent: (ev) => {
        switch (ev.type) {
          case "turn":
            turns = ev.n || turns;
            updateHead(runEl, { steps, ms: Date.now() - t0, running: true, turns });
            break;

          case "reasoning": {
            reasoning += ev.text;
            const box = ensureReason(runEl);
            const t = box.querySelector(".ag-reason-text");
            const c = box.querySelector(".ag-reason-count");
            t.textContent = reasoning;
            c.textContent = `${reasoning.length} зн.`;
            if (pinned) host.scrollTop = host.scrollHeight;
            break;
          }

          case "reasoning_done":
            reasoning = ev.text || reasoning;
            // размышления закончились — сворачиваем, чтобы не мешали читать
            if (reasonOpen) {
              const box = runEl.querySelector(".ag-reason");
              if (box && reasoning.length > 400) {
                box.querySelector(".ag-reason-toggle")?.setAttribute("aria-expanded", "false");
                box.querySelector(".ag-reason-body").hidden = true;
                reasonOpen = false;
              }
            }
            break;

          case "draft":
            draft += ev.text;
            paintDraft();
            break;

          case "tool_start": {
            const el = addStepEl(runEl, ev);
            stepEls.set(ev.id, el);
            steps += 1;
            syncStepsFold(runEl, steps);
            updateHead(runEl, { steps, ms: Date.now() - t0, running: true, turns });
            if (pinned) host.scrollTop = host.scrollHeight;
            break;
          }

          case "tool_end": {
            const el = stepEls.get(ev.id) || runEl.querySelector('.ag-step[data-status="running"]');
            if (el) finishStepEl(el, ev);
            break;
          }

          case "delta":
            answer += ev.text;
            paintAnswer();
            break;

          // напечатанное оказалось преамбулой перед инструментами —
          // уводим в блок «рассуждает вслух», освобождая место ответу
          case "demote":
            if (answer.trim()) {
              draft += (draft ? "\n\n" : "") + answer.trim();
              answer = "";
              paintDraft();
              paintAnswer();
            }
            break;

          case "artifact": {
            artifacts.push(ev);
            let box = runEl.querySelector(".ag-artifacts");
            if (!box) {
              const tmp = document.createElement("div");
              tmp.innerHTML = `<div class="ag-artifacts"></div>`;
              box = tmp.firstElementChild;
              runEl.appendChild(box);
            }
            const a = document.createElement("div");
            a.innerHTML = artifactHTML(ev);
            box.appendChild(a.firstElementChild);
            if (pinned) host.scrollTop = host.scrollHeight;
            break;
          }

          case "done":
            answer = ev.answer || answer;
            turns = ev.turns || turns;
            paintAnswer();
            break;

          // сервер честно предупреждает, что остановился по времени/лимиту
          case "notice": {
            const n = document.createElement("div");
            n.className = "ag-notice";
            n.innerHTML = `<i class="ph ph-info" aria-hidden="true"></i><span>${esc(ev.text)}</span>`;
            runEl.querySelector(".ag-answer")?.before(n);
            if (pinned) host.scrollTop = host.scrollHeight;
            break;
          }

          case "error":
            error = ev.error || "Ошибка";
            break;
        }
      },
    });
  } catch (e) {
    if (e.name !== "AbortError") error = e.message || "Не удалось выполнить";
  } finally {
    clearInterval(tick);
    host.removeEventListener("scroll", onScroll);
    const ms = Date.now() - t0;

    if (error) {
      const b = ensureAnswer(runEl);
      b.innerHTML = `<div class="ag-error"><i class="ph ph-warning-circle"></i>
        <span>${esc(error)}</span>
        <button class="ag-act" data-act="again" data-i="-1">
          <i class="ph ph-arrow-clockwise"></i>Ещё раз</button></div>`;
    }
    updateHead(runEl, { steps, ms, running: false, error: !!error, turns });
    runEl.classList.remove("is-running");
    runEl.dataset.i = String(store.get(key).length);

    store.push(key, {
      role: "agent", answer, reasoning, draft, steps: collectSteps(runEl),
      artifacts, error, ms, turns, ts: Date.now(),
    });
    if (error && !answer) toast(error);

    // финальная перерисовка — чтобы кнопки действий и разметка были каноничными
    renderAgent({ keepScroll: true });
    if (pinned) host.scrollTop = host.scrollHeight;
    if (wasEmpty) renderQuick();

    state.ai.busy = false;
    state.ai.abort = null;
    setBusyUI(false);
  }
}

/** Собираем шаги из DOM — состояние уже отрисовано, дублировать логику незачем. */
function collectSteps(runEl) {
  return [...runEl.querySelectorAll(".ag-step")].map((el) => ({
    group: el.dataset.group,
    label: el.querySelector(".ag-step-label")?.textContent || "",
    argsSummary: el.querySelector(".ag-step-args")?.textContent || "",
    preview: el.querySelector(".ag-step-preview")?.textContent || "",
    status: el.dataset.status,
    ms: parseMs(el.querySelector(".ag-step-ms")?.textContent || ""),
  }));
}

function parseMs(s) {
  const n = parseFloat(s);
  if (!n) return 0;
  if (s.includes("мс")) return n;
  if (s.includes("мин")) return Math.round(n * 60000);
  return Math.round(n * 1000);
}

export function agentStop() {
  if (state.ai.abort) {
    state.ai.abort.abort();
    state.ai.abort = null;
  }
}

function setBusyUI(busy) {
  const sendBtn = $(".ai-send[type=submit]");
  const stopBtn = $("#ai-stop");
  if (stopBtn) stopBtn.hidden = !busy;
  if (sendBtn) sendBtn.hidden = busy;
  $("#ai-pane")?.classList.toggle("is-busy", busy);
  $("#ai-fab")?.classList.toggle("thinking", busy);
  $("#ai-bar-dots")?.toggleAttribute("hidden", !busy);
}

function sessionId() {
  let id = null;
  try { id = localStorage.getItem("konspect.session"); } catch { /* noop */ }
  if (!id) {
    id = Math.random().toString(36).slice(2) + Date.now().toString(36);
    try { localStorage.setItem("konspect.session", id); } catch { /* noop */ }
  }
  return id;
}

/* ---------- действия ---------- */
function bind() {
  const host = $("#ai-agent");
  if (!host) return;

  host.onclick = async (e) => {
    // «ещё N действий» — разворачиваем длинный список
    const more = e.target.closest(".ag-more");
    if (more) {
      const list = more.previousElementSibling;
      if (list?.classList.contains("ag-steps")) {
        const open = list.classList.toggle("is-open");
        more.textContent = open
          ? "свернуть"
          : `ещё ${list.children.length - FOLD_AFTER} ${
            plural(list.children.length - FOLD_AFTER, "действие", "действия", "действий")}`;
      }
      return;
    }

    // размышления
    const rt = e.target.closest(".ag-reason-toggle");
    if (rt) {
      const box = rt.closest(".ag-reason");
      const body = box.querySelector(".ag-reason-body");
      const open = rt.getAttribute("aria-expanded") === "true";
      rt.setAttribute("aria-expanded", String(!open));
      body.hidden = open;
      if (!open) {
        body.style.maxHeight = body.scrollHeight + "px";
        setTimeout(() => { body.style.maxHeight = ""; }, 260);
      }
      return;
    }

    const btn = e.target.closest(".ag-act");
    if (!btn) return;
    const i = +btn.dataset.i;
    const list = store.get(state.ai.ctxKey);
    const msg = list[i];
    const act = btn.dataset.act;

    if (act === "copy") {
      await copyText(msg?.answer || "");
      return;
    }
    if (act === "note") {
      const cid = state.ai.ctx.kind === "conspect" ? state.ai.ctx.id : null;
      notes.add(cid, msg?.answer || "", state.ai.ctx.title || "Агент");
      toast("Сохранено в заметки");
      return;
    }
    if (act === "again") {
      const q = msg?.q || [...list].reverse().find((m) => m.role === "user")?.text;
      if (!q) return;
      if (i >= 0) {
        store.runs[state.ai.ctxKey] = list.slice(0, i);
        lsSet(KEY, store.runs);
        renderAgent();
      }
      agentSend(q);
    }
  };
}

/* ---------- инициализация ---------- */
export function initAgent() {
  try {
    state.ai.mode = localStorage.getItem("konspect.ai.mode") || "chat";
  } catch { state.ai.mode = "chat"; }

  document.querySelectorAll("[data-ai-mode]").forEach((b) => {
    b.addEventListener("click", () => setMode(b.dataset.aiMode, { focus: true }));
  });

  $("#ai-agent-new")?.addEventListener("click", () => {
    store.clear(state.ai.ctxKey);
    resetAgentMemory();
    renderAgent();
    toast("Новый диалог с агентом");
  });

  window.addEventListener("ai-mode-changed", () => {
    if (agentMode()) renderAgent();
  });

  setMode(state.ai.mode);
  renderAgent();
}

/** Сбросить память агента на сервере (при «новом чате»). */
export async function resetAgentMemory() {
  try {
    await fetch("/api/agent/reset", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session: sessionId() }),
    });
  } catch { /* не критично */ }
}

/** Новый диалог: чистим и локальную ленту, и память агента на сервере. */
export function newAgentChat() {
  store.clear(state.ai.ctxKey);
  resetAgentMemory();
  renderAgent();
}

/** Последний ответ агента в текущем контексте (для свёрнутой полосы). */
export function lastAgentRun() {
  const list = store.get(state.ai.ctxKey);
  for (let i = list.length - 1; i >= 0; i--) {
    if (list[i].role === "agent") return list[i];
  }
  return null;
}
