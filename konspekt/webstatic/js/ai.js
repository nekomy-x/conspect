/* ============================================================
   ai.js — ИИ-панель: треды по контексту, стриминг, цитаты, действия.

   Главное отличие от прежней версии: чат больше НЕ привязан к «одному
   открытому конспекту» и не сбрасывается при навигации. У каждого
   контекста (лекция / задание / вся учёба) свой тред, который живёт
   между переходами и перезагрузками.
   ============================================================ */
"use strict";

import { $, $$, esc, toast, copyText, throttleRAF, timeAgo } from "./util.js";
import { state, threads, notes } from "./store.js";
import { streamAI, postJSON } from "./api.js";
import { renderMD } from "./md.js";
import { isMobile, toggleAi, closeDrawers } from "./panes.js";
import { agentMode, agentSend, agentStop, setMode, renderAgent, newAgentChat, lastAgentRun } from "./agent.js";

const STEPS = ["ищу в конспекте", "читаю раздел", "формулирую ответ", "шлифую формулировки"];

/* ---------- контекст ---------- */
export function ctxKey(ctx) {
  if (!ctx) return "global";
  if (ctx.kind === "conspect" && ctx.id) return `conspect:${ctx.id}`;
  if (ctx.kind === "assignment" && ctx.cmid) return `assignment:${ctx.cmid}`;
  return "global";
}

export function currentCtx() {
  return state.ai.ctx;
}

/** Переключить контекст чата (не открывая панель). */
export function setAiContext(ctx, { render = true } = {}) {
  state.ai.ctx = ctx;
  state.ai.ctxKey = ctxKey(ctx);
  const name = $("#ai-ctx-name");
  if (name) name.textContent = ctx.title || "Вся учёба";
  if (render) { renderThread(); if (agentMode()) renderAgent(); }
  refreshBar();
}

/** Открыть панель сразу в нужном контексте. */
export function openAiFor(ctx) {
  if (ctx) setAiContext(ctx);
  toggleAi(true);
  setTimeout(() => $("#ai-input")?.focus(), 120);
}

export function toggleAiPane() {
  toggleAi();
}

/* ---------- отрисовка сообщения ---------- */
function msgHTML(m, i) {
  if (m.role === "user") {
    return `<div class="msg user" data-i="${i}">
      ${m.quote ? `<div class="msg-quote"><i class="ph ph-quotes" aria-hidden="true"></i>
        <span>${esc(m.quote)}</span></div>` : ""}
      <div class="msg-text">${m.text ? esc(m.text) : ""}</div>
    </div>`;
  }
  if (m.error) {
    return `<div class="msg assistant error" data-i="${i}">
      <div class="msg-text">${esc(m.text || "Не получилось")}</div>
      <div class="msg-actions"><button class="msg-act" data-act="retry" data-i="${i}">
        <i class="ph ph-arrow-clockwise"></i> Попробовать снова</button></div>
    </div>`;
  }
  return `<div class="msg assistant" data-i="${i}">
    ${m.reasoning ? `<div class="msg-reason is-done" data-role="reason">
      <i class="ph ph-brain" aria-hidden="true"></i>
      <div class="msg-reason-body">${esc(m.reasoning)}</div>
    </div>` : ""}
    <div class="msg-body">${renderMD(m.text || "")}</div>
    <div class="msg-actions">
      <button class="msg-act" data-act="copy" data-i="${i}" title="Скопировать ответ">
        <i class="ph ph-clipboard-text"></i><span>Копировать</span></button>
      <button class="msg-act" data-act="regen" data-i="${i}" title="Сгенерировать заново">
        <i class="ph ph-arrow-clockwise"></i><span>Ещё раз</span></button>
      <button class="msg-act" data-act="shorter" data-i="${i}" title="Сделать короче">
        <i class="ph ph-arrows-in-simple"></i><span>Короче</span></button>
      <button class="msg-act" data-act="note" data-i="${i}" title="Сохранить в заметки">
        <i class="ph ph-notebook"></i><span>В заметки</span></button>
    </div>
  </div>`;
}

