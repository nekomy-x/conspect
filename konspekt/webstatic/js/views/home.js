/* ============================================================
   views/home.js — дашборд: сегодня, счётчики, дедлайны.
   ============================================================ */
"use strict";

import { $, esc, plural } from "../util.js";
import { api } from "../api.js";
import { state } from "../store.js";

function lessonNow(it) {
  const [h1, m1] = (it.time.split("–")[0] || "").split(":").map(Number);
  const [h2, m2] = (it.time.split("–")[1] || "").split(":").map(Number);
  const now = new Date();
  const cur = now.getHours() * 60 + now.getMinutes();
  return h1 != null && h2 != null && cur >= h1 * 60 + (m1 || 0) && cur <= h2 * 60 + (m2 || 0);
}

function nextLesson(items) {
  const now = new Date();
  const cur = now.getHours() * 60 + now.getMinutes();
  for (const it of items) {
    const [h, m] = (it.time.split("–")[0] || "").split(":").map(Number);
    if (h != null && h * 60 + (m || 0) >= cur) return it;
  }
  return null;
}

export function deadlineRow(a) {
  const dl = a.deadline_ts ? new Date(a.deadline_ts * 1000) : null;
  const now = Date.now() / 1000;
  const badge = {
    done: `<span class="badge done">сдано</span>`,
    overdue: `<span class="badge overdue">просрочено</span>`,
    urgent: `<span class="badge urgent">${a.left_hours < 48 ? a.left_hours + " ч" : a.left_days + " дн"}</span>`,
    ok: `<span class="badge ok">${a.left_days} дн</span>`,
  }[a.status] || "";

  // полоса «сколько срока уже прошло» — видно, что время уходит
  let progress = "";
  if (dl && a.status !== "done") {
    const total = a.deadline_ts - (a.created_ts || a.deadline_ts - 7 * 86400);
    const done = now - (a.created_ts || a.deadline_ts - 7 * 86400);
    const pct = total > 0 ? Math.max(2, Math.min(100, (done / total) * 100)) : 100;
    progress = `<div class="dl-progress ${a.status}"><i style="width:${pct}%"></i></div>`;
  }

  return `
  <a class="deadline-row" href="#/assignment/${a.cmid}">
    <div class="dl-date"><b>${dl ? dl.getDate() : "—"}</b>
      <span>${dl ? dl.toLocaleString("ru", { month: "short" }) : ""}</span></div>
    <div class="dl-info">
      <div class="dl-name">${esc(a.name)}</div>
      <div class="dl-course">${esc(a.course)}${a.deadline ? ` · ${a.deadline}` : ""}</div>
      ${progress}
    </div>
    ${badge}
  </a>`;
}

export function conspectCard(c, n) {
  const chips = [];
  if (c.has_presentation) chips.push(`<span class="chip gold">презентация</span>`);
  if (c.n_slides) chips.push(`<span class="chip teal">${c.n_slides} слайдов</span>`);
  if (c.reading_min) chips.push(`<span class="chip">${c.reading_min} мин</span>`);
  return `
  <a class="card conspect-card" href="#/conspect/${encodeURIComponent(c.id)}">
    ${n != null ? `<div class="conspect-num">${n}</div>` : ""}
    <div class="conspect-body">
      <div class="conspect-title">${esc(c.title)}</div>
      ${c.quote ? `<div class="conspect-quote">«${esc(c.quote)}»</div>` : ""}
      <div class="chips">${chips.join("")}</div>
    </div>
  </a>`;
}

