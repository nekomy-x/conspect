/* ============================================================
   views/assignments.js — задания: по срокам или по предметам,
   с чек-листом «что сдать» (галочки живут локально).
   ============================================================ */
"use strict";

import { $, $$, esc, plural, toast } from "../util.js";
import { api } from "../api.js";
import { state } from "../store.js";
import { setAiContext, openAiFor } from "../ai.js";
import { deadlineRow } from "./home.js";

const CHECK_KEY = "konspect.checks.v1";

const checks = (() => {
  try { return JSON.parse(localStorage.getItem(CHECK_KEY) || "{}"); } catch { return {}; }
})();

function saveChecks() {
  try { localStorage.setItem(CHECK_KEY, JSON.stringify(checks)); } catch { /* noop */ }
}

let mode = "time"; // time | course

export async function renderAssignments() {
  const content = $("#content");
  let items = state.assignments;
  if (!items) {
    try {
      const d = await api("/api/assignments");
      items = state.assignments = d.items || [];
    } catch (e) {
      content.innerHTML = `<div class="page"><div class="empty">
        <i class="ph ph-warning-circle empty-ph"></i>${esc(e.message)}</div></div>`;
      return;
    }
  }
  paint(items);
}

function paint(items) {
  const content = $("#content");
  const bar = `
    <div class="lib-bar">
      <div class="page-sub" style="margin:0">${items.length}
        ${plural(items.length, "активное задание", "активных задания", "активных заданий")} · из Moodle</div>
      <div class="segmented" role="tablist" aria-label="Группировка">
        ${[["time", "По срокам"], ["course", "По предметам"]]
          .map(([k, label]) => `<button type="button" role="tab" data-mode="${k}"
            aria-selected="${mode === k}" class="${mode === k ? "on" : ""}">${label}</button>`).join("")}
      </div>
    </div>`;

  let blocks = "";
  if (!items.length) {
    blocks = `<div class="empty"><i class="ph ph-check-circle empty-ph"></i>
      Активных заданий нет — красота</div>`;
  } else if (mode === "course") {
    const byCourse = new Map();
    for (const a of items) {
      const k = a.course || "Без курса";
      if (!byCourse.has(k)) byCourse.set(k, []);
      byCourse.get(k).push(a);
    }
    blocks = [...byCourse.entries()].sort(([a], [b]) => a.localeCompare(b, "ru")).map(([course, list]) => `
      <section class="course-block">
        <div class="course-head">
          <span class="course-name">${esc(course)}</span>
          <span class="course-count">${list.length}</span>
        </div>
        <div class="grid">${list.map(deadlineRow).join("")}</div>
      </section>`).join("");
  } else {
    const groups = { urgent: [], ok: [], overdue: [], done: [] };
    for (const a of items) (groups[a.status] || groups.ok).push(a);
    blocks = [
      ["Горит", groups.urgent],
      ["Предстоящие", groups.ok],
      ["Просроченные", groups.overdue],
      ["Сдано / готово", groups.done],
    ].filter(([, list]) => list.length).map(([label, list]) => `
      <div class="section-label">${label} · ${list.length}</div>
      <div class="grid">${list.map(deadlineRow).join("")}</div>`).join("");
  }

  content.innerHTML = `<div class="page">${bar}${blocks}</div>`;

  $$("[data-mode]").forEach((b) => b.addEventListener("click", () => {
    mode = b.dataset.mode;
    paint(items);
  }));
}

/* ---------- чек-лист «что сдать» (используется на карточке задания) ---------- */
export function checkHTML(cmid, list) {
  return `<ul class="checklist">${list.map((w, i) => {
    const key = `${cmid}:${i}`;
    const on = !!checks[key];
    return `<li class="${on ? "on" : ""}">
      <button type="button" class="check-box" data-key="${esc(key)}"
              aria-pressed="${on}" aria-label="Отметить пункт">${on ? "✓" : ""}</button>
      <span>${esc(w)}</span></li>`;
  }).join("")}</ul>`;
}

