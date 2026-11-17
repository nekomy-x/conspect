/* ============================================================
   tree.js — левое дерево «Раздел → Лекция → Подраздел».

   Ключевое поведение (solo-режим): раскрыт ровно один раздел и одна
   лекция в нём; открыл другое — предыдущее схлопывается, но остаётся
   видимой цепочка до активной темы. В multi-режиме раскрытий много.
   ============================================================ */
"use strict";

import { $, $$, esc, debounce, plural, timeAgo, onIdle } from "./util.js";
import { state, prefs, savePrefs, read, pins, lastRead } from "./store.js";
import { prefetch } from "./api.js";

let expandedCourses = new Set();
let expandedLectures = new Set();
let filter = "";
let focusIndex = -1;

/* ---------- данные ---------- */
function lectureNumber(title) {
  const m = String(title || "").match(/(\d+)/);
  return m ? +m[1] : 999;
}

export function courseOf(cid) {
  const c = (state.conspects || []).find((x) => x.id === cid);
  return c ? (c.course || "Без курса") : null;
}

export function conspectById(cid) {
  return (state.conspects || []).find((c) => c.id === cid) || null;
}

/** Разделы → лекции. Лекции внутри курса — по номеру, потом по дате. */
export function buildTree() {
  const groups = new Map();
  for (const c of state.conspects || []) {
    const key = c.course || "Без курса";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(c);
  }
  for (const list of groups.values()) {
    list.sort((a, b) =>
      (lectureNumber(a.title) - lectureNumber(b.title)) || (a.mtime - b.mtime));
  }
  return groups;
}

/* ---------- «продолжить» ---------- */
function continueHTML() {
  const l = lastRead.get();
  if (!l || !l.cid) return "";
  const c = conspectById(l.cid);
  if (!c) return "";
  const mark = read.get(l.cid);
  const pct = l.pct ? Math.round(l.pct * 100) : Math.round((mark?.raw ?? 0) * 100);
  const href = `#/conspect/${encodeURIComponent(l.cid)}${l.sec != null ? `/${l.sec}` : ""}`;
  return `
  <a class="continue-card" href="${href}">
    <div class="continue-top">
      <i class="ph ph-bookmark-simple" aria-hidden="true"></i>
      <span>Продолжить чтение</span>
      ${pct > 0 ? `<span class="continue-pct">${pct}%</span>` : ""}
    </div>
    <div class="continue-title">${esc(l.title || c.title)}</div>
    <div class="continue-meta">${esc(c.course || "без курса")} · ${timeAgo((l.ts || 0) / 1000)}</div>
    ${pct > 0 ? `<div class="continue-bar"><i style="width:${pct}%"></i></div>` : ""}
  </a>`;
}

/* ---------- узел лекции ---------- */
function dotClass(cid) {
  return { done: " is-done", progress: " is-progress", new: "" }[read.state(cid)] || "";
}

function lectNode(c, isNew) {
  const open = expandedLectures.has(c.id) || !!filter;
  const secs = c.sections || [];
  const secLinks = secs.map((s, i) => `
    <div class="tree-node tree-node-sec" role="treeitem" aria-level="3">
      <div class="tree-row tree-row-sec${prefs.treeMode === "solo" ? " lvl2" : ""}">
        <a class="tree-link" href="#/conspect/${encodeURIComponent(c.id)}/${i}"
           title="${esc(s.title)}">
          <span class="tree-sec-mark" aria-hidden="true"></span>
          <span class="tree-name">${esc(s.title)}</span>
        </a>
      </div>
    </div>`).join("");

  return `
  <div class="tree-node tree-node-lect${isNew ? " is-new" : ""}" role="treeitem"
       aria-level="2" aria-expanded="${open}" data-cid="${esc(c.id)}">
    <div class="tree-row tree-row-lect">
      ${secs.length
        ? `<button class="tree-chev" type="button" data-act="toggle-lect" aria-hidden="true" tabindex="-1">
             <i class="ph ph-caret-right"></i></button>`
        : `<span class="tree-chev is-empty" aria-hidden="true"></span>`}
      <span class="tree-dot${dotClass(c.id)}" aria-hidden="true"></span>
      <a class="tree-link" href="#/conspect/${encodeURIComponent(c.id)}" title="${esc(c.title)}">
        <span class="tree-name">${esc(c.title)}</span>
        <span class="tree-meta">
          ${c.n_slides ? `${c.n_slides} сл · ` : ""}${c.reading_min || 1} мин
        </span>
      </a>
      <button class="tree-pin${pins.has(c.id) ? " on" : ""}" type="button" data-act="pin"
              title="${pins.has(c.id) ? "Открепить" : "Закрепить"}"
              aria-label="${pins.has(c.id) ? "Открепить лекцию" : "Закрепить лекцию"}">
        <i class="ph${pins.has(c.id) ? "-fill" : ""} ph-push-pin" aria-hidden="true"></i>
      </button>
    </div>
    <div class="tree-kids" role="group"${open ? "" : " hidden"}>${secLinks}</div>
  </div>`;
}

