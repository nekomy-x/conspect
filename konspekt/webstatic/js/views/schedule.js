/* ============================================================
   views/schedule.js — расписание на 4 недели + связь с курсами.
   ============================================================ */
"use strict";

import { $, esc } from "../util.js";
import { api } from "../api.js";
import { state } from "../store.js";
import { listen } from "../lifecycle.js";
import { openModal } from "../tools.js";

function sameCourse(a, b) {
  const norm = (s) => String(s || "").toLowerCase().replace(/[^а-яa-z0-9 ]/g, "").trim();
  const x = norm(a), y = norm(b);
  if (!x || !y) return false;
  return x === y || x.startsWith(y) || y.startsWith(x)
    || x.split(" ")[0] === y.split(" ")[0];
}

function openLesson(title, day) {
  const conspects = (state.conspects || []).filter((c) => sameCourse(c.course, title));
  const assignments = (state.assignments || []).filter((a) => sameCourse(a.course, title));
  openModal(`
    <div class="section-label" style="margin-top:0">Пара</div>
    <div class="doc-title" style="font-size:22px;margin-bottom:6px">${esc(title)}</div>
    <div class="meta">${esc(day.weekday)}, ${esc(day.date)}</div>
    <div class="section-label">Конспекты по предмету</div>
    ${conspects.length
      ? `<div class="grid">${conspects.map((c) => `
          <a class="card conspect-card" href="#/conspect/${encodeURIComponent(c.id)}">
            <div class="conspect-body">
              <div class="conspect-title">${esc(c.title)}</div>
              <div class="chips"><span class="chip">${c.n_slides || 0} слайдов</span>
                <span class="chip">${c.reading_min || 1} мин</span></div>
            </div>
          </a>`).join("")}</div>`
      : `<p class="all-quiet"><i class="ph ph-info"></i> Конспектов по этому предмету пока нет</p>`}
    <div class="section-label">Задания</div>
    ${assignments.length
      ? `<div class="grid">${assignments.map((a) => `
          <a class="deadline-row" href="#/assignment/${a.cmid}">
            <div class="dl-info"><div class="dl-name">${esc(a.name)}</div>
              <div class="dl-course">${esc(a.deadline || "без срока")}</div></div>
          </a>`).join("")}</div>`
      : `<p class="all-quiet"><i class="ph ph-info"></i> Заданий не найдено</p>`}
  `);
}

export async function renderSchedule() {
  const content = $("#content");
  let d = state.schedule;
  if (!d) {
    try {
      const r = await api("/api/schedule");
      d = state.schedule = r.days || [];
    } catch (e) {
      content.innerHTML = `<div class="page"><div class="empty">
        <i class="ph ph-warning-circle empty-ph"></i>${esc(e.message)}</div></div>`;
      return;
    }
  }

  const weeks = [];
  for (let i = 0; i < d.length; i += 7) weeks.push(d.slice(i, i + 7));

  content.innerHTML = `<div class="page wide">
    <div class="page-sub">ближайшие ${weeks.length} недели · чётность недель учитывается</div>
    ${weeks.map((days, wi) => `
      <div class="section-label">${wi === 0 ? "Эта неделя" : `Через ${wi} нед.`}</div>
      <div class="sched-grid">
        ${days.map((day) => `
          <div class="sched-day ${day.is_today ? "today" : ""} ${day.is_past ? "past" : ""}">
            <div class="sched-day-head">
              <span class="sched-wd">${esc(day.weekday.slice(0, 3))}</span>
              <span class="sched-num">${+day.date.slice(8)}</span>
            </div>
            ${day.items.length
              ? day.items.map((it) => `
                <button type="button" class="sched-item ${it.kind === "online" ? "online" : ""}"
                        data-title="${esc(it.title)}" data-day="${esc(day.date)}">
                  <span class="t">${esc(it.time)}</span>${esc(it.title)}
                  ${it.place ? `<span class="sched-place">${esc(it.place)}</span>` : ""}
                </button>`).join("")
              : `<div class="sched-empty">—</div>`}
          </div>`).join("")}
      </div>`).join("")}
  </div>`;

  listen(content, "click", (e) => {
    const b = e.target.closest(".sched-item");
    if (!b) return;
    const day = d.find((x) => x.date === b.dataset.day);
    openLesson(b.dataset.title, day || { weekday: "", date: b.dataset.day });
  });
}