export function renderThread({ keepScroll = false } = {}) {
  const chat = $("#ai-chat");
  if (!chat) return;
  const key = state.ai.ctxKey;
  const msgs = threads.get(key);

  if (!msgs.length) {
    chat.innerHTML = emptyHTML();
    setQuick();
    return;
  }
  chat.innerHTML = msgs.map(msgHTML).join("");
  bindChat();
  if (!keepScroll) {
    const saved = threads.scroll(key);
    chat.scrollTop = saved > 0 && saved < chat.scrollHeight - chat.clientHeight
      ? saved : chat.scrollHeight;
  }
}

function emptyHTML() {
  const ctx = state.ai.ctx;
  const title = ctx.kind === "conspect" ? "этой лекции"
    : ctx.kind === "assignment" ? "этого задания" : "твоей учёбы";
  return `<div class="ai-empty">
    <span class="ai-empty-mark">к</span>
    <p>Спрошу что угодно по материалу ${title} — отвечу, опираясь на конспект,
       а не на выдумки.</p>
    <p class="ai-empty-hint">Выдели текст в лекции — и он попадёт сюда цитатой.</p>
  </div>`;
}

/* ---------- быстрые вопросы ---------- */
function quickQuestions() {
  const k = state.ai.ctx.kind;
  if (k === "conspect") return [
    "Что главное в этой лекции?",
    "Объясни простыми словами",
    "Какие термины нужно выучить?",
    "Составь 5 вопросов к экзамену",
  ];
  if (k === "assignment") return [
    "Что здесь требуется сделать?",
    "Разбери условие по шагам",
    "Проверь мой черновик ответа",
    "Какие типичные ошибки тут делают?",
  ];
  return [
    "Что у меня на завтра?",
    "Что горит по срокам?",
    "По каким темам есть конспекты?",
    "Составь план подготовки на неделю",
  ];
}

function setQuick() {
  const host = $("#ai-quick");
  if (!host) return;
  if (agentMode()) return;   // у агента свои быстрые задачи (agent.js)
  host.innerHTML = quickQuestions()
    .map((q) => `<button type="button" data-q="${esc(q)}">${esc(q)}</button>`).join("");
}

/* ---------- пуск ответа ---------- */
function pendingHTML() {
  return `<div class="msg assistant pending">
    <div class="ai-steps" data-elapsed="0">
      <span class="ai-step-dot"></span>
      <span class="ai-step-text">${STEPS[0]}</span>
      <span class="ai-step-time">0 с</span>
    </div>
  </div>`;
}

function startSteps(el) {
  const t0 = Date.now();
  let step = 0;
  const textEl = $(".ai-step-text", el);
  const timeEl = $(".ai-step-time", el);
  const timer = setInterval(() => {
    const sec = Math.floor((Date.now() - t0) / 1000);
    if (timeEl) timeEl.textContent = `${sec} с`;
    const next = Math.min(STEPS.length - 1, Math.floor(sec / 4));
    if (next !== step && textEl) { step = next; textEl.textContent = STEPS[step]; }
  }, 500);
  return () => clearInterval(timer);
}

function nearBottom(chat) {
  return chat.scrollHeight - chat.scrollTop - chat.clientHeight < 90;
}

/**
 * Отправить вопрос. ctx — контекст (по умолчанию текущий).
 * quote — необязательная цитата выделенного фрагмента.
 */
