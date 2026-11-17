/* ============================================================
   panes.js — раскладка: четыре колонки, ресайз, сворачивание,
   мобильные оверлеи. Всё состояние ширин живёт в prefs и
   применяется CSS-переменными на #app — один источник правды.
   ============================================================ */
"use strict";

import { $, $$, clamp } from "./util.js";
import { prefs, savePrefs, PANE_LIMITS } from "./store.js";

const app = $("#app");
const backdrop = $("#backdrop");
const mq = window.matchMedia("(max-width: 900px)");

export const isMobile = () => mq.matches;

let lastTreeWidth = prefs.tree >= PANE_LIMITS.tree.min ? prefs.tree : PANE_LIMITS.tree.def;
let lastAiWidth = prefs.ai >= PANE_LIMITS.ai.min ? prefs.ai : PANE_LIMITS.ai.def;

/* ---------- применение ---------- */
function currentWidth(pane) {
  if (pane === "nav") return prefs.railCollapsed ? PANE_LIMITS.nav.rail : prefs.nav;
  if (pane === "tree") return prefs.treeOpen ? prefs.tree : 0;
  return prefs.aiOpen ? prefs.ai : 0;
}

function updateSeparators() {
  $$(".pane-handle").forEach((h) => {
    const pane = h.dataset.pane;
    const lim = PANE_LIMITS[pane];
    h.setAttribute("aria-valuenow", String(Math.round(currentWidth(pane))));
    h.setAttribute("aria-valuemin", String(pane === "nav" ? lim.min : 0));
    h.setAttribute("aria-valuemax", String(lim.max));
    const collapsed = pane === "nav" ? prefs.railCollapsed
      : pane === "tree" ? !prefs.treeOpen : !prefs.aiOpen;
    h.classList.toggle("is-collapsed", collapsed);
  });
}

export function applyLayout() {
  app.style.setProperty("--nav-w",
    (prefs.railCollapsed ? PANE_LIMITS.nav.rail : prefs.nav) + "px");
  app.style.setProperty("--tree-w", (prefs.treeOpen ? prefs.tree : 0) + "px");
  app.style.setProperty("--ai-w", (prefs.aiOpen ? prefs.ai : 0) + "px");
  app.classList.toggle("rail-collapsed", !!prefs.railCollapsed);
  app.classList.toggle("tree-collapsed", !prefs.treeOpen);
  app.classList.toggle("ai-open", !!prefs.aiOpen);

  const tBtn = $("#tree-toggle");
  if (tBtn) tBtn.setAttribute("aria-pressed", String(!!prefs.treeOpen));
  const fab = $("#ai-fab");
  if (fab) fab.setAttribute("aria-expanded", String(!!prefs.aiOpen));
  const pane = $("#ai-pane");
  if (pane) pane.setAttribute("aria-hidden", String(!prefs.aiOpen));

  updateSeparators();
  window.dispatchEvent(new CustomEvent("layout-change"));
}

function setWidth(pane, w) {
  if (pane === "nav") prefs.nav = w;
  else if (pane === "tree") prefs.tree = w;
  else prefs.ai = w;
  applyLayout();
}

/* ---------- открыть/закрыть панели ---------- */
export function toggleRail(force) {
  prefs.railCollapsed = force ?? !prefs.railCollapsed;
  savePrefs();
  applyLayout();
}

export function toggleTree(force) {
  if (isMobile()) { openDrawer("tree"); return; }
  const next = force ?? !prefs.treeOpen;
  if (next && prefs.tree < PANE_LIMITS.tree.min) prefs.tree = lastTreeWidth;
  prefs.treeOpen = next;
  savePrefs();
  applyLayout();
}

export function toggleAi(force, focus = true) {
  if (isMobile()) {
    const open = force ?? !app.classList.contains("ai-sheet-open");
    app.classList.toggle("ai-sheet-open", open);
    backdrop.hidden = !open;
    if (open) $("#ai-sheet-close")?.focus?.();
    window.dispatchEvent(new CustomEvent("layout-change"));
    return open;
  }
  const next = force ?? !prefs.aiOpen;
  if (next && prefs.ai < PANE_LIMITS.ai.min) prefs.ai = lastAiWidth;
  prefs.aiOpen = next;
  savePrefs();
  applyLayout();
  if (next && focus) setTimeout(() => $("#ai-input")?.focus(), 60);
  return next;
}

/* ---------- мобильные оверлеи ---------- */
export function openDrawer(which) {
  app.classList.toggle("rail-open", which === "rail");
  app.classList.toggle("tree-open", which === "tree");
  const anyOpen = app.classList.contains("rail-open") || app.classList.contains("tree-open")
    || app.classList.contains("ai-sheet-open");
  backdrop.hidden = !anyOpen;
  if (which === "tree") setTimeout(() => $("#tree-filter")?.focus(), 120);
}

export function closeDrawers() {
  app.classList.remove("rail-open", "tree-open", "ai-sheet-open");
  backdrop.hidden = true;
}

/* ---------- перетаскивание ---------- */
const COLLAPSE_AT = { nav: 150, tree: 120, ai: 200 };

