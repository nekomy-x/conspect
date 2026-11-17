/* ============================================================
   reader.js — читалка конспекта.

   Что нового против прежней версии:
   • TOC строится по ВСЕМ подразделам (h3/h4 = md `##`/`###`), включая
     третий уровень, — раньше `###` в оглавление не попадали вовсе;
   • id разделов совпадают с индексами из /api/conspects.sections,
     поэтому ссылка из дерева и поиска ведёт точно в нужное место;
   • подсветка активного раздела — через IntersectionObserver;
   • «липкая» шапка документа, прогресс чтения, возврат к позиции;
   • на узких экранах TOC превращается в дискложер, а не исчезает.
   ============================================================ */
"use strict";

import { $, $$, esc, debounce, toast, copyText, plural } from "./util.js";
import { state, read, lastRead, pins, prefs } from "./store.js";
import { api } from "./api.js";
import { onCleanup } from "./lifecycle.js";
import { toggleAi, isMobile } from "./panes.js";
import { setActive, conspectById } from "./tree.js";
import { setAiContext, openAiFor, quoteAsk } from "./ai.js";
import { showQuiz, showSummary, showFlashcards, showNotes, showTranscript, closeModal } from "./tools.js";

/* ---------- скелетон под финальную раскладку ---------- */
function readerSkeleton() {
  return `<div class="reader">
    <aside class="toc"><div class="skeleton sk-line" style="width:80%"></div>
      ${'<div class="skeleton sk-line" style="width:65%"></div>'.repeat(5)}</aside>
    <div class="doc">
      <div class="skeleton sk-title" style="width:60%"></div>
      <div class="skeleton sk-line" style="width:35%"></div>
      <div style="height:18px"></div>
      ${'<div class="skeleton sk-line" style="width:95%"></div>'.repeat(3)}
      <div style="height:14px"></div>
      ${'<div class="skeleton sk-line" style="width:88%"></div>'.repeat(4)}
    </div>
  </div>`;
}

