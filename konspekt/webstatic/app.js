/* ============================================================
   app.js — точка входа: тема, глобальные клавиши, справка,
   загрузка данных, живое обновление, старт роутера.

   Вся логика разложена по модулям в /static/js/.
   ============================================================ */
"use strict";

import { $, esc, onIdle, toast } from "./js/util.js";
import { state, prefs } from "./js/store.js";
import { api } from "./js/api.js";
import { initPanes, toggleTree, toggleAi, isMobile, openDrawer, closeDrawers } from "./js/panes.js";
import { initTree, renderTree } from "./js/tree.js";
import { initAi } from "./js/ai.js";
import { initAgent } from "./js/agent.js";
import { initSearch, openCmdk, closeCmdk, isCmdkOpen, invalidateIndex } from "./js/search.js";
import { initRouter, navigate, prevNext } from "./js/router.js";
import { closeModal } from "./js/tools.js";

/* ---------- тема ---------- */
const THEME_KEY = "konspect-theme";

function applyTheme(t) {
  document.documentElement.dataset.theme = t;
  const label = $("#theme-label");
  if (label) label.textContent = t === "light" ? "Светлая" : "Тёмная";
  const icon = $("#theme-icon");
  if (icon) icon.className = t === "light" ? "ph ph-sun" : "ph ph-moon-stars";
  try { localStorage.setItem(THEME_KEY, t); } catch { /* noop */ }
}

function initTheme() {
  let saved = "dark";
  try { saved = localStorage.getItem(THEME_KEY) || "dark"; } catch { /* noop */ }
  applyTheme(saved);
  $("#theme-toggle")?.addEventListener("click", () => {
    applyTheme(document.documentElement.dataset.theme === "light" ? "dark" : "light");
  });
}

/* ---------- справка по клавишам ---------- */
const SHORTCUTS = [
  ["⌘K / Ctrl+K", "Поиск и команды"],
  ["/", "Фильтр в дереве тем"],
  ["g затем h / k / a / s", "Главная · Темы · Задания · Пары"],
  ["⌘B", "Показать или скрыть дерево"],
  ["⌘I", "Показать или скрыть ИИ-панель"],
  ["⇧F", "Режим «только текст»"],
  ["j / k", "Вниз / вверх по дереву"],
  ["← / →", "Схлопнуть / раскрыть раздел"],
  ["[ / ]", "Предыдущая / следующая лекция"],
  ["m", "Закрепить лекцию в дереве"],
  ["Alt+← / →", "Ширина панели (на разделителе)"],
  ["Esc", "Закрыть верхнее окно"],
  ["?", "Эта справка"],
];

function initHelp() {
  const grid = $("#help-grid");
  if (grid) {
    grid.innerHTML = SHORTCUTS.map(([k, v]) =>
      `<div class="help-row"><kbd>${esc(k)}</kbd><span>${esc(v)}</span></div>`).join("");
  }
  const open = () => { $("#help-overlay").hidden = false; };
  const close = () => { $("#help-overlay").hidden = true; };
  $("#help-btn")?.addEventListener("click", open);
  $("#help-close")?.addEventListener("click", close);
  $("#help-overlay")?.addEventListener("click", (e) => {
    if (e.target === $("#help-overlay")) close();
  });
  window.__closeHelp = close;
}

/* ---------- режим «только текст» ---------- */
function toggleFocus(force) {
  const app = $("#app");
  const on = force ?? !app.classList.contains("focus-mode");
  app.classList.toggle("focus-mode", on);
  if (on) toast("Режим «только текст» — Esc чтобы выйти");
}