/* ---------- группа курса ---------- */
function courseNode(name, list) {
  const q = filter.toLowerCase();
  const matches = (c) => !q
    || c.title.toLowerCase().includes(q)
    || name.toLowerCase().includes(q)
    || (c.sections || []).some((s) => s.title.toLowerCase().includes(q));

  const shown = list.filter(matches);
  if (!shown.length) return "";

  const open = expandedCourses.has(name) || !!filter;
  const active = state.activeConspect;
  const hasActive = shown.some((c) => c.id === active);

  return `
  <div class="tree-node tree-node-course${hasActive && !open ? " has-active" : ""}" role="treeitem"
       aria-level="1" aria-expanded="${open}" data-course="${esc(name)}">
    <div class="tree-row tree-row-course">
      <button class="tree-chev" type="button" data-act="toggle-course" aria-hidden="true" tabindex="-1">
        <i class="ph ph-caret-right"></i>
      </button>
      <span class="tree-course-name">${esc(name)}</span>
      <span class="tree-count">${shown.length}</span>
    </div>
    <div class="tree-kids" role="group"${open ? "" : " hidden"}>
      ${shown.map((c) => lectNode(c, Date.now() / 1000 - c.mtime < 48 * 3600)).join("")}
    </div>
  </div>`;
}

/* ---------- сборка ---------- */
export function renderTree() {
  const host = $("#tree");
  const cont = $("#tree-continue");
  if (!host) return;

  // ничего не раскрыто (первый заход или данные приехали позже инициализации) —
  // раскрываем раздел активной темы, иначе последнего чтения, иначе первый
  if (state.conspects?.length && !expandedCourses.size && !filter) {
    const groups = buildTree();
    const l = lastRead.get();
    const anchor = state.activeConspect || l?.cid;
    const preferred = anchor ? courseOf(anchor) : null;
    const first = [...groups.keys()].sort((a, b) => a.localeCompare(b, "ru"))[0];
    const pick = (preferred && groups.has(preferred)) ? preferred : first;
    if (pick) {
      expandedCourses.add(pick);
      const anchorCid = anchor && courseOf(anchor) === pick ? anchor : null;
      if (anchorCid && (state.activeSec != null || l?.sec != null)) {
        expandedLectures.add(anchorCid);
      }
    }
  }

  if (cont) cont.innerHTML = filter ? "" : continueHTML();

  if (!state.conspects) {
    host.innerHTML = `<div class="tree-skeleton">
      ${'<div class="skeleton sk-line" style="width:70%"></div>'.repeat(4)}
    </div>`;
    return;
  }
  if (!state.conspects.length) {
    host.innerHTML = `<div class="tree-empty">
      <i class="ph ph-book-open" aria-hidden="true"></i>
      <p>Конспектов пока нет.</p>
      <p class="tree-empty-sub">Пришли боту в Telegram ссылку на запись лекции — он сделает конспект сам.</p>
    </div>`;
    return;
  }

  const groups = buildTree();
  const q = filter.toLowerCase();
  const pinnedItems = state.conspects.filter((c) => pins.has(c.id) && (!q
    || c.title.toLowerCase().includes(q) || (c.course || "").toLowerCase().includes(q)));

  const pinnedBlock = !filter && pinnedItems.length
    ? `<div class="tree-node tree-node-course is-pinned-group" role="treeitem" aria-level="1" aria-expanded="true">
         <div class="tree-row tree-row-course is-static">
           <span class="tree-chev is-empty" aria-hidden="true"></span>
           <span class="tree-course-name">Закреплённые</span>
           <span class="tree-count">${pinnedItems.length}</span>
         </div>
         <div class="tree-kids" role="group">
           ${pinnedItems.map((c) => {
             const secs = c.sections || [];
             return `<div class="tree-node tree-node-lect" role="treeitem" aria-level="2"
                          aria-expanded="false" data-cid="${esc(c.id)}">
               <div class="tree-row tree-row-lect">
                 <span class="tree-chev is-empty" aria-hidden="true"></span>
                 <span class="tree-dot${dotClass(c.id)}" aria-hidden="true"></span>
                 <a class="tree-link" href="#/conspect/${encodeURIComponent(c.id)}" title="${esc(c.title)}">
                   <span class="tree-name">${esc(c.title)}</span>
                   <span class="tree-meta">${esc(c.course || "без курса")}${secs.length ? ` · ${secs.length} разд.` : ""}</span>
                 </a>
                 <button class="tree-pin on" type="button" data-act="pin" title="Открепить" aria-label="Открепить лекцию">
                   <i class="ph-fill ph-push-pin" aria-hidden="true"></i>
                 </button>
               </div>
             </div>`;
           }).join("")}
         </div>
       </div>`
    : "";

  const body = [...groups.entries()]
    .sort(([a], [b]) => a.localeCompare(b, "ru"))
    .map(([name, list]) => courseNode(name, list))
    .join("");

  if (!body && !pinnedBlock) {
    host.innerHTML = `<div class="tree-empty">
      <i class="ph ph-magnifying-glass" aria-hidden="true"></i>
      <p>Ничего не нашлось.</p>
      <p class="tree-empty-sub">Попробуй другое слово или сбрось фильтр (Esc).</p>
    </div>`;
    return;
  }

  host.innerHTML = pinnedBlock + body;
  syncChevrons();
}

