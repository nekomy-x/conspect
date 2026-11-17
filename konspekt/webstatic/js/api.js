/* ============================================================
   api.js — сетевой слой: JSON-запросы и SSE-поток ИИ.
   ============================================================ */
"use strict";

const FRIENDLY = {
  400: "Запрос не понят сервером",
  401: "Сессия истекла — обнови страницу и войди заново",
  404: "Не найдено",
  500: "Что-то сломалось на сервере",
  504: "Нейросеть не ответила вовремя",
};

/* ---------- отмена запросов при навигации ----------
   Роутер перед каждым рендером обрывает запросы прошлого экрана.
   Оборванный запрос «подвешивается» (промис не завершается), поэтому
   устаревший экран не может перерисовать контент поверх нового —
   и при этом ни один catch не показывает ложную ошибку. */
let currentSignal = null;

export function setFetchSignal(sig) { currentSignal = sig; }

const hang = () => new Promise(() => {});

export async function api(path, opts = {}) {
  const explicit = opts.signal;            // отмена по воле пользователя
  const signal = explicit ?? currentSignal ?? undefined;
  let r;
  try {
    r = await fetch(path, { ...opts, signal });
  } catch (e) {
    if (e?.name === "AbortError") {
      // явная отмена — это сообщение для интерфейса;
      // обрыв из-за навигации — тихо забываем устаревший экран
      if (explicit) throw new Error("Запрос отменён");
      return hang();
    }
    throw new Error("Нет связи с сервером");
  }
  if (!r.ok) {
    let msg = "";
    try { msg = (await r.json())?.error || ""; } catch { /* не json */ }
    throw new Error(msg && msg.length < 200 ? msg : (FRIENDLY[r.status] || `Ошибка ${r.status}`));
  }
  return r.json();
}

export const getJSON = (path, opts) => api(path, opts);

export const postJSON = (path, body, signal) => api(path, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
  signal,
});

/**
 * Потоковый ответ ИИ (SSE над fetch).
 * onDelta(text) вызывается по мере генерации; возвращает полный текст.
 * Бросает исключение при ошибке — вызывающий решает, откатываться ли на
 * небуферизованный /api/ai/ask.
 */
export async function streamAI(payload, { signal, onDelta, onStart, onReasoning } = {}) {
  const r = await fetch("/api/ai/ask/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
    signal,
  });

  if (!r.ok) {
    let msg = "";
    try { msg = (await r.json())?.error || ""; } catch { /* не json */ }
    throw new Error(msg || FRIENDLY[r.status] || `Ошибка ${r.status}`);
  }
  if (!r.body) throw new Error("Поток недоступен");

  const reader = r.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  let full = "";
  let started = false;

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });

    let idx;
    while ((idx = buf.indexOf("\n\n")) >= 0) {
      const chunk = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      const line = chunk.split("\n").find((l) => l.startsWith("data:"));
      if (!line) continue;
      let ev;
      try { ev = JSON.parse(line.slice(5).trim()); } catch { continue; }

      if (ev.type === "start") {
        started = true;
        onStart?.();
      } else if (ev.type === "reasoning") {
        // модель размышляет — это не ответ, отдаём отдельным колбэком
        onReasoning?.(ev.text);
      } else if (ev.type === "delta") {
        full += ev.text;
        onDelta?.(ev.text, full);
      } else if (ev.type === "error") {
        throw new Error(ev.error || FRIENDLY[500]);
      } else if (ev.type === "done") {
        return full;
      }
    }
  }
  if (!started && !full) throw new Error("Пустой ответ");
  return full;
}

/** Тихая предзагрузка (ховер по дереву) — кэш браузера сделает остальное. */
export function prefetch(path) {
  try { fetch(path, { priority: "low" }).catch(() => {}); } catch { /* noop */ }
}

/**
 * Универсальный SSE-поток событий (fetch + reader).
 *
 * Нужен агенту: сервер шлёт не только текст ответа, но и «начал
 * инструмент», «инструмент ответил», «размышляю» — каждый кадр это
 * отдельное событие со своим типом. onEvent получает разобранный объект.
 */
export async function streamSSE(path, payload, { signal, onEvent } = {}) {
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
    signal,
  });

  if (!r.ok) {
    let msg = "";
    try { msg = (await r.json())?.error || ""; } catch { /* не json */ }
    throw new Error(msg || FRIENDLY[r.status] || `Ошибка ${r.status}`);
  }
  if (!r.body) throw new Error("Поток недоступен");

  const reader = r.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });

    let idx;
    while ((idx = buf.indexOf("\n\n")) >= 0) {
      const chunk = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      const line = chunk.split("\n").find((l) => l.startsWith("data:"));
      if (!line) continue;
      let ev;
      try { ev = JSON.parse(line.slice(5).trim()); } catch { continue; }
      onEvent?.(ev);
    }
  }
}