/* ---------- основной рендер ---------- */
export async function renderConspect(cid, secArg = null) {
  const content = $("#content");
  content.innerHTML = readerSkeleton();

  let d = state.docCache.get(cid);
  if (!d) {
    try {
      d = await api(`/api/conspect/${encodeURIComponent(cid)}`);
      state.docCache.set(cid, d);
    } catch (e) {
      content.innerHTML = `<div class="page"><div class="empty">
        <i class="ph ph-file-x empty-ph"></i>${esc(e.message || "Конспект не найден")}
        <div style="margin-top:16px"><a class="btn" href="#/conspects">К библиотеке</a></div>
      </div></div>`;
      return;
    }
  }

  // гарантируем, что список конспектов подгружен (нужен дереву и «соседям»)
  const meta = conspectById(cid);
  if (!meta) await ensureConspects();

  const doc = document.createElement("div");
  doc.innerHTML = d.html;

  // id разделам — в порядке документа, ровно как в /api/conspects.sections
  const heads = $$("h3, h4", doc);
  const titleNorm = (d.title || "").replace(/\s+/g, " ").trim();
  const toc = [];
  heads.forEach((h, i) => {
    h.id = `sec-${i}`;
    const text = h.textContent.replace(/\s+/g, " ").trim();
    if (i === 0 && h.tagName === "H3" && text === titleNorm) {
      h.remove();               // дубль заголовка — прячем, индекс сохраняем
      return;
    }
    const isH2 = h.tagName === "H3";
    toc.push({ i, text, lvl: isH2 ? 2 : 3 });
  });

  const meta2 = conspectById(cid) || {};
  const pinned = pins.has(cid);
  const mark = read.get(cid);

  content.innerHTML = `
    <div class="read-bar" id="read-bar" style="width:0"></div>
    <div class="doc-head">
      ${d.course ? `<div class="doc-course">${esc(d.course)}</div>` : ""}
      <div class="doc-title">${esc(d.title)}</div>
      <div class="doc-meta">
        <span class="badge mut">${d.size_kb} КБ</span>
        <span class="badge mut">${meta2.reading_min || 1} мин чтения</span>
        ${d.n_slides ? `<span class="badge ok">${d.n_slides} слайдов</span>` : ""}
        <span class="badge mut">${(meta2.sections || toc).length} ${plural((meta2.sections || toc).length, "раздел", "раздела", "разделов")}</span>
        ${mark ? `<span class="badge done">читал${mark.raw >= 0.9 ? " до конца" : ` ${Math.round(mark.raw * 100)}%`}</span>` : ""}
      </div>
      <div class="doc-actions">
        ${d.n_slides ? `<a class="btn" href="#/slides/${esc(cid)}"><i class="ph ph-slides"></i> Слайды</a>` : ""}
        <button class="btn" id="btn-quiz" type="button"><i class="ph ph-question"></i> Квиз</button>
        <button class="btn" id="btn-summary" type="button"><i class="ph ph-text-aa"></i> Выжимка</button>
        ${d.has_transcript ? `<button class="btn" id="btn-transcript" type="button"><i class="ph ph-microphone"></i> Стенограмма</button>` : ""}
        <button class="btn" id="btn-notes" type="button"><i class="ph ph-notebook"></i> Заметки</button>
        <button class="btn" id="btn-pin" type="button" aria-pressed="${pinned}">
          <i class="ph${pinned ? "-fill" : ""} ph-push-pin"></i> ${pinned ? "Закреплено" : "Закрепить"}</button>
        <a class="btn" href="/media/conspects/${encodeURIComponent(cid)}/conspect.md" download>
          <i class="ph ph-download-simple"></i> Скачать MD</a>
      </div>
      <p class="sel-hint"><b>Выдели любой текст</b> — спрошу по нему в чате рядом</p>
    </div>

    <div class="reader-body">
      <details class="toc-disc" id="toc-disc">
        <summary><i class="ph ph-list-dashes" aria-hidden="true"></i> Содержание
          <span class="meta">${toc.length}</span></summary>
        <nav class="toc toc-inline" aria-label="Содержание лекции">
          ${toc.map((t) => `<a href="#sec-${t.i}" class="${t.lvl === 3 ? "lvl3" : ""}" data-sec="${t.i}">${esc(t.text)}</a>`).join("")}
        </nav>
      </details>
      <aside class="toc" id="toc" aria-label="Содержание лекции">
        <div class="toc-title">Содержание</div>
        <div class="toc-links">
        ${toc.map((t) => `<a href="#sec-${t.i}" class="${t.lvl === 3 ? "lvl3" : ""}" data-sec="${t.i}">${esc(t.text)}</a>`).join("")}
        </div>
      </aside>
      <article class="doc-body" id="doc-body">${doc.innerHTML}</article>
    </div>

    <button class="to-top" id="to-top" type="button" title="Наверх" aria-label="Наверх" hidden>
      <i class="ph ph-arrow-up" aria-hidden="true"></i>
    </button>`;

  // цель для подсветки активного раздела
  const targets = toc
    .map((t) => ({ t, el: $(`#doc-body #sec-${t.i}`) }))
    .filter((x) => x.el);

  setupStickyHeader(content);
  setupTocSpy(targets);
  setupProgress(content, cid, targets);
  setupSelection(d);
  setupActions(d, cid, meta2);

  // куда скроллить: явный раздел из ссылки → сохранённая позиция → начало
  const sec = secArg != null ? +secArg : null;
  if (sec != null && !Number.isNaN(sec)) {
    requestAnimationFrame(() => scrollToSection(sec, false));
    state.activeSec = sec;
  } else {
    restorePosition(cid, content);
  }
  setActive(cid, sec);

  // приветственный контекст чата
  setAiContext({ kind: "conspect", id: cid, title: d.title });
  // в читалке ИИ-панель открыта всегда — но на мобиле не выезжаем сами
  if (!isMobile() && !prefs.aiOpen) toggleAi(true, false);
  lastRead.set(cid, d.title, sec, 0);
}

async function ensureConspects() {
  if (state.conspects) return;
  try {
    const r = await api("/api/conspects");
    state.conspects = r.items || [];
  } catch { state.conspects = []; }
}

export function scrollToSection(i, smooth = true) {
  const el = $(`#doc-body #sec-${i}`);
  if (!el) return;
  const content = $("#content");
  const top = el.getBoundingClientRect().top - content.getBoundingClientRect().top
    + content.scrollTop - 24;
  content.scrollTo({ top, behavior: smooth ? "smooth" : "auto" });
}

/* ---------- липкая шапка ---------- */
function setupStickyHeader(content) {
  const head = $(".doc-head");
  if (!head) return;
  const sentinel = document.createElement("div");
  sentinel.className = "sticky-sentinel";
  head.after(sentinel);
  const io = new IntersectionObserver(([e]) => {
    $(".main")?.classList.toggle("scrolled", !e.isIntersecting);
    const crumbs = $("#crumbs");
    crumbs?.classList.toggle("compact", !e.isIntersecting);
  }, { root: content, threshold: 0 });
  io.observe(sentinel);
  onCleanup(() => io.disconnect());
}