function syncChevrons() {
  $$(".tree-node", $("#tree")).forEach((n) => {
    const open = n.getAttribute("aria-expanded") === "true";
    const kids = $(":scope > .tree-kids", n);
    if (kids) kids.hidden = !open;
    const chev = $(":scope > .tree-row > .tree-chev", n);
    if (chev && !chev.classList.contains("is-empty")) chev.classList.toggle("open", open);
  });

  // после перерисовки возвращаем подсветку активной темы
  paintActive();
}

/* ---------- раскрытие ---------- */
function collapseOthers(keep) {
  if (prefs.treeMode !== "solo") return;
  for (const c of [...expandedCourses]) if (c !== keep) expandedCourses.delete(c);
  for (const l of [...expandedLectures]) {
    const owner = courseOf(l);
    if (owner !== keep) expandedLectures.delete(l);
  }
}

export function toggleCourse(name, force) {
  const open = force ?? !expandedCourses.has(name);
  if (open) {
    collapseOthers(name);
    expandedCourses.add(name);
  } else {
    expandedCourses.delete(name);
  }
  renderTree();
  focusNode(`[data-course="${cssEscape(name)}"]`);
}

export function toggleLecture(cid, force) {
  const open = force ?? !expandedLectures.has(cid);
  if (open) {
    if (prefs.treeMode === "solo") {
      const owner = courseOf(cid);
      collapseOthers(owner);
      expandedCourses.add(owner);
      for (const l of [...expandedLectures]) if (l !== cid) expandedLectures.delete(l);
    }
    expandedLectures.add(cid);
  } else {
    expandedLectures.delete(cid);
  }
  renderTree();
  focusNode(`[data-cid="${cssEscape(cid)}"]`);
}