export async function send(text, { quote = null, ctxOverride = null, history } = {}) {
  const question = String(text || "").trim();
  if (!question) return;
  if (state.ai.busy) { toast("Дождись ответа или нажми «стоп»"); return; }

  const ctx = ctxOverride || state.ai.ctx;
  if (ctxOverride) setAiContext(ctxOverride, { render: false });
  const key = state.ai.ctxKey;

  const chat = $("#ai-chat");
  const wasEmpty = !threads.get(key).length;
  if (wasEmpty) chat.innerHTML = "";

  threads.push(key, { role: "user", text: question, quote, ts: Date.now() });
  const userEl = document.createElement("div");
  userEl.innerHTML = msgHTML({ role: "user", text: question, quote }, 0);
  chat.appendChild(userEl.firstElementChild);

  const pend = document.createElement("div");
  pend.innerHTML = pendingHTML();
  const pendEl = pend.firstElementChild;
  chat.appendChild(pendEl);
  chat.scrollTop = chat.scrollHeight;
  $("#ai-quick")?.replaceChildren();

  const stopSteps = startSteps(pendEl);

  // размышления модели — отдельный приглушённый блок: это НЕ ответ,
  // поэтому показываем их тихо и сворачиваем, как только пошёл текст
  const thinkEl = document.createElement("div");
  thinkEl.className = "msg-reason";
  thinkEl.hidden = true;
  let think = "";
  const paintThink = throttleRAF(() => {
    thinkEl.hidden = !think.trim();
    thinkEl.innerHTML = `<i class="ph ph-brain" aria-hidden="true"></i>
      <div class="msg-reason-body">${esc(think.trim())}</div>`;
    if (pinned) chat.scrollTop = chat.scrollHeight;
  });

  const ac = new AbortController();
  state.ai.abort = ac;
  state.ai.busy = true;
  setBusyUI(true);
  refreshBar();

  // история для модели: всё, кроме только что добавленного вопроса
  const hist = history || threads.get(key).slice(0, -1)
    .map((m) => ({ role: m.role, content: m.quote ? `«${m.quote}» ${m.text}` : m.text }));

  const payload = {
    question: quote ? `Поясни фрагмент из лекции: «${quote}»\n\nМой вопрос: ${question}` : question,
    context: { kind: ctx.kind, id: ctx.id, cmid: ctx.cmid },
    history: hist,
  };

  const body = document.createElement("div");
  body.className = "msg-body";
  const refresh = throttleRAF(() => {
    body.innerHTML = renderMD(acc);
    if (pinned) chat.scrollTop = chat.scrollHeight;
  });
  let acc = "";
  let pinned = true;

  const paint = (text) => {
    acc = text;
    refresh();
  };

  try {
    let full;
    try {
      full = await streamAI(payload, {
        signal: ac.signal,
        onStart: () => {
          stopSteps();
          pendEl.classList.remove("pending");
          if (!thinkEl.isConnected) pendEl.appendChild(thinkEl);
          pendEl.appendChild(body);
        },
        onReasoning: (piece) => { think += piece; paintThink(); },
        onDelta: (_piece, all) => {
          stopSteps();
          if (!thinkEl.isConnected) pendEl.appendChild(thinkEl);
          if (!body.isConnected) pendEl.appendChild(body);
          // пошёл ответ — размышления убираем с глаз, но не теряем
          if (think.trim()) thinkEl.classList.add("is-done");
          paint(all);
        },
      });
    } catch (e) {
      if (e.name === "AbortError") throw e;
      // стриминг недоступен — откатываемся на обычный запрос
      full = await fallbackAsk(payload, ac.signal, (partial) => paint(partial));
    }

    stopSteps();
    pendEl.remove();
    const answer = full || acc || "Пустой ответ";
    threads.push(key, {
      role: "assistant", text: answer, q: question, ts: Date.now(),
      reasoning: think.trim().slice(0, 1200) || undefined,
    });
    renderThread({ keepScroll: true });
    if (nearBottom(chat)) chat.scrollTop = chat.scrollHeight;
  } catch (e) {
    stopSteps();
    if (e.name === "AbortError") {
      const partial = acc.trim();
      if (partial) {
        threads.push(key, {
          role: "assistant", text: partial + "\n\n_— остановлено_", ts: Date.now(),
          reasoning: think.trim().slice(0, 1200) || undefined,
        });
      } else {
        toast("Остановлено");
      }
    } else {
      threads.push(key, { role: "assistant", text: e.message || "Ошибка", error: true,
                          q: question, ts: Date.now() });
    }
    renderThread({ keepScroll: true });
  } finally {
    state.ai.busy = false;
    state.ai.abort = null;
    setBusyUI(false);
    refreshBar();
    if (wasEmpty) setQuick();
  }
}

async function fallbackAsk(payload, signal, onPartial) {
  const r = await postJSON("/api/ai/ask", payload, signal);
  onPartial(r.answer || "");
  return r.answer || "";
}

