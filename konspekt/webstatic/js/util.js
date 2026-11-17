/* ============================================================
   util.js — мелкие утилиты: DOM, формат, тосты.
   ============================================================ */
"use strict";

export const $ = (s, p = document) => p.querySelector(s);
export const $$ = (s, p = document) => [...p.querySelectorAll(s)];

export const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

export const clamp = (v, min, max) => Math.max(min, Math.min(max, v));

export function debounce(fn, ms) {
  let t = null;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

export function throttleRAF(fn) {
  let queued = false, lastArgs = null;
  return (...a) => {
    lastArgs = a;
    if (queued) return;
    queued = true;
    requestAnimationFrame(() => { queued = false; fn(...lastArgs); });
  };
}

export const prefersReduced = () =>
  window.matchMedia("(prefers-reduced-motion: reduce)").matches;

export function onIdle(fn, timeout = 1200) {
  if ("requestIdleCallback" in window) window.requestIdleCallback(fn, { timeout });
  else setTimeout(fn, 200);
}

/* ---------- тосты (aria-live контейнер уже в разметке) ---------- */
export function toast(text, kind = "") {
  const wrap = $("#toast-wrap");
  if (!wrap) return;
  const t = document.createElement("div");
  t.className = "toast" + (kind ? ` ${kind}` : "");
  t.setAttribute("role", kind === "error" ? "alert" : "status");
  t.textContent = text;
  wrap.appendChild(t);
  setTimeout(() => { t.classList.add("out"); }, 2600);
  setTimeout(() => t.remove(), 3100);
}

/* ---------- русские числительные ---------- */
export function plural(n, one, few, many) {
  const m10 = n % 10, m100 = n % 100;
  if (m10 === 1 && m100 !== 11) return one;
  if (m10 >= 2 && m10 <= 4 && (m100 < 10 || m100 >= 20)) return few;
  return many;
}

/* ---------- даты ---------- */
const MONTHS = ["янв", "фев", "мар", "апр", "мая", "июн",
                "июл", "авг", "сен", "окт", "ноя", "дек"];

export const monthsShort = () => MONTHS;

export function fmtDeadline(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  return `${d.getDate()}.${String(d.getMonth() + 1).padStart(2, "0")} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

/** «осталось 3 дн», «через 5 ч», «просрочено на 2 дн» */
export function fmtLeft(ts, now = Date.now() / 1000) {
  if (!ts) return "";
  const left = ts - now;
  const abs = Math.abs(left);
  const days = Math.floor(abs / 86400);
  const hours = Math.floor(abs / 3600);
  const mins = Math.floor(abs / 60);
  let core = days > 0 ? `${days} дн` : hours > 0 ? `${hours} ч` : `${mins} мин`;
  return left < 0 ? `просрочено на ${core}` : core;
}

export function timeAgo(ts) {
  if (!ts) return "";
  const sec = Math.max(1, Date.now() / 1000 - ts);
  if (sec < 3600) return `${Math.floor(sec / 60)} мин назад`;
  if (sec < 86400) return `${Math.floor(sec / 3600)} ч назад`;
  const d = Math.floor(sec / 86400);
  if (d === 1) return "вчера";
  if (d < 30) return `${d} дн назад`;
  const dd = new Date(ts * 1000);
  return `${dd.getDate()} ${MONTHS[dd.getMonth()]}`;
}

/** Копирование с фолбэком для не-HTTPS контекстов. */
export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast("Скопировано");
    return true;
  } catch {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch { ok = false; }
    ta.remove();
    toast(ok ? "Скопировано" : "Не удалось скопировать", ok ? "" : "error");
    return ok;
  }
}

/* ---------- фокус ---------- */
const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select, textarea, [tabindex]:not([tabindex="-1"])';

/** Ловушка фокуса: возвращает функцию снятия. */
export function trapFocus(container, onEscape) {
  const prev = document.activeElement;
  const onKey = (e) => {
    if (e.key === "Escape" && onEscape) { e.stopPropagation(); onEscape(); return; }
    if (e.key !== "Tab") return;
    const items = $$(FOCUSABLE, container).filter((el) => el.offsetParent !== null);
    if (!items.length) return;
    const first = items[0], last = items[items.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  };
  container.addEventListener("keydown", onKey);
  const firstInput = $(FOCUSABLE, container);
  if (firstInput) setTimeout(() => firstInput.focus(), 30);
  return () => {
    container.removeEventListener("keydown", onKey);
    if (prev && prev.focus) try { prev.focus(); } catch { /* noop */ }
  };
}