/* ============================================================
   store.js — состояние приложения и всё, что переживает перезагрузку.
   Ключи localStorage собраны здесь, чтобы не расползались по модулям.
   ============================================================ */
"use strict";

const K = {
  prefs: "konspect.ui.v1",
  read: "konspect.read.v1",       // { [cid]: { pct, sec, ts } }
  pins: "konspect.pins.v1",       // [cid]
  last: "konspect.last.v1",       // { cid, sec, title, pct, ts }
  threads: "konspect.ai.threads.v1",
  theme: "konspect-theme",
};

function lsGet(key, fallback) {
  try {
    const raw = localStorage.getItem(key);
    return raw == null ? fallback : JSON.parse(raw);
  } catch { return fallback; }
}

function lsSet(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* приватный режим */ }
}

/* ---------- настройки раскладки ---------- */
export const PANE_LIMITS = {
  nav:  { min: 56, max: 320, def: 230, rail: 56 },
  tree: { min: 200, max: 460, def: 300 },
  ai:   { min: 320, max: 720, def: 400 },
};

const PREFS_DEFAULTS = {
  nav: 230,
  tree: 300,
  ai: 400,
  railCollapsed: false,
  treeOpen: true,
  aiOpen: false,
  treeMode: "solo",   // solo | multi
  treeWidthHint: 0,
};

export const prefs = Object.assign({}, PREFS_DEFAULTS, lsGet(K.prefs, {}));

export function savePrefs() {
  const { nav, tree, ai, railCollapsed, treeOpen, aiOpen, treeMode } = prefs;
  lsSet(K.prefs, { nav, tree, ai, railCollapsed, treeOpen, aiOpen, treeMode });
}

/* ---------- состояние рантайма ---------- */
export const state = {
  route: { name: "home", args: [] },
  conspects: null,        // список из /api/conspects
  docCache: new Map(),    // cid -> /api/conspect ответ
  assignments: null,
  schedule: null,
  stats: null,
  activeConspect: null,   // cid открытой лекции
  activeSec: null,        // индекс подраздела, к которому перешли
  scrollMemo: new Map(),  // ключ роута -> scrollTop
  searchIndex: null,
  ai: {
    threads: lsGet(K.threads, {}),
    ctxKey: "global",
    ctx: { kind: "global", title: "Вся учёба" },
    busy: false,
    abort: null,
  },
};

/* ---------- прогресс чтения ---------- */
const readMarks = lsGet(K.read, {});

export const read = {
  get(cid) { return readMarks[cid] || null; },
  all() { return readMarks; },
  /** pct: 0..1; помечаем прочитанным на 90%+, иначе «в процессе». */
  set(cid, pct, sec = null) {
    if (!cid) return;
    const prev = readMarks[cid] || { pct: 0 };
    const next = Math.max(prev.pct || 0, pct);
    readMarks[cid] = {
      pct: next >= 0.9 ? 1 : next,
      raw: next >= 0.9 ? next : Math.min(next, 0.85),
      sec: sec ?? prev.sec ?? null,
      ts: Date.now(),
    };
    lsSet(K.read, readMarks);
  },
  state(cid) {
    const m = readMarks[cid];
    if (!m) return "new";
    return (m.raw ?? m.pct) >= 0.9 || m.pct >= 1 ? "done" : "progress";
  },
};

/* ---------- закреплённые ---------- */
let pinList = lsGet(K.pins, []);

export const pins = {
  list() { return pinList.slice(); },
  has(id) { return pinList.includes(id); },
  toggle(id) {
    pinList = pinList.includes(id) ? pinList.filter((x) => x !== id) : [id, ...pinList];
    lsSet(K.pins, pinList);
    return pinList.includes(id);
  },
};

/* ---------- «продолжить» ---------- */
export const lastRead = {
  get() { return lsGet(K.last, null); },
  set(cid, title, sec = null, pct = 0) {
    if (!cid) return;
    lsSet(K.last, { cid, title, sec, pct, ts: Date.now() });
  },
};

/* ---------- треды ИИ ---------- */
const THREAD_MAX_MSGS = 40;
const THREAD_MAX_CHARS = 200_000;

export const threads = {
  get(key) {
    const t = state.ai.threads[key];
    return t ? t.msgs : [];
  },
  scroll(key) {
    const t = state.ai.threads[key];
    return t ? t.scroll || 0 : 0;
  },
  push(key, msg) {
    const t = state.ai.threads[key] || { msgs: [], scroll: 0, updatedAt: 0 };
    t.msgs.push(msg);
    if (t.msgs.length > THREAD_MAX_MSGS) t.msgs = t.msgs.slice(-THREAD_MAX_MSGS);
    let size = 0;
    for (const m of t.msgs) size += (m.text || "").length;
    while (size > THREAD_MAX_CHARS && t.msgs.length > 4) {
      size -= (t.msgs[0].text || "").length;
      t.msgs.shift();
    }
    t.updatedAt = Date.now();
    state.ai.threads[key] = t;
    lsSet(K.threads, state.ai.threads);
    return t;
  },
  update(key, index, patch) {
    const t = state.ai.threads[key];
    if (!t || !t.msgs[index]) return;
    Object.assign(t.msgs[index], patch);
    lsSet(K.threads, state.ai.threads);
  },
  truncateAfter(key, index) {
    const t = state.ai.threads[key];
    if (!t) return;
    t.msgs = t.msgs.slice(0, index);
    lsSet(K.threads, state.ai.threads);
  },
  clear(key) {
    delete state.ai.threads[key];
    lsSet(K.threads, state.ai.threads);
  },
  setScroll(key, scroll) {
    const t = state.ai.threads[key];
    if (!t) return;
    t.scroll = scroll;
  },
  saveScroll(key, scroll) {
    const t = state.ai.threads[key];
    if (!t) { state.ai.threads[key] = { msgs: [], scroll, updatedAt: Date.now() }; }
    else t.scroll = scroll;
    lsSet(K.threads, state.ai.threads);
  },
};

/* ---------- заметки ---------- */
const K_NOTE = "konspect.notes.v1";
let noteList = lsGet(K_NOTE, []);

export const notes = {
  /** Заметки по конспекту (или «без привязки», если cid не передан). */
  list(cid = undefined) {
    const all = noteList.slice().sort((a, b) => b.ts - a.ts);
    return cid === undefined ? all : all.filter((n) => n.cid === cid);
  },
  add(cid, text, title = "") {
    if (!text || !text.trim()) return;
    noteList.unshift({
      id: `n${Date.now()}${Math.floor(Math.random() * 1000)}`,
      cid: cid || null,
      title,
      text: text.trim().slice(0, 20000),
      ts: Date.now(),
    });
    if (noteList.length > 200) noteList = noteList.slice(0, 200);
    lsSet(K_NOTE, noteList);
  },
  remove(id) {
    noteList = noteList.filter((n) => n.id !== id);
    lsSet(K_NOTE, noteList);
  },
  count(cid) { return cid ? noteList.filter((n) => n.cid === cid).length : noteList.length; },
};

export const storageKeys = K;