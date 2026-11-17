/* ============================================================
   views/slides.js — просмотр слайдов лекции.
   Клавиши ←/→ работают, но слушатель снимается при уходе с экрана.
   ============================================================ */
"use strict";

import { $, $$, esc } from "../util.js";
import { api } from "../api.js";
import { listen } from "../lifecycle.js";

export async function renderSlides(cid) {
  const content = $("#content");
  content.innerHTML = `<div class="page"><div class="skeleton sk-title" style="width:40%"></div>
    <div class="skeleton" style="height:340px;border-radius:10px"></div></div>`;

  let items = [];
  try {
    const d = await api(`/api/slides/${encodeURIComponent(cid)}`);
    items = d.items || [];
  } catch { items = []; }

  if (!items.length) {
    content.innerHTML = `<div class="page"><div class="empty">
      <i class="ph ph-image empty-ph"></i>Слайдов нет
      <div style="margin-top:14px"><a class="btn" href="#/conspect/${esc(cid)}">К конспекту</a></div>
    </div></div>`;
    return;
  }

  let cur = 0;
  content.innerHTML = `
  <div class="page">
    <div class="slides-viewer">
      <div class="slides-bar">
        <span class="page-sub" style="margin:0">${items.length} слайдов · ←→ листать</span>
        <a class="btn sm" href="#/conspect/${esc(cid)}"><i class="ph ph-arrow-left"></i> К конспекту</a>
      </div>
      <div class="slide-main"><img id="slide-img" src="${items[0].url}" alt="Слайд 1"></div>
      <div class="slide-nav">
        <button class="btn" id="slide-prev" type="button" aria-label="Предыдущий слайд">
          <i class="ph ph-caret-left"></i></button>
        <span class="slide-count" id="slide-count" aria-live="polite">1 / ${items.length}</span>
        <button class="btn" id="slide-next" type="button" aria-label="Следующий слайд">
          <i class="ph ph-caret-right"></i></button>
        <a class="btn" id="slide-open" href="${items[0].url}" target="_blank" rel="noopener">
          <i class="ph ph-arrows-out"></i> Во весь экран</a>
      </div>
      <div class="slide-thumb-strip" id="slide-strip">
        ${items.map((s, i) => `<button class="slide-thumb ${i === 0 ? "active" : ""}" type="button"
          data-i="${i}" aria-label="Слайд ${i + 1}"><img src="${s.url}" loading="lazy" alt=""></button>`).join("")}
      </div>
    </div>
  </div>`;

  const go = (i) => {
    cur = (i + items.length) % items.length;
    $("#slide-img").src = items[cur].url;
    $("#slide-img").alt = `Слайд ${cur + 1}`;
    $("#slide-count").textContent = `${cur + 1} / ${items.length}`;
    $("#slide-open").href = items[cur].url;
    $$("#slide-strip .slide-thumb").forEach((t, j) => t.classList.toggle("active", j === cur));
  };

  $("#slide-prev").addEventListener("click", () => go(cur - 1));
  $("#slide-next").addEventListener("click", () => go(cur + 1));
  $$("#slide-strip .slide-thumb").forEach((t) => t.addEventListener("click", () => go(+t.dataset.i)));

  listen(document, "keydown", (e) => {
    if (e.target.matches("input, textarea")) return;
    if (e.key === "ArrowLeft") { e.preventDefault(); go(cur - 1); }
    if (e.key === "ArrowRight") { e.preventDefault(); go(cur + 1); }
  });
}