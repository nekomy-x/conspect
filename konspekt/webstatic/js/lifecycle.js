/* ============================================================
   lifecycle.js — единый реестр уборки для экранов.

   Раньше обработчики скролла/выделения вешались при каждом рендере и
   никогда не снимались: после 20 открытых лекций на один скролл
   работало 20 обработчиков. Теперь каждый экран регистрирует свои
   слушатели здесь, а роутер вызывает runCleanup() перед новым рендером.
   ============================================================ */
"use strict";

let fns = [];

/** Зарегистрировать функцию уборки для текущего экрана. */
export function onCleanup(fn) {
  if (typeof fn === "function") fns.push(fn);
}

/** Короткая форма: addEventListener, который снимут сам. */
export function listen(target, type, handler, opts) {
  target.addEventListener(type, handler, opts);
  onCleanup(() => target.removeEventListener(type, handler, opts));
}

export function runCleanup() {
  for (const fn of fns) {
    try { fn(); } catch { /* один сломанный обработчик не должен ломать переход */ }
  }
  fns = [];
}

export const cleanupCount = () => fns.length;