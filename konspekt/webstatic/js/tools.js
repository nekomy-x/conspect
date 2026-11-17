/* ============================================================
   tools.js — модалки и учебные инструменты: выжимка, квиз,
   флеш-карточки, заметки, стенограмма.
   ============================================================ */
"use strict";

import { $, $$, esc, trapFocus, toast, copyText, timeAgo } from "./util.js";
import { postJSON } from "./api.js";
import { renderMD } from "./md.js";
import { notes } from "./store.js";
import { openAiFor, send as aiSend } from "./ai.js";

let untrap = null;

export function openModal(html, { onEscape } = {}) {
  const ov = $("#modal-overlay");
  const m = $("#modal");
  m.innerHTML = `<button class="icon-btn modal-close" id="modal-x" type="button" aria-label="Закрыть">
      <i class="ph ph-x" aria-hidden="true"></i></button>${html}`;
  ov.hidden = false;
  ov.onclick = (e) => { if (e.target === ov) closeModal(); };
  $("#modal-x").onclick = () => closeModal();
  if (untrap) untrap();
  untrap = trapFocus(m, () => { closeModal(); onEscape?.(); });
}

export function closeModal() {
  $("#modal-overlay").hidden = true;
  $("#modal").innerHTML = "";
  if (untrap) { untrap(); untrap = null; }
}

const bodyEl = (id) => $(`#${id}`);

function setAnswer(html) {
  const el = bodyEl("ai-out");
  if (el) el.className = "ai-answer";
  if (el) el.innerHTML = html;
}

/* ---------- пояснение фрагмента: флеш-карточки (интерактив) ---------- */
export async function showFlashcards(text, cid) {
  openModal(`
    <div class="section-label" style="margin-top:0">Флеш-карточки</div>
    <div class="quote-source">«${esc(text.slice(0, 200))}${text.length > 200 ? "…" : ""}»</div>
    <div class="ai-answer pending" id="ai-out">ИИ составляет карточки…</div>`);

  let md;
  try {
    const r = await postJSON("/api/ai/explain", { text, mode: "flash", conspect_id: cid });
    md = r.answer || "";
  } catch (e) {
    setAnswer(`<span class="err-text">${esc(e.message)}</span>`);
    return;
  }

  const cards = [];
  for (const line of md.split("\n")) {
    const m = line.match(/\*\*(.+?)\*\*\s*[—–→:-]+\s*(.+)/);
    if (m) cards.push([m[1].trim(), m[2].trim()]);
  }
  if (!cards.length) { setAnswer(renderMD(md)); return; }

  let idx = 0;
  setAnswer("");
  bodyEl("ai-out").innerHTML = `
    <div class="flashcard" id="flashcard" role="button" tabindex="0" aria-label="Перевернуть карточку">
      <div class="flashcard-inner">
        <div class="flash-face front" id="fc-front"></div>
        <div class="flash-face back" id="fc-back"></div>
      </div>
    </div>
    <div class="fc-nav">
      <button class="btn sm" id="fc-prev" type="button"><i class="ph ph-caret-left"></i> Назад</button>
      <span class="meta" id="fc-idx"></span>
      <button class="btn sm" id="fc-next" type="button">Вперёд <i class="ph ph-caret-right"></i></button>
    </div>`;

  const upd = () => {
    bodyEl("fc-front").textContent = cards[idx][0];
    bodyEl("fc-back").textContent = cards[idx][1];
    bodyEl("fc-idx").textContent = `${idx + 1} / ${cards.length}`;
    $("#flashcard").classList.remove("flipped");
  };
  upd();
  const flip = () => $("#flashcard").classList.toggle("flipped");
  $("#flashcard").onclick = flip;
  $("#flashcard").onkeydown = (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); flip(); }
  };
  $("#fc-prev").onclick = (e) => { e.stopPropagation(); idx = (idx - 1 + cards.length) % cards.length; upd(); };
  $("#fc-next").onclick = (e) => { e.stopPropagation(); idx = (idx + 1) % cards.length; upd(); };
}

/* ---------- выжимка ---------- */
export async function showSummary(cid) {
  openModal(`
    <div class="section-label" style="margin-top:0">Выжимка лекции</div>
    <div class="ai-answer pending" id="ai-out">ИИ читает конспект — обычно меньше минуты…</div>`);
  try {
    const r = await postJSON("/api/ai/summary", { conspect_id: cid });
    setAnswer(renderMD(r.answer || ""));
  } catch (e) {
    setAnswer(`<span class="err-text">${esc(e.message)}</span>`);
  }
}

