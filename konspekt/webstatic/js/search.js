/* ============================================================
   search.js — командная палитра (⌘K).

   Было: 4 пункта навигации + названия конспектов.
   Стало: разделы, лекции, ПОДРАЗДЕЛЫ, задания, пары, команды,
   история запросов и «продолжить чтение» — с нечётким матчем,
   скоупами и переходом прямо к подразделу.
   ============================================================ */
"use strict";

import { $, $$, esc, debounce, trapFocus, plural } from "./util.js";
import { state, read, lastRead, prefs, savePrefs } from "./store.js";
import { toggleAi, toggleTree } from "./panes.js";
import { send as aiSend, openAiFor } from "./ai.js";

const HISTORY_KEY = "konspect.search.hist.v1";

let items = [];
let sel = 0;
let untrap = null;
let hist = (() => {
  try { return JSON.parse(localStorage.getItem(HISTORY_KEY) || "[]"); } catch { return []; }
})();

function saveHist(q) {
  if (!q || q.length < 2) return;
  hist = [q, ...hist.filter((x) => x !== q)].slice(0, 6);
  try { localStorage.setItem(HISTORY_KEY, JSON.stringify(hist)); } catch { /* noop */ }
}

/* ---------- нечёткий матч ---------- */
function fuzzy(needle, hay) {
  const n = needle.toLowerCase();
  const h = String(hay || "").toLowerCase();
  if (!n) return { score: 1, pos: [] };

  const direct = h.indexOf(n);
  if (direct >= 0) {
    const pos = [...Array(n.length)].map((_, i) => direct + i);
    return { score: 100 - direct + (direct === 0 ? 40 : 0) + n.length, pos };
  }

  let hi = 0, score = 0, streak = 0;
  const pos = [];
  for (const ch of n) {
    const found = h.indexOf(ch, hi);
    if (found < 0) return null;
    pos.push(found);
    streak = found === hi ? streak + 1 : 0;
    score += 2 + streak;
    hi = found + 1;
  }
  return { score, pos };
}

function mark(text, pos) {
  if (!pos || !pos.length) return esc(text);
  const set = new Set(pos);
  let out = "", inMark = false;
  for (let i = 0; i < text.length; i++) {
    const on = set.has(i);
    if (on && !inMark) { out += "<mark>"; inMark = true; }
    if (!on && inMark) { out += "</mark>"; inMark = false; }
    out += esc(text[i]);
  }
  return out + (inMark ? "</mark>" : "");
}

/* ---------- скоупы ---------- */
function parseQuery(raw) {
  const q = raw.trim();
  const m = q.match(/^(к|k|з|z|п|p|>|@)\s*:?\s*(.*)$/i);
  if (!m) return { scope: null, q };
  const scope = { "к": "conspects", "k": "conspects", "з": "assign", "z": "assign",
                  "п": "sched", "p": "sched", ">": "cmd", "@": "cmd" }[m[1].toLowerCase()];
  return { scope, q: m[2].trim() };
}

const SCOPE_LABELS = {
  conspects: "конспекты", assign: "задания", sched: "расписание", cmd: "команды",
};