export function stop() {
  if (agentMode()) { agentStop(); return; }
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
}

/* ---------- действия над ответом ---------- */
function bindChat() {
  const chat = $("#ai-chat");
  chat.onclick = async (e) => {
    const btn = e.target.closest(".msg-act");
    if (!btn) return;
    const key = state.ai.ctxKey;
    const msgs = threads.get(key);
    const i = +btn.dataset.i;
    const msg = msgs[i];
    const act = btn.dataset.act;

    if (act === "copy") {
      await copyText(msg.text || "");
      return;
    }
    if (act === "regen") {
      const q = msg.q || msgs.slice(0, i).reverse().find((m) => m.role === "user")?.text;
      if (!q) return;
      threads.truncateAfter(key, i);
      renderThread({ keepScroll: true });
      send(q, { history: threads.get(key).map((m) => ({ role: m.role, content: m.text })) });
      return;
    }
    if (act === "shorter") {
      send("Сделай ответ короче — только суть, без воды.", {
        history: msgs.map((m) => ({ role: m.role, content: m.text })),
      });
      return;
    }
    if (act === "note") {
      const cid = state.ai.ctx.kind === "conspect" ? state.ai.ctx.id : null;
      notes.add(cid, msg.text || "", state.ai.ctx.title || "Заметка");
      toast("Сохранено в заметки");
      return;
    }
    if (act === "retry") {
      const q = msg.q || msgs.slice(0, i).reverse().find((m) => m.role === "user")?.text;
      if (!q) return;
      threads.truncateAfter(key, i);
      renderThread({ keepScroll: true });
      send(q, { history: threads.get(key).map((m) => ({ role: m.role, content: m.text })) });
    }
  };

  chat.oninput = null;
}

/* ---------- цитата из выделения ---------- */
export function quoteAsk(quote, question) {
  const ctx = state.ai.ctx;
  const title = ctx.kind === "conspect" ? ctx.title : "Вся учёба";
  send(question, { quote, ctxOverride: { ...ctx, title } });
}

/* ---------- меню контекста ---------- */
function buildCtxMenu() {
  const menu = $("#ai-ctx-menu");
  const items = [{ key: "global", label: "Вся учёба", sub: "расписание, сроки, темы" }];

  if (state.ai.ctx.kind === "conspect" && state.ai.ctx.id) {
    items.push({
      key: `conspect:${state.ai.ctx.id}`,
      label: state.ai.ctx.title,
      sub: "текущая лекция",
      ctx: { kind: "conspect", id: state.ai.ctx.id, title: state.ai.ctx.title },
    });
  }
  if (state.ai.ctx.kind === "assignment" && state.ai.ctx.cmid) {
    items.push({
      key: `assignment:${state.ai.ctx.cmid}`,
      label: state.ai.ctx.title,
      sub: "текущее задание",
      ctx: { kind: "assignment", cmid: state.ai.ctx.cmid, title: state.ai.ctx.title },
    });
  }
  for (const c of (state.conspects || []).slice(0, 12)) {
    const k = `conspect:${c.id}`;
    if (items.some((x) => x.key === k)) continue;
    items.push({ key: k, label: c.title, sub: c.course || "лекция",
                 ctx: { kind: "conspect", id: c.id, title: c.title } });
  }

  menu.innerHTML = items.map((it) => `
    <button class="ai-ctx-item${it.key === state.ai.ctxKey ? " is-active" : ""}"
            role="option" aria-selected="${it.key === state.ai.ctxKey}" data-key="${esc(it.key)}">
      <span class="ai-ctx-item-label">${esc(it.label)}</span>
      <span class="ai-ctx-item-sub">${esc(it.sub)}</span>
    </button>`).join("");

  menu.onclick = (e) => {
    const b = e.target.closest(".ai-ctx-item");
    if (!b) return;
    const item = items.find((x) => x.key === b.dataset.key);
    setAiContext(item?.ctx || { kind: "global", title: "Вся учёба" });
    closeCtxMenu();
    $("#ai-input")?.focus();
  };
}