/* ---------- подсветка активного раздела ---------- */
function setupTocSpy(targets) {
  if (!targets.length) return;
  const content = $("#content");
  const links = $$(".toc a[data-sec]");
  let current = targets[0].t.i;

  const setCur = (i) => {
    if (i === current) return;
    current = i;
    links.forEach((l) => l.classList.toggle("active", +l.dataset.sec === i));
    const active = links.find?.((l) => +l.dataset.sec === i);
    if (active && active.offsetParent) active.scrollIntoView({ block: "nearest" });
  };

  const io = new IntersectionObserver(() => {
    // ближайший заголовок выше отметки 25% высоты окна
    const mark = content.getBoundingClientRect().top + Math.min(200, content.clientHeight * 0.25);
    let best = targets[0];
    for (const t of targets) {
      if (t.el.getBoundingClientRect().top <= mark) best = t;
    }
    setCur(best.t.i);
  }, { root: content, rootMargin: "0px 0px -60% 0px", threshold: [0, 1] });

  targets.forEach((t) => io.observe(t.el));
  setCur(current);
  onCleanup(() => io.disconnect());
}

/* ---------- прогресс чтения ---------- */
function setupProgress(content, cid, targets) {
  const bar = $("#read-bar");
  const toTop = $("#to-top");
  let lastSaved = 0;

  const onScroll = () => {
    const h = content.scrollHeight - content.clientHeight;
    const pct = h > 0 ? Math.min(1, content.scrollTop / h) : 1;
    if (bar) bar.style.width = `${(pct * 100).toFixed(1)}%`;
    if (toTop) toTop.hidden = content.scrollTop < 400;

    const now = Date.now();
    if (now - lastSaved > 1200) {
      lastSaved = now;
      read.set(cid, pct, state.activeSec);
      lastRead.set(cid, conspectById(cid)?.title || "", state.activeSec, pct);
    }
  };

  // запоминаем ТОЧНУЮ позицию при уходе со страницы
  const saveOnLeave = () => {
    const h = content.scrollHeight - content.clientHeight;
    const pct = h > 0 ? Math.min(1, content.scrollTop / h) : 1;
    read.set(cid, pct, state.activeSec);
    lastRead.set(cid, conspectById(cid)?.title || "", state.activeSec, pct);
  };

  content.addEventListener("scroll", onScroll, { passive: true });
  toTop?.addEventListener("click", () => content.scrollTo({ top: 0, behavior: "smooth" }));
  window.addEventListener("beforeunload", saveOnLeave);
  onCleanup(() => {
    content.removeEventListener("scroll", onScroll);
    window.removeEventListener("beforeunload", saveOnLeave);
    saveOnLeave();
  });
  onScroll();
}

function restorePosition(cid, content) {
  const mark = read.get(cid);
  const l = lastRead.get();
  if (!mark || !l || l.cid !== cid || !mark.raw || mark.raw < 0.05) return;
  const h = content.scrollHeight - content.clientHeight;
  if (h <= 0) return;
  const top = Math.round(h * mark.raw);
  requestAnimationFrame(() => {
    content.scrollTo({ top, behavior: "auto" });
    const hint = document.createElement("button");
    hint.className = "resume-hint";
    hint.type = "button";
    hint.innerHTML = `<i class="ph ph-map-pin-line" aria-hidden="true"></i>
      Вы остановились здесь · ${Math.round(mark.raw * 100)}% — <b>в начало</b>`;
    hint.addEventListener("click", () => {
      content.scrollTo({ top: 0, behavior: "smooth" });
      hint.remove();
    });
    content.appendChild(hint);
    setTimeout(() => hint.remove(), 5200);
  });
}