function wireHandle(el) {
  const pane = el.dataset.pane;
  if (!pane) return;
  const lim = PANE_LIMITS[pane];
  let startX = 0, startW = 0, dragging = false, moved = false;

  const sign = () => (pane === "ai" ? -1 : 1);

  const end = (e) => {
    if (!dragging) return;
    dragging = false;
    document.body.classList.remove("resizing");
    app.classList.remove("no-anim");
    try { el.releasePointerCapture(e.pointerId); } catch { /* noop */ }

    const w = currentWidth(pane);
    if (w > 0 && w < COLLAPSE_AT[pane]) {
      // потянули почти до нуля — сворачиваем панель целиком
      if (pane === "nav") { prefs.railCollapsed = true; }
      else if (pane === "tree") { prefs.treeOpen = false; }
      else { prefs.aiOpen = false; }
    } else if (w > 0) {
      const fixed = clamp(w, lim.min, lim.max);
      if (pane === "nav") { prefs.railCollapsed = false; prefs.nav = fixed; }
      else if (pane === "tree") { prefs.treeOpen = true; prefs.tree = fixed; lastTreeWidth = fixed; }
      else { prefs.aiOpen = true; prefs.ai = fixed; lastAiWidth = fixed; }
    } else {
      if (pane === "tree") prefs.treeOpen = false;
      if (pane === "ai") prefs.aiOpen = false;
      if (pane === "nav") prefs.railCollapsed = true;
    }
    savePrefs();
    applyLayout();
  };

  el.addEventListener("pointerdown", (e) => {
    if (isMobile() || e.button !== 0) return;
    dragging = true;
    moved = false;
    startX = e.clientX;
    startW = currentWidth(pane) || (pane === "nav" ? PANE_LIMITS.nav.rail : lim.min);
    if (startW === 0) { // панель была свёрнута — начинаем с минимума
      dragging = true;
      startW = pane === "nav" ? PANE_LIMITS.nav.rail : lim.min;
      if (pane === "tree") prefs.treeOpen = true;
      if (pane === "ai") prefs.aiOpen = true;
    }
    app.classList.add("no-anim");
    try { el.setPointerCapture(e.pointerId); } catch { /* noop */ }
    e.preventDefault();
  });

  el.addEventListener("pointermove", (e) => {
    if (!dragging) return;
    const dx = (e.clientX - startX) * sign();
    if (Math.abs(dx) > 2) moved = true;
    let w = startW + dx;

    // «магнит» к свёрнутому состоянию рядом с нулём
    if (w < COLLAPSE_AT[pane] * 0.55) w = pane === "nav" ? PANE_LIMITS.nav.rail : 0;
    else w = clamp(w, pane === "nav" ? PANE_LIMITS.nav.rail : lim.min, lim.max);

    if (pane === "nav") prefs.railCollapsed = w <= PANE_LIMITS.nav.rail;
    else if (pane === "tree") prefs.treeOpen = w > 0;
    else prefs.aiOpen = w > 0;

    setWidth(pane, w);
  });

  el.addEventListener("pointerup", end);
  el.addEventListener("pointercancel", end);

  el.addEventListener("dblclick", () => {
    if (isMobile()) return;
    const def = pane === "nav" ? PANE_LIMITS.nav.def : lim.def;
    if (pane === "nav") { prefs.railCollapsed = false; prefs.nav = def; }
    if (pane === "tree") { prefs.treeOpen = true; prefs.tree = def; lastTreeWidth = def; }
    if (pane === "ai") { prefs.aiOpen = true; prefs.ai = def; lastAiWidth = def; }
    savePrefs();
    applyLayout();
  });

  el.addEventListener("keydown", (e) => {
    const step = e.shiftKey ? 64 : 16;
    let delta = 0;
    if (e.key === "ArrowLeft") delta = -step * sign();
    else if (e.key === "ArrowRight") delta = step * sign();
    else if (e.key === "Home") { if (pane !== "nav") { prefs.treeOpen = pane === "tree" ? false : prefs.treeOpen; } e.preventDefault(); }
    else if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      if (pane === "tree") toggleTree();
      else if (pane === "ai") toggleAi();
      else toggleRail();
      return;
    } else return;

    e.preventDefault();
    const w0 = currentWidth(pane) || (pane === "nav" ? PANE_LIMITS.nav.rail : lim.min);
    const w = clamp(w0 + delta, pane === "nav" ? PANE_LIMITS.nav.rail : lim.min, lim.max);
    if (pane === "nav") { prefs.railCollapsed = false; prefs.nav = w; }
    else if (pane === "tree") { prefs.treeOpen = true; prefs.tree = w; lastTreeWidth = w; }
    else { prefs.aiOpen = true; prefs.ai = w; lastAiWidth = w; }
    savePrefs();
    applyLayout();
  });
}

/* ---------- инициализация ---------- */
export function initPanes() {
  $$(".pane-handle").forEach(wireHandle);

  $("#tree-toggle")?.addEventListener("click", () => toggleTree());
  $("#rail-collapse")?.addEventListener("click", () => toggleRail());
  $("#menu-btn")?.addEventListener("click", () => openDrawer("rail"));
  $("#ai-hide")?.addEventListener("click", () => toggleAi(false));
  $("#ai-sheet-close")?.addEventListener("click", () => closeDrawers());
  $("#ai-bar")?.addEventListener("click", () => toggleAi(true));
  backdrop.addEventListener("click", closeDrawers);

  mq.addEventListener("change", () => {
    closeDrawers();
    applyLayout();
  });

  window.addEventListener("beforeunload", savePrefs);
  applyLayout();
}