function openCtxMenu() {
  buildCtxMenu();
  const menu = $("#ai-ctx-menu");
  menu.hidden = false;
  $("#ai-ctx")?.setAttribute("aria-expanded", "true");
}
function closeCtxMenu() {
  $("#ai-ctx-menu").hidden = true;
  $("#ai-ctx")?.setAttribute("aria-expanded", "false");
}

/* ---------- полоса, когда панель свёрнута ---------- */
export function refreshBar() {
  const bar = $("#ai-bar");
  if (!bar) return;
  const open = $("#app")?.classList.contains("ai-open")
    || $("#app")?.classList.contains("ai-sheet-open");
  const msgs = threads.get(state.ai.ctxKey);
  const last = msgs[msgs.length - 1];
  const agentRun = agentMode() ? lastAgentRun() : null;
  const worth = state.ai.busy
    || (last && last.role === "assistant" && !last.error)
    || (agentRun && agentRun.answer && !agentRun.error);
  bar.hidden = !!open || !worth;
  const dots = $("#ai-bar-dots");
  if (dots) {
    dots.hidden = !state.ai.busy;
    dots.textContent = state.ai.busy
      ? (agentMode() ? "агент работает…" : "готовит ответ…") : "";
  }
  const text = $(".ai-bar-text", bar);
  if (text && !state.ai.busy) {
    const title = (state.ai.ctx.title || "").slice(0, 24);
    text.textContent = agentMode()
      ? `Агент: ${title || "задача"}`
      : (last ? `Ответ по «${title}»` : "ИИ-ассистент");
  }
}

/* ---------- инициализация ---------- */
export function initAi() {
  const form = $("#ai-form");
  form?.addEventListener("submit", (e) => {
    e.preventDefault();
    const inp = $("#ai-input");
    const v = inp.value.trim();
    if (!v) return;
    inp.value = "";
    // режим «Агент» — та же строка ввода, но задача уходит агенту
    if (agentMode()) { agentSend(v); return; }
    send(v);
  });

  $("#ai-input")?.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && state.ai.busy) { e.preventDefault(); stop(); }
    if (e.key === "ArrowUp" && !$("#ai-input").value) {
      const msgs = threads.get(state.ai.ctxKey);
      const lastUser = [...msgs].reverse().find((m) => m.role === "user");
      if (lastUser) { e.preventDefault(); $("#ai-input").value = lastUser.text; }
    }
  });

  $("#ai-stop")?.addEventListener("click", () => stop());
  $("#ai-fab")?.addEventListener("click", () => {
    toggleAi();
    if (!$("#app").classList.contains("ai-sheet-open")) setTimeout(() => $("#ai-input")?.focus(), 60);
  });

  $("#ai-ctx")?.addEventListener("click", (e) => {
    e.stopPropagation();
    $("#ai-ctx-menu").hidden ? openCtxMenu() : closeCtxMenu();
  });
  document.addEventListener("click", (e) => {
    if (!e.target.closest("#ai-ctx-menu") && !e.target.closest("#ai-ctx")) closeCtxMenu();
  });

  $("#ai-new")?.addEventListener("click", () => {
    if (agentMode()) { newAgentChat(); toast("Новый диалог с агентом"); return; }
    threads.clear(state.ai.ctxKey);
    renderThread();
    toast("Начат новый чат в этом контексте");
  });

  $("#ai-quick")?.addEventListener("click", (e) => {
    const b = e.target.closest("[data-q]");
    if (b) send(b.dataset.q);
  });

  // запоминаем позицию скролла треда
  const chat = $("#ai-chat");
  chat?.addEventListener("scroll", throttleRAF(() => {
    if (!state.ai.busy) threads.saveScroll(state.ai.ctxKey, chat.scrollTop);
  }), { passive: true });

  window.addEventListener("layout-change", refreshBar);
  // смена режима: перерисовываем нужную ленту и обновляем подсказки
  window.addEventListener("ai-mode-changed", () => {
    renderThread();
    setQuick();
    refreshBar();
  });

  setAiContext({ kind: "global", title: "Вся учёба" }, { render: false });
  renderThread();
  refreshBar();
}