/* ---------- выделение → ИИ ---------- */
function setupSelection(d) {
  const popover = $("#ai-popover");
  let hideTimer = null;
  let lastSelection = null;

  const getSelectionText = () => {
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed) return null;
    const t = sel.toString().trim();
    if (t.length < 4 || t.length > 2000) return null;
    const anchor = sel.anchorNode;
    const el = anchor && (anchor.nodeType === 1 ? anchor : anchor.parentElement);
    if (!el || !$("#doc-body")?.contains(el)) return null;
    return t;
  };

  const showPopover = () => {
    const t = getSelectionText();
    if (!t) return;
    lastSelection = t;
    const rect = window.getSelection().getRangeAt(0).getBoundingClientRect();
    popover.hidden = false;
    const pw = popover.offsetWidth || 240;
    let x = rect.left + rect.width / 2 - pw / 2;
    x = Math.max(10, Math.min(x, window.innerWidth - pw - 10));
    let y = rect.top - popover.offsetHeight - 10;
    if (y < 60) y = rect.bottom + 10;
    popover.style.left = `${x}px`;
    popover.style.top = `${y}px`;
  };

  const onSelChange = debounce(() => {
    if (getSelectionText()) showPopover();
    else popover.hidden = true;
  }, 220);
  const onScrollAll = () => { popover.hidden = true; };

  document.addEventListener("selectionchange", onSelChange);
  document.addEventListener("scroll", onScrollAll, { passive: true, capture: true });

  const onClickPopover = (e) => {
    const btn = e.target.closest("[data-mode]");
    if (!btn) return;
    popover.hidden = true;
    const text = lastSelection;
    if (!text) return;
    window.getSelection()?.removeAllRanges();
    const mode = btn.dataset.mode;

    if (mode === "copy") { copyText(text); return; }
    if (mode === "flash") { showFlashcards(text, d.id); return; }

    const prompts = {
      explain: "Поясни подробнее этот фрагмент.",
      simple: "Объясни этот фрагмент простыми словами, как первокурснику.",
      exam: "Составь по этому фрагменту вопросы, которые могут быть на экзамене, и коротко ответь на них.",
    };
    openAiFor({ kind: "conspect", id: d.id, title: d.title });
    quoteAsk(text, prompts[mode] || prompts.explain);
  };

  popover.addEventListener("click", onClickPopover);

  // «пояснить» по Enter при активном выделении
  const onKey = (e) => {
    if (e.key !== "Enter" || popover.hidden) return;
    if (!lastSelection) return;
    e.preventDefault();
    openAiFor({ kind: "conspect", id: d.id, title: d.title });
    quoteAsk(lastSelection, "Поясни подробнее этот фрагмент.");
    popover.hidden = true;
    window.getSelection()?.removeAllRanges();
  };
  document.addEventListener("keydown", onKey);

  onCleanup(() => {
    document.removeEventListener("selectionchange", onSelChange);
    document.removeEventListener("scroll", onScrollAll, { capture: true });
    popover.removeEventListener("click", onClickPopover);
    document.removeEventListener("keydown", onKey);
    popover.hidden = true;
  });
}

/* ---------- кнопки документа ---------- */
function setupActions(d, cid, meta) {
  $("#btn-quiz")?.addEventListener("click", () => showQuiz(cid));
  $("#btn-summary")?.addEventListener("click", () => showSummary(cid));
  $("#btn-transcript")?.addEventListener("click", () => showTranscript(cid, d.title));
  $("#btn-notes")?.addEventListener("click", () => showNotes(cid, d.title));
  $("#btn-pin")?.addEventListener("click", (e) => {
    const on = pins.toggle(cid);
    const b = e.currentTarget;
    b.setAttribute("aria-pressed", String(on));
    b.innerHTML = `<i class="ph${on ? "-fill" : ""} ph-push-pin"></i> ${on ? "Закреплено" : "Закрепить"}`;
    toast(on ? "Закреплено в дереве" : "Откреплено");
    window.dispatchEvent(new CustomEvent("tree-refresh"));
  });

  // копирование кода
  $("#doc-body")?.addEventListener("click", (e) => {
    const b = e.target.closest(".copy-btn");
    if (b) copyText(b.dataset.copy || "");
  });
}

/* ---------- соседние лекции (в порядке курса, как в дереве) ---------- */
export function neighbours(cid) {
  if (!state.conspects) return { prev: null, next: null };
  const course = conspectById(cid)?.course || "";
  const num = (t) => {
    const m = String(t || "").match(/(\d+)/);
    return m ? +m[1] : 999;
  };
  const list = state.conspects
    .filter((c) => (c.course || "") === course)
    .sort((a, b) => (num(a.title) - num(b.title)) || (a.mtime - b.mtime));
  const i = list.findIndex((c) => c.id === cid);
  return { prev: i > 0 ? list[i - 1] : null, next: i >= 0 && i < list.length - 1 ? list[i + 1] : null };
}

export { closeModal };