const cssEscape = (s) => String(s).replace(/["\\]/g, "\\$&");

/* ---------- активная тема ---------- */
/* Подсветка активной лекции/подраздела. Отдельно от setActive, потому что
   вызывается и после перерисовки дерева (данные могли приехать позже). */
function paintActive({ scroll = false } = {}) {
  const host = $("#tree");
  if (!host) return;
  $$(".tree-node-lect.is-active, .tree-row-sec.is-active", host).forEach((el) =>
    el.classList.remove("is-active"));

  const cid = state.activeConspect;
  if (!cid) return;
  const node = $(`.tree-node-lect[data-cid="${cssEscape(cid)}"]`, host);
  if (!node) return;                     // дерево ещё пустое — подсветим после загрузки
  node.classList.add("is-active");

  const sec = state.activeSec;
  if (sec != null) {
    $$(".tree-row-sec", node)[+sec]?.classList.add("is-active");
  }
  if (scroll) {
    const row = (sec != null ? $(".tree-row-sec.is-active", node) : null)
      || $(".tree-row-lect", node);
    row?.scrollIntoView({ block: "nearest" });
  }
}

export function setActive(cid, sec = null) {
  state.activeConspect = cid;
  state.activeSec = sec;
  if (!cid) { paintActive(); return; }

  const course = courseOf(cid);
  if (course && !expandedCourses.has(course)) {
    expandedCourses.add(course);
    collapseOthers(course);
  }
  if (sec != null && !expandedLectures.has(cid)) expandedLectures.add(cid);

  renderTree();
  paintActive({ scroll: true });
}

/* ---------- клавиатура ---------- */
function rows() {
  return $$(".tree-node:not([hidden]) .tree-row", $("#tree"))
    .filter((r) => r.offsetParent !== null && r.closest(".tree-kids:not([hidden])") !== null
      || r.closest(".tree-node-course") === r.parentElement && !r.parentElement.hidden);
}

function focusNode(selector) {
  const node = $(selector, $("#tree"));
  if (!node) return;
  $$(".tree-node", $("#tree")).forEach((n) => n.setAttribute("tabindex", "-1"));
  node.setAttribute("tabindex", "0");
  node.focus({ preventScroll: true });
}

function visibleNodes() {
  const out = [];
  const walk = (host) => {
    $$(":scope > .tree-node", host).forEach((n) => {
      out.push(n);
      const kids = $(":scope > .tree-kids", n);
      if (kids && !kids.hidden) walk(kids);
    });
  };
  walk($("#tree"));
  return out;
}

function onKey(e) {
  const nodes = visibleNodes();
  if (!nodes.length) return;
  let idx = nodes.indexOf(document.activeElement.closest(".tree-node"));
  const cur = nodes[idx];
  const act = (el, f) => { f(); focusNode(`[data-cid="${cssEscape(el.dataset.cid)}"]`); };

  switch (e.key) {
    case "ArrowDown":
      e.preventDefault();
      focusNode2(nodes[Math.min(nodes.length - 1, idx + 1)]);
      break;
    case "ArrowUp":
      e.preventDefault();
      focusNode2(nodes[Math.max(0, idx - 1)]);
      break;
    case "ArrowRight": {
      if (!cur) break;
      e.preventDefault();
      if (cur.dataset.course) toggleCourse(cur.dataset.course, true);
      else if (cur.dataset.cid) {
        if (!expandedLectures.has(cur.dataset.cid)) toggleLecture(cur.dataset.cid, true);
        else focusNode2(nodes[idx + 1] || cur);
      }
      break;
    }
    case "ArrowLeft": {
      if (!cur) break;
      e.preventDefault();
      if (cur.dataset.course) toggleCourse(cur.dataset.course, false);
      else if (cur.dataset.cid && expandedLectures.has(cur.dataset.cid)) toggleLecture(cur.dataset.cid, false);
      else {
        const owner = courseOf(state.activeConspect);
        const parent = nodes.find((n) => n.dataset.course && n.dataset.course === cur.dataset.course);
        if (parent) focusNode2(parent);
      }
      break;
    }
    case "Home": e.preventDefault(); focusNode2(nodes[0]); break;
    case "End": e.preventDefault(); focusNode2(nodes[nodes.length - 1]); break;
    case "Enter":
    case " ": {
      if (!cur) break;
      const link = $(".tree-link", cur);
      if (link) { e.preventDefault(); window.location.hash = link.getAttribute("href").slice(1); }
      break;
    }
    case "m": {
      if (!cur?.dataset.cid) break;
      e.preventDefault();
      const on = pins.toggle(cur.dataset.cid);
      renderTree();
      focusNode2($(`.tree-node-lect[data-cid="${cssEscape(cur.dataset.cid)}"]`, $("#tree")));
      break;
    }
    default: break;
  }
}

function focusNode2(node) {
  if (!node) return;
  $$(".tree-node", $("#tree")).forEach((n) => n.setAttribute("tabindex", "-1"));
  node.setAttribute("tabindex", "0");
  node.focus({ preventScroll: true });
  node.scrollIntoView({ block: "nearest" });
}

/* ---------- клики ---------- */
function onClick(e) {
  const actBtn = e.target.closest("[data-act]");
  const node = e.target.closest(".tree-node");
  if (!node) return;

  if (actBtn) {
    const act = actBtn.dataset.act;
    e.preventDefault();
    e.stopPropagation();
    if (act === "toggle-course") toggleCourse(node.dataset.course);
    else if (act === "toggle-lect") toggleLecture(node.dataset.cid);
    else if (act === "pin") {
      pins.toggle(node.dataset.cid);
      renderTree();
      focusNode2($(`.tree-node-lect[data-cid="${cssEscape(node.dataset.cid)}"]`, $("#tree")));
    }
    return;
  }

  // клик по строке вне ссылки — ведём себя как ссылка
  if (e.target.closest(".tree-link")) {
    if (prefs.treeMode === "solo" && node.dataset.cid) toggleLecture(node.dataset.cid, true);
    return;
  }
  const link = $(".tree-link", node);
  if (link) window.location.hash = link.getAttribute("href").slice(1);
  else if (node.dataset.course) toggleCourse(node.dataset.course);
}

function onHover(e) {
  const link = e.target.closest(".tree-link");
  if (!link) return;
  const m = link.getAttribute("href")?.match(/^#\/conspect\/([^/]+)/);
  if (m) onIdle(() => prefetch(`/api/conspect/${encodeURIComponent(decodeURIComponent(m[1]))}`));
}

/* ---------- инициализация ---------- */
export function initTree() {
  const host = $("#tree");
  if (!host) return;

  host.addEventListener("click", onClick);
  host.addEventListener("keydown", onKey);
  host.addEventListener("mouseover", onHover);
  host.addEventListener("focusin", (e) => {
    const node = e.target.closest(".tree-node");
    if (node) focusNode2(node);
  });

  const input = $("#tree-filter");
  const apply = debounce((v) => {
    filter = v.trim();
    renderTree();
  }, 120);
  input?.addEventListener("input", (e) => apply(e.target.value));
  input?.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      e.stopPropagation();
      input.value = "";
      filter = "";
      renderTree();
      input.blur();
    }
    if (e.key === "ArrowDown") {
      e.preventDefault();
      const first = visibleNodes()[0];
      if (first) focusNode2(first);
    }
  });

  const modeBtn = $("#tree-mode");
  const syncMode = () => {
    modeBtn?.setAttribute("aria-pressed", String(prefs.treeMode === "solo"));
    modeBtn?.setAttribute("title", prefs.treeMode === "solo"
      ? "Открыт один раздел за раз — нажми, чтобы оставлять открытыми несколько"
      : "Открыто может быть несколько разделов");
  };
  modeBtn?.addEventListener("click", () => {
    prefs.treeMode = prefs.treeMode === "solo" ? "multi" : "solo";
    savePrefs();
    syncMode();
    renderTree();
  });
  syncMode();

  // раскрыть раздел, где пользователь остановился
  const l = lastRead.get();
  if (l?.cid) {
    const course = courseOf(l.cid);
    if (course) expandedCourses.add(course);
    if (l.sec != null) expandedLectures.add(l.cid);
    state.activeConspect = l.cid;
  }
  renderTree();
}

export function treeStats() {
  const n = (state.conspects || []).length;
  const secs = (state.conspects || []).reduce((s, c) => s + (c.sections || []).length, 0);
  return `${n} ${plural(n, "лекция", "лекции", "лекций")} · ${secs} подразделов`;
}