/* ============================================================
   views/conspects.js — библиотека: все лекции сеткой по курсам.
   Дополняет дерево (быстрый обзор), а не заменяет его.
   ============================================================ */
"use strict";

import { $, esc, plural, debounce } from "../util.js";
import { api } from "../api.js";
import { state, read, pins } from "../store.js";
import { conspectCard } from "./home.js";

let sortMode = "course"; // course | recent | progress
let query = "";

export async function renderConspects() {
  const content = $("#content");
  if (!state.conspects) {
    try {
      const d = await api("/api/conspects");
      state.conspects = d.items || [];
    } catch (e) {
      content.innerHTML = `<div class="page"><div class="empty">
        <i class="ph ph-warning-circle empty-ph"></i>${esc(e.message)}</div></div>`;
      return;
    }
  }
  paint();
}

function paint() {
  const content = $("#content");
  const all = state.conspects || [];
  const q = query.toLowerCase();
  const items = q
    ? all.filter((c) => c.title.toLowerCase().includes(q)
        || (c.course || "").toLowerCase().includes(q)
        || (c.sections || []).some((s) => s.title.toLowerCase().includes(q)))
    : all;

  const totalSlides = all.reduce((s, c) => s + (c.n_slides || 0), 0);
  const totalMin = all.reduce((s, c) => s + (c.reading_min || 0), 0);

  let blocks = "";
  if (!all.length) {
    blocks = `<div class="empty"><i class="ph ph-book-open empty-ph"></i>Конспектов пока нет.</div>`;
  } else if (!items.length) {
    blocks = `<div class="empty"><i class="ph ph-magnifying-glass empty-ph"></i>
      Ничего не нашлось по «${esc(query)}»</div>`;
  } else if (sortMode === "recent") {
    blocks = `<div class="grid cols-2">${items.map((c) => conspectCard(c)).join("")}</div>`;
  } else if (sortMode === "progress") {
    const done = items.filter((c) => read.state(c.id) === "done");
    const prog = items.filter((c) => read.state(c.id) === "progress");
    const fresh = items.filter((c) => read.state(c.id) === "new");
    const block = (label, list) => list.length
      ? `<div class="section-label">${label} · ${list.length}</div>
         <div class="grid cols-2">${list.map((c) => conspectCard(c)).join("")}</div>` : "";
    blocks = block("Прочитано", done) + block("В процессе", prog) + block("Не открывал", fresh);
  } else {
    const byCourse = new Map();
    for (const c of items) {
      const k = c.course || "Без курса";
      if (!byCourse.has(k)) byCourse.set(k, []);
      byCourse.get(k).push(c);
    }
    blocks = [...byCourse.entries()]
      .sort(([a], [b]) => a.localeCompare(b, "ru"))
      .map(([course, list]) => {
        const pinned = list.filter((c) => pins.has(c.id));
        const rest = list.filter((c) => !pins.has(c.id));
        return `
        <section class="course-block">
          <div class="course-head">
            <span class="course-name">${esc(course)}</span>
            <span class="course-count">${list.length} ${plural(list.length, "лекция", "лекции", "лекций")}</span>
          </div>
          ${pinned.length ? `<div class="grid cols-2">${pinned.map((c) => conspectCard(c)).join("")}</div>` : ""}
          <div class="grid cols-2">${rest.map((c) => conspectCard(c)).join("")}</div>
        </section>`;
      }).join("");
  }

  content.innerHTML = `
  <div class="page wide">
    <div class="lib-bar">
      <div class="lib-search">
        <i class="ph ph-magnifying-glass" aria-hidden="true"></i>
        <input id="lib-q" type="search" placeholder="Фильтр по названию, курсу, разделу"
               value="${esc(query)}" aria-label="Фильтр библиотеки">
      </div>
      <div class="segmented" role="tablist" aria-label="Сортировка">
        ${[["course", "По курсам"], ["recent", "Свежие"], ["progress", "По чтению"]]
          .map(([k, label]) => `<button type="button" role="tab" data-sort="${k}"
            aria-selected="${sortMode === k}" class="${sortMode === k ? "on" : ""}">${label}</button>`).join("")}
      </div>
    </div>
    <div class="page-sub">${all.length} ${plural(all.length, "лекция", "лекции", "лекций")}
      · ${totalSlides} слайдов · ${Math.round(totalMin / 60 * 10) / 10} ч чтения</div>
    ${blocks}
  </div>`;

  const input = $("#lib-q");
  input?.addEventListener("input", debounce((e) => {
    query = e.target.value;
    const pos = e.target.selectionStart;
    paint();
    const next = $("#lib-q");
    if (next) { next.focus(); next.setSelectionRange(pos, pos); }
  }, 160));

  document.querySelectorAll("[data-sort]").forEach((b) => {
    b.addEventListener("click", () => { sortMode = b.dataset.sort; paint(); });
  });
}