export async function renderHome() {
  const content = $("#content");
  let d;
  try { d = await api("/api/stats"); } catch (e) {
    content.innerHTML = `<div class="page"><div class="empty">
      <i class="ph ph-warning-circle empty-ph"></i>${esc(e.message)}
      <div style="margin-top:14px"><button class="btn" id="retry-home" type="button">Повторить</button></div>
    </div></div>`;
    $("#retry-home")?.addEventListener("click", () => renderHome());
    return;
  }
  state.stats = d;

  const badge = $("#badge-assignments");
  if (badge) {
    badge.hidden = !(d.counts.urgent > 0);
    badge.textContent = d.counts.urgent;
  }

  const nowLesson = d.today.items.find(lessonNow);
  const upcoming = nowLesson ? null : nextLesson(d.today.items);

  const lessons = d.today.items.length
    ? d.today.items.map((it) => {
        const isNow = lessonNow(it);
        const isNext = upcoming && it === upcoming;
        return `
        <div class="lesson ${isNow ? "lesson-now" : ""} ${isNext ? "lesson-next" : ""}">
          <span class="lesson-time">${esc(it.time)}</span>
          <div>
            <div class="lesson-name">${esc(it.title)}</div>
            ${it.place ? `<div class="lesson-place">${esc(it.place)}</div>` : ""}
          </div>
          ${isNow ? `<span class="lesson-flag now">идёт</span>`
            : isNext ? `<span class="lesson-flag next">далее</span>` : ""}
        </div>`;
      }).join("")
    : `<div class="sched-empty" style="padding:18px 0">пар нет — свободный день</div>`;

  const deadlinesBlock = d.deadlines.length
    ? `<div class="section-label">Скоро сдавать</div>
       <div class="grid">${d.deadlines.map(deadlineRow).join("")}</div>`
    : `<div class="section-label">Скоро сдавать</div>
       <p class="all-quiet"><i class="ph ph-check-circle"></i> Горящих дедлайнов нет</p>`;

  const overdueBlock = (d.overdue || []).length
    ? `<div class="section-label">Просроченные</div>
       <div class="grid">${d.overdue.map(deadlineRow).join("")}</div>`
    : "";

  const recents = d.recent_conspects.length
    ? d.recent_conspects.map((c) => conspectCard(c)).join("")
    : `<div class="empty">
        <i class="ph ph-book-open empty-ph"></i>
        <p>Конспектов пока нет.</p>
        <ol class="empty-steps">
          <li>Скинь боту в Telegram ссылку на запись лекции</li>
          <li>Он расшифрует звук и соберёт конспект со слайдами</li>
          <li>Конспект появится здесь — с оглавлением и ИИ-чатом</li>
        </ol>
       </div>`;

  const wdCap = d.today.weekday[0].toUpperCase() + d.today.weekday.slice(1);
  const dayN = new Date(d.today.date + "T00:00:00").getDate();
  const monN = new Date(d.today.date + "T00:00:00").toLocaleString("ru", { month: "long" }).slice(0, 3);

  content.innerHTML = `
  <div class="page wide">
    <div class="hero">
      <div class="today-card">
        <div class="today-head">
          <span class="today-date">${wdCap}, ${dayN} ${monN}</span>
          <span class="today-wd">неделя ${esc(d.today.parity)}</span>
          <span class="parity-chip">сегодня</span>
        </div>
        <div class="lessons">${lessons}</div>
      </div>
      <div class="stat-cards">
        <a class="stat acc" href="#/conspects">
          <b>${d.counts.conspects}</b>
          <span>${plural(d.counts.conspects, "конспект", "конспекта", "конспектов")}</span>
          <i class="ph ph-arrow-right stat-go" aria-hidden="true"></i>
        </a>
        <a class="stat teal" href="#/assignments">
          <b>${d.counts.assignments_open}</b>
          <span>${plural(d.counts.assignments_open, "открытое задание", "открытых задания", "открытых заданий")}</span>
          <i class="ph ph-arrow-right stat-go" aria-hidden="true"></i>
        </a>
        <a class="stat ${d.counts.urgent > 0 ? "rose" : ""}" href="#/assignments">
          <b>${d.counts.urgent}</b>
          <span>срок ≤ 3 дней</span>
          <i class="ph ph-arrow-right stat-go" aria-hidden="true"></i>
        </a>
        <div class="stat">
          <b>${d.counts.slides}</b>
          <span>${plural(d.counts.slides, "слайд в базе", "слайда в базе", "слайдов в базе")}</span>
        </div>
      </div>
    </div>

    ${deadlinesBlock}
    ${overdueBlock}

    <div class="section-label">Последние конспекты</div>
    <div class="grid cols-2">${recents}</div>
  </div>`;
}
