/* ============================================================
   router.js — хэш-роутинг, хлебные крошки, память скролла.
   Перед каждым рендером: снимаем обработчики прошлого экрана
   (runCleanup) и обрываем его сетевые запросы (AbortController).
   ============================================================ */
"use strict";

import { $, $$, esc } from "./util.js";
import { state } from "./store.js";
import { runCleanup } from "./lifecycle.js";
import { setFetchSignal } from "./api.js";
import { renderHome } from "./views/home.js";
import { renderConspects } from "./views/conspects.js";
import { renderAssignments, renderAssignment } from "./views/assignments.js";
import { renderSchedule } from "./views/schedule.js";
import { renderSlides } from "./views/slides.js";
import { renderConspect, neighbours } from "./reader.js";
import { setActive, renderTree } from "./tree.js";
import { closeDrawers } from "./panes.js";

const routes = [
  { re: /^#\/$/, name: "home" },
  { re: /^#\/conspects$/, name: "conspects" },
  { re: /^#\/conspect\/([^/]+)(?:\/(\d+))?$/, name: "conspect" },
  { re: /^#\/slides\/([^/]+)$/, name: "slides" },
  { re: /^#\/assignments$/, name: "assignments" },
  { re: /^#\/assignment\/(\d+)$/, name: "assignment" },
  { re: /^#\/schedule$/, name: "schedule" },
];

const SCROLL_MEMO_ROUTES = new Set(["home", "conspects", "assignments", "schedule"]);

let controller = new AbortController();
let booted = false;

function routeKey(route) {
  if (!route) return null;
  return route.name + (route.args?.length ? ":" + route.args.join(":") : "");
}

/* ---------- крошки ---------- */
function crumbsFor(route) {
  const { name, args } = route;
  switch (name) {
    case "home": return [{ label: "Главная" }];
    case "conspects": return [{ label: "Библиотека", href: "#/conspects" }];
    case "conspect": {
      const c = (state.conspects || []).find((x) => x.id === args[0]);
      return [
        { label: "Темы", href: "#/conspects" },
        ...(c?.course ? [{ label: c.course, href: "#/conspects" }] : []),
        { label: c?.title || "Конспект" },
      ];
    }
    case "slides": {
      const c = (state.conspects || []).find((x) => x.id === args[0]);
      return [{ label: "Темы", href: "#/conspects" },
              { label: c?.title || "Конспект", href: `#/conspect/${args[0]}` },
              { label: "Слайды" }];
    }
    case "assignments": return [{ label: "Задания", href: "#/assignments" }];
    case "assignment": return [{ label: "Задания", href: "#/assignments" }, { label: "Задание" }];
    case "schedule": return [{ label: "Расписание" }];
    default: return [];
  }
}

function paintCrumbs(route) {
  const c = $("#crumbs");
  if (!c) return;
  const parts = crumbsFor(route);
  c.innerHTML = parts.map((p, i) => {
    const last = i === parts.length - 1;
    const text = `<span class="crumb-text">${esc(p.label)}</span>`;
    return last || !p.href
      ? `<b class="crumb${last ? " is-last" : ""}">${text}</b>`
      : `<a class="crumb" href="${p.href}">${text}</a>`;
  }).join('<span class="crumb-sep" aria-hidden="true">/</span>');
  // сжатая мобильная форма: «‹ Назад»
  const prev = parts.length > 1 ? parts[parts.length - 2] : null;
  const back = $("#crumb-back");
  if (back) back.hidden = !prev;
}

function setActiveNav(name) {
  const target = name === "conspect" || name === "slides" ? "conspects" : name;
  $$(".rail-nav a, .tabbar a").forEach((a) =>
    a.classList.toggle("active", a.dataset.nav === target));
  $$(".rail-nav a, .tabbar a").forEach((a) => {
    if (a.dataset.nav === target) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
}

/* ---------- рендер ---------- */
function render() {
  const route = state.route;
  const content = $("#content");
  if (!content) return;

  runCleanup();
  controller.abort();
  controller = new AbortController();
  setFetchSignal(controller.signal);

  setActiveNav(route.name);
  paintCrumbs(route);
  closeDrawers();

  $(".read-bar")?.remove();
  $("#ai-popover") && ($("#ai-popover").hidden = true);
  content.scrollTop = 0;
  content.className = "content";

  const pages = {
    home: renderHome,
    conspects: renderConspects,
    conspect: () => renderConspect(route.args[0], route.args[1]),
    slides: () => renderSlides(route.args[0]),
    assignments: renderAssignments,
    assignment: () => renderAssignment(route.args[0]),
    schedule: renderSchedule,
  };

  if (route.name === "conspect") {
    setActive(route.args[0], route.args[1] != null ? +route.args[1] : null);
  } else {
    clearTreeActive();
  }

  const result = pages[route.name]?.();
  // восстановление позиции для списочных экранов
  if (SCROLL_MEMO_ROUTES.has(route.name)) {
    const key = routeKey(route);
    Promise.resolve(result).finally(() => {
      const top = state.scrollMemo.get(key) || 0;
      if (top) requestAnimationFrame(() => { content.scrollTop = top; });
    });
  }
  document.title = titleFor(route);
}

function titleFor(route) {
  const base = "Конспект";
  if (route.name === "conspect") {
    const c = (state.conspects || []).find((x) => x.id === route.args[0]);
    return c ? `${c.title} — ${base}` : base;
  }
  const names = { home: "Главная", conspects: "Библиотека", assignments: "Задания",
                  assignment: "Задание", schedule: "Расписание", slides: "Слайды" };
  return `${names[route.name] || ""} — ${base}`;
}

function clearTreeActive() {
  state.activeConspect = null;
  state.activeSec = null;
  $$(".tree-node-lect.is-active, .tree-row-sec.is-active").forEach((el) =>
    el.classList.remove("is-active"));
}

export function navigate() {
  const hash = location.hash || "#/";
  let m = null, name = null;
  for (const r of routes) {
    m = hash.match(r.re);
    if (m) { name = r.name; break; }
  }
  if (!name) { location.hash = "#/"; return; }

  const content = $("#content");
  const prevKey = routeKey(state.route);
  if (booted && prevKey && content) state.scrollMemo.set(prevKey, content.scrollTop);

  state.route = { name, args: m ? m.slice(1).filter((x) => x != null) : [] };
  render();
  booted = true;

  // «назад/вперёд» в браузере тоже возвращает на место
  window.dispatchEvent(new CustomEvent("route-change", { detail: state.route }));
}

export function initRouter() {
  window.addEventListener("hashchange", navigate);
}

export function prevNext() {
  if (state.route.name !== "conspect") return { prev: null, next: null };
  return neighbours(state.route.args[0]);
}

export function refreshTree() { renderTree(); }