/* ---------- глобальные клавиши ---------- */
function initKeys() {
  let gPending = false;
  let gTimer = null;

  document.addEventListener("keydown", (e) => {
    const inField = e.target.matches("input, textarea, select, [contenteditable]");

    // ⌘K — палитра
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
      e.preventDefault();
      isCmdkOpen() ? closeCmdk() : openCmdk();
      return;
    }
    // ⌘B — дерево, ⌘I — ИИ
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "b") {
      e.preventDefault();
      toggleTree();
      return;
    }
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "i") {
      e.preventDefault();
      toggleAi();
      return;
    }

    if (e.key === "Escape") {
      if (isCmdkOpen()) { closeCmdk(); return; }
      if (!$("#help-overlay").hidden) { window.__closeHelp?.(); return; }
      if (!$("#modal-overlay").hidden) { closeModal(); return; }
      if ($("#app").classList.contains("focus-mode")) { toggleFocus(false); return; }
      if ($("#app").classList.contains("ai-sheet-open")
          || $("#app").classList.contains("tree-open")
          || $("#app").classList.contains("rail-open")) { closeDrawers(); return; }
      return;
    }

    if (inField) return;

    // g-навигация
    if (gPending) {
      const map = { h: "#/", k: "#/conspects", a: "#/assignments", s: "#/schedule" };
      const target = map[e.key.toLowerCase()];
      gPending = false;
      clearTimeout(gTimer);
      if (target) { e.preventDefault(); location.hash = target; }
      return;
    }
    if (e.key === "g") {
      gPending = true;
      gTimer = setTimeout(() => { gPending = false; }, 1200);
      return;
    }

    if (e.key === "?") { e.preventDefault(); $("#help-btn")?.click(); return; }
    if (e.key === "/") {
      e.preventDefault();
      if (isMobile() || !prefs.treeOpen) toggleTree();
      if (isMobile()) openDrawer("tree");
      setTimeout(() => $("#tree-filter")?.focus(), 120);
      return;
    }
    if (e.key.toLowerCase() === "f" && e.shiftKey) { e.preventDefault(); toggleFocus(); return; }
    if (e.key === "[") { const { prev } = prevNext(); if (prev) location.hash = `#/conspect/${prev.id}`; return; }
    if (e.key === "]") { const { next } = prevNext(); if (next) location.hash = `#/conspect/${next.id}`; return; }
    if (e.key.toLowerCase() === "t" && e.shiftKey) { e.preventDefault(); $("#theme-toggle")?.click(); }
  });
}

/* ---------- данные ---------- */
async function loadConspects({ silent = false } = {}) {
  try {
    const d = await api("/api/conspects");
    state.conspects = d.items || [];
    renderTree();
    invalidateIndex();
    if (!silent) {
      // библиотека и дерево готовы — подтянем остальное для поиска
      onIdle(() => {
        api("/api/assignments").then((r) => {
          state.assignments = r.items || [];
          invalidateIndex();
        }).catch(() => {});
        api("/api/schedule").then((r) => {
          state.schedule = r.days || [];
          invalidateIndex();
        }).catch(() => {});
      });
    }
  } catch {
    state.conspects = [];
    renderTree();
  }
}

/* ---------- живое обновление ---------- */
function initPolling() {
  const tick = async () => {
    if (document.visibilityState !== "visible") return;
    try {
      const d = await api("/api/stats");
      state.stats = d;
      const badge = $("#badge-assignments");
      if (badge) {
        badge.hidden = !(d.counts.urgent > 0);
        badge.textContent = d.counts.urgent;
      }
      const known = (state.conspects || []).length;
      if (d.counts.conspects !== known) await loadConspects({ silent: true });
    } catch { /* тихо: сеть может мигнуть */ }
  };
  setInterval(tick, 60000);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") tick();
  });
  onIdle(tick, 4000);
}

/* ---------- старт ---------- */
function boot() {
  initTheme();
  initPanes();
  initAi();
  initAgent();
  initSearch();
  initTree();
  initHelp();
  initKeys();
  initRouter();
  initPolling();

  $("#crumb-back")?.addEventListener("click", () => history.back());

  // закрепление/открепление из читалки обновляет дерево
  window.addEventListener("tree-refresh", () => renderTree());

  navigate();
  loadConspects();
}

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
else boot();

// на всякий случай: не даём «мертвым» кнопкам без обработчика уводить со страницы
window.addEventListener("error", (e) => {
  if (e.message?.includes("ResizeObserver")) return;
  console.warn("[konspect]", e.message);
});