export function bindChecklist(root) {
  $$(".check-box", root).forEach((b) => b.addEventListener("click", () => {
    const key = b.dataset.key;
    checks[key] = !checks[key];
    saveChecks();
    b.setAttribute("aria-pressed", String(!!checks[key]));
    b.textContent = checks[key] ? "✓" : "";
    b.closest("li")?.classList.toggle("on", !!checks[key]);
  }));
}

/* ---------- карточка одного задания ---------- */
export async function renderAssignment(cmid) {
  const content = $("#content");
  content.innerHTML = `<div class="page">
    <div class="skeleton sk-title" style="width:55%"></div>
    ${'<div class="skeleton sk-line" style="width:92%"></div>'.repeat(5)}</div>`;

  let a;
  try {
    a = await api(`/api/assignment/${cmid}`);
  } catch (e) {
    content.innerHTML = `<div class="page"><div class="empty">
      <i class="ph ph-file-x empty-ph"></i>${esc(e.message)}
      <div style="margin-top:14px"><a class="btn" href="#/assignments">Ко всем заданиям</a></div>
    </div></div>`;
    return;
  }

  setAiContext({ kind: "assignment", cmid: a.cmid, title: a.name });

  const statusBadge = {
    done: `<span class="badge done">сдано</span>`,
    overdue: `<span class="badge overdue">просрочено</span>`,
    urgent: `<span class="badge urgent">горит</span>`,
    ok: `<span class="badge ok">в работе</span>`,
  }[a.status] || "";

  content.innerHTML = `<div class="page">
    <div class="doc-head">
      <div class="doc-course">${esc(a.course || "Задание")}</div>
      <div class="doc-title" style="font-size:26px">${esc(a.name)}</div>
      <div class="doc-meta">
        ${a.deadline ? `<span class="badge mut">${esc(a.deadline)}</span>` : ""}
        ${statusBadge}
      </div>
      <div class="doc-actions">
        <button class="btn primary" id="as-ai" type="button">
          <i class="ph ph-sparkle"></i> Спросить ИИ</button>
        <a class="btn" href="${esc(a.url)}" target="_blank" rel="noopener">
          <i class="ph ph-arrow-square-out"></i> Открыть в Moodle</a>
        <button class="btn" id="as-plan" type="button">
          <i class="ph ph-list-checks"></i> Что сдать?</button>
      </div>
    </div>
    ${a.intro_html ? `<div class="section-label">Условие</div>
      <div class="card doc-body intro-block">${a.intro_html}</div>` : ""}
    ${a.what_html ? `<div class="section-label">Что сдать</div>
      <div class="card doc-body doc-pad" id="as-what">${a.what_html}</div>` : ""}
    <div class="section-label">Разбор / Ответ</div>
    ${a.answer_html
      ? `<div class="card doc-body doc-pad">${a.answer_html}</div>`
      : `<div class="empty"><i class="ph ph-pencil-line empty-ph"></i>
         Ответа пока нет.<br>Попроси бота в Telegram:
         <code>напиши ответ на задание ${a.cmid}</code></div>`}
  </div>`;

  // чек-лист по пунктам «что сдать»
  const whatHost = $("#as-what");
  if (whatHost) {
    const li = $$("li", whatHost);
    if (li.length) {
      const list = li.map((x) => x.textContent.trim());
      whatHost.innerHTML = checkHTML(a.cmid, list);
      bindChecklist(whatHost);
    }
  }

  $("#as-ai")?.addEventListener("click", () => openAiFor({
    kind: "assignment", cmid: a.cmid, title: a.name,
  }));
  $("#as-plan")?.addEventListener("click", () => {
    openAiFor({ kind: "assignment", cmid: a.cmid, title: a.name });
    setTimeout(() => {
      const form = $("#ai-form");
      const inp = $("#ai-input");
      if (inp) { inp.value = "Разложи по шагам, что именно нужно сдать и в каком порядке."; }
      form?.requestSubmit();
    }, 150);
    toast("ИИ разбирает задание");
  });
}