/* ---------- сборка индекса ---------- */
function buildItems() {
  const out = [];

  const nav = [
    ["Главная", "#/", "домой, сегодня, дедлайны", "ph-house"],
    ["Библиотека конспектов", "#/conspects", "все лекции сеткой", "ph-squares-four"],
    ["Задания", "#/assignments", "дедлайны, лабы", "ph-check-square"],
    ["Расписание", "#/schedule", "пары на 4 недели", "ph-calendar-blank"],
  ];
  for (const [label, href, sub, ico] of nav) {
    out.push({ group: "Разделы", label, sub, ico, scope: "nav",
               go: () => { location.hash = href; } });
  }

  for (const c of (state.conspects || [])) {
    const st = read.state(c.id);
    out.push({
      group: "Лекции", scope: "conspects",
      label: c.title, sub: c.course || "без курса",
      ico: "ph-book-open-text",
      meta: `${c.reading_min || 1} мин${st === "done" ? " · прочитано" : st === "progress" ? " · в процессе" : ""}`,
      go: () => { location.hash = `#/conspect/${encodeURIComponent(c.id)}`; },
    });
    (c.sections || []).forEach((s, i) => {
      out.push({
        group: "Подразделы", scope: "conspects", sub: true,
        label: s.title, sub: c.title,
        ico: s.level === 2 ? "ph-text-h-two" : "ph-text-h-three",
        go: () => { location.hash = `#/conspect/${encodeURIComponent(c.id)}/${i}`; },
      });
    });
  }

  for (const a of (state.assignments || [])) {
    out.push({
      group: "Задания", scope: "assign",
      label: a.name, sub: a.course || "задание",
      ico: "ph-pencil-simple-line",
      meta: a.deadline || "",
      go: () => { location.hash = `#/assignment/${a.cmid}`; },
    });
  }

  for (const d of (state.schedule || [])) {
    if (!d.items?.length) continue;
    for (const it of d.items) {
      out.push({
        group: "Пары", scope: "sched",
        label: it.title, sub: `${d.weekday}, ${d.date.slice(8)}.${d.date.slice(5, 7)}`,
        ico: "ph-clock", meta: it.time,
        go: () => { location.hash = "#/schedule"; },
      });
    }
  }

  const cmds = [
    ["Спросить ИИ", "ph-sparkle", () => openAiFor(null)],
    ["Свернуть/развернуть ИИ-панель", "ph-sidebar-simple-flip", () => toggleAi()],
    ["Свернуть/развернуть дерево тем", "ph-tree-structure", () => toggleTree()],
    ["Открывать один раздел за раз", "ph-arrows-in-line-vertical", () => {
      prefs.treeMode = prefs.treeMode === "solo" ? "multi" : "solo"; savePrefs();
      location.hash = location.hash || "#/";
      window.dispatchEvent(new CustomEvent("tree-mode-change"));
    }],
    ["Сменить тему", "ph-moon-stars", () => $("#theme-toggle")?.click()],
    ["Горячие клавиши", "ph-keyboard", () => $("#help-btn")?.click()],
    ["Выйти", "ph-sign-out", () => { location.href = "/logout"; }],
  ];
  for (const [label, ico, go] of cmds) {
    out.push({ group: "Команды", scope: "cmd", label, sub: "действие", ico, go });
  }

  return out;
}

function ensureIndex() {
  if (!items.length) items = buildItems();
}
export const invalidateIndex = () => { items = []; };

/* ---------- открытие/закрытие ---------- */
export function openCmdk(prefill = "") {
  ensureIndex();
  const ov = $("#cmdk-overlay");
  ov.hidden = false;
  const inp = $("#cmdk-input");
  inp.value = prefill;
  update(prefill);
  untrap = trapFocus($(".cmdk"), closeCmdk);
  setTimeout(() => inp.focus(), 20);
}

export function closeCmdk() {
  $("#cmdk-overlay").hidden = true;
  if (untrap) { untrap(); untrap = null; }
}

export const isCmdkOpen = () => !$("#cmdk-overlay").hidden;

/* ---------- результаты ---------- */
function update(raw) {
  ensureIndex();
  const { scope, q } = parseQuery(raw);
  const results = $("#cmdk-results");
  const needle = q.toLowerCase();

  let pool = items.filter((it) => !scope || it.scope === scope
    || (scope === "conspects" && it.scope === "nav" && false));

  // стартовый экран: продолжить + история
  if (!needle) {
    const extra = [];
    const l = lastRead.get();
    if (l?.cid) {
      extra.push({
        group: "Продолжить", label: l.title || "Продолжить чтение",
        sub: "вернуться к чтению", ico: "ph-bookmark-simple",
        go: () => { location.hash = `#/conspect/${encodeURIComponent(l.cid)}${l.sec != null ? `/${l.sec}` : ""}`; },
      });
    }
    if (!scope) {
      for (const h of hist) {
        extra.push({
          group: "История", label: h, sub: "недавний запрос", ico: "ph-clock-counter-clockwise",
          go: () => { $("#cmdk-input").value = h; update(h); $("#cmdk-input").focus(); },
        });
      }
      extra.push(...results.length ? [] : pool.filter((i) => i.scope === "nav"));
    } else {
      extra.push(...pool.filter((i) => i.group === "Команды" || i.scope === scope).slice(0, 40));
    }
    render(extra, "");
    return;
  }

  const scored = [];
  for (const it of pool) {
    const m1 = fuzzy(needle, it.label);
    const m2 = it.sub ? fuzzy(needle, it.sub) : null;
    if (!m1 && !m2) continue;
    const score = Math.max(m1?.score || 0, (m2?.score || 0) * 0.5)
      + (it.group === "Лекции" ? 6 : 0) + (it.sub ? -4 : 0);
    scored.push({ ...it, score, pos: m1?.pos || null });
  }
  scored.sort((a, b) => b.score - a.score);
  render(scored.slice(0, 40), needle);
}