/* ---------- квиз ---------- */
export async function showQuiz(cid) {
  openModal(`
    <div class="quiz-modal" id="quiz-holder">
      <div class="section-label" style="margin-top:0">Квиз по лекции</div>
      <div class="pending">ИИ составляет вопросы…</div>
    </div>`);

  let qs;
  try {
    const r = await postJSON("/api/ai/quiz", { conspect_id: cid, topic: "" });
    qs = (r.questions || []).filter((q) => q.q && Array.isArray(q.options) && q.options.length >= 2);
  } catch (e) {
    $("#quiz-holder").innerHTML = `<div class="err-text">${esc(e.message)}</div>`;
    return;
  }
  if (!qs.length) {
    $("#quiz-holder").innerHTML = `<div class="err-text">Не удалось составить вопросы — попробуй ещё раз</div>`;
    return;
  }

  let cur = 0, correct = 0;
  const wrong = new Set();
  const letters = ["A", "B", "C", "D", "E"];

  const renderQ = () => {
    const q = qs[cur];
    $("#quiz-holder").innerHTML = `
      <div class="quiz-progress">${qs.map((_, i) =>
        `<i class="${i < cur ? (wrong.has(i) ? "wrongq" : "done") : i === cur ? "cur" : ""}"></i>`).join("")}</div>
      <div class="quiz-q">${esc(q.q)}</div>
      <div class="quiz-opts">
        ${q.options.map((opt, i) => `<button class="quiz-opt" type="button" data-i="${i}">
          <span class="opt-letter">${letters[i] || i + 1}</span>${esc(opt)}
        </button>`).join("")}
      </div>`;
    $$("#quiz-holder .quiz-opt").forEach((b) => {
      b.onclick = () => {
        const i = +b.dataset.i;
        const ok = i === q.correct;
        if (ok) correct++; else wrong.add(cur);
        $$("#quiz-holder .quiz-opt").forEach((bb, j) => {
          bb.disabled = true;
          if (j === q.correct) bb.classList.add("correct");
          else if (j === i && !ok) bb.classList.add("wrong");
        });
        setTimeout(() => {
          cur++;
          if (cur < qs.length) renderQ(); else renderResult();
        }, 850);
      };
    });
  };

  const renderResult = () => {
    const pct = Math.round((correct / qs.length) * 100);
    const mood = pct >= 80 ? "good" : pct >= 50 ? "mid" : "bad";
    const verdict = pct >= 80 ? "Готов к экзамену"
      : pct >= 50 ? "Почти — повтори слабые места" : "Перечитай конспект";
    $("#quiz-holder").innerHTML = `
      <div class="quiz-result">
        <div class="score ${mood}">${correct}/${qs.length}</div>
        <div class="meta">${verdict}</div>
        <div class="quiz-again-row">
          <button class="btn sm" id="quiz-again" type="button">
            <i class="ph ph-arrow-counter-clockwise"></i> Ещё раз</button>
          ${wrong.size ? `<button class="btn sm" id="quiz-wrong" type="button">
            <i class="ph ph-chat-circle-text"></i> Разобрать ошибки</button>` : ""}
        </div>
      </div>`;
    $("#quiz-again").onclick = () => { closeModal(); showQuiz(cid); };
    const w = $("#quiz-wrong");
    if (w) w.onclick = () => {
      const q = qs[[...wrong][0]];
      closeModal();
      openAiFor(null);
      aiSend(`Разбери, почему я ошибся: ${q.q}\n\nВерный ответ: ${q.options[q.correct]}`);
    };
  };

  renderQ();
}

/* ---------- заметки ---------- */
export function showNotes(cid, title) {
  const list = notes.list(cid);
  openModal(`
    <div class="section-label" style="margin-top:0">Заметки${title ? ` · ${esc(title)}` : ""}</div>
    ${list.length ? `<div class="notes-list">${list.map((n) => `
      <div class="note" data-id="${esc(n.id)}">
        <div class="note-body">${renderMD(n.text)}</div>
        <div class="note-foot">
          <span class="meta">${timeAgo(n.ts / 1000)}</span>
          <span class="note-actions">
            <button class="msg-act" data-act="copy" data-id="${esc(n.id)}" type="button">
              <i class="ph ph-clipboard-text"></i> Копировать</button>
            <button class="msg-act" data-act="del" data-id="${esc(n.id)}" type="button">
              <i class="ph ph-trash"></i> Удалить</button>
          </span>
        </div>
      </div>`).join("")}</div>`
      : `<div class="empty"><i class="ph ph-notebook empty-ph"></i>Заметок пока нет.<br>
         Сохраняй ответы ИИ кнопкой <b>«В заметки»</b>.</div>`}`);

  const host = $("#modal");
  host.onclick = async (e) => {
    const b = e.target.closest("[data-act]");
    if (!b) return;
    const id = b.dataset.id;
    const note = notes.list().find((n) => n.id === id);
    if (b.dataset.act === "copy") { await copyText(note?.text || ""); return; }
    if (b.dataset.act === "del") {
      notes.remove(id);
      toast("Заметка удалена");
      showNotes(cid, title);
    }
  };
}

/* ---------- стенограмма ---------- */
export async function showTranscript(cid, title) {
  openModal(`
    <div class="section-label" style="margin-top:0">Стенограмма${title ? ` · ${esc(title)}` : ""}</div>
    <div class="pending" id="ai-out">Загружаю…</div>`);
  try {
    const r = await fetch(`/media/conspects/${encodeURIComponent(cid)}/transcript.txt`);
    if (!r.ok) throw new Error("Стенограмма недоступна");
    const text = await r.text();
    const out = bodyEl("ai-out");
    if (!out) return;
    out.className = "ai-answer transcript";
    out.textContent = text;
  } catch (e) {
    setAnswer(`<span class="err-text">${esc(e.message)}</span>`);
  }
}

/* ---------- ошибки в модалках ---------- */
export const errHTML = (msg) => `<span class="err-text">${esc(msg)}</span>`;