function render(list, needle) {
  const results = $("#cmdk-results");
  const foot = $("#cmdk-foot");
  items.length; // индекс уже построен
  window.__cmdk = list;

  if (!list.length) {
    results.innerHTML = `<div class="cmdk-none">Ничего не нашлось</div>`;
    foot.innerHTML = "";
    sel = 0;
    return;
  }

  let html = "", lastGroup = null, idx = 0;
  for (const it of list) {
    it._i = idx++;
    if (it.group !== lastGroup) {
      html += `<div class="cmdk-group">${esc(it.group)}</div>`;
      lastGroup = it.group;
    }
    html += `
      <div class="cmdk-item${it._i === 0 ? " sel" : ""}" id="cmdk-item-${it._i}"
           role="option" aria-selected="${it._i === 0}" data-i="${it._i}">
        <span class="cmdk-ico"><i class="ph ${it.ico}" aria-hidden="true"></i></span>
        <span class="cmdk-body">
          <span class="cmdk-label">${mark(it.label, it.pos)}</span>
          <span class="cmdk-sub">${esc(it.sub || "")}</span>
        </span>
        ${it.meta ? `<span class="cmdk-meta">${esc(it.meta)}</span>` : ""}
      </div>`;
  }
  results.innerHTML = html;
  sel = 0;
  $("#cmdk-input").setAttribute("aria-activedescendant", "cmdk-item-0");

  foot.innerHTML = needle
    ? `<span>${list.length} ${plural(list.length, "результат", "результата", "результатов")}</span>
       <span class="cmdk-hint"><kbd>↑↓</kbd> выбор <kbd>↵</kbd> открыть <kbd>esc</kbd> закрыть</span>`
    : `<span class="cmdk-scopes">Скоупы: <kbd>к:</kbd> конспекты <kbd>з:</kbd> задания
       <kbd>п:</kbd> пары <kbd>&gt;</kbd> команды</span>`;
}

function select(delta) {
  const list = window.__cmdk || [];
  if (!list.length) return;
  sel = (sel + delta + list.length) % list.length;
  $$(".cmdk-item").forEach((el) => {
    const on = +el.dataset.i === sel;
    el.classList.toggle("sel", on);
    el.setAttribute("aria-selected", String(on));
  });
  $("#cmdk-input").setAttribute("aria-activedescendant", `cmdk-item-${sel}`);
  $(`#cmdk-item-${sel}`)?.scrollIntoView({ block: "nearest" });
}

function activate(i = sel) {
  const list = window.__cmdk || [];
  const it = list[i];
  if (!it) return;
  saveHist($("#cmdk-input").value.trim());
  closeCmdk();
  it.go();
}

/* ---------- инициализация ---------- */
export function initSearch() {
  $("#search-trigger")?.addEventListener("click", () => openCmdk());
  $("#cmdk-overlay")?.addEventListener("click", (e) => {
    if (e.target === $("#cmdk-overlay")) closeCmdk();
  });
  $("#cmdk-input")?.addEventListener("input", debounce((e) => update(e.target.value), 90));
  $("#cmdk-input")?.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") { e.preventDefault(); select(1); }
    else if (e.key === "ArrowUp") { e.preventDefault(); select(-1); }
    else if (e.key === "Enter") { e.preventDefault(); activate(); }
  });
  $("#cmdk-results")?.addEventListener("click", (e) => {
    const el = e.target.closest(".cmdk-item");
    if (el) activate(+el.dataset.i);
  });
  $("#cmdk-results")?.addEventListener("mousemove", (e) => {
    const el = e.target.closest(".cmdk-item");
    if (!el) return;
    sel = +el.dataset.i;
    $$(".cmdk-item").forEach((x) => {
      const on = +x.dataset.i === sel;
      x.classList.toggle("sel", on);
      x.setAttribute("aria-selected", String(on));
    });
  });

  window.addEventListener("tree-mode-change", invalidateIndex);
}