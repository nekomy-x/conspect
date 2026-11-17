/* ============================================================
   md.js — единый markdown-рендерер для ответов ИИ.
   Умеет: заголовки, списки (в т.ч. вложенные), таблицы, код-блоки
   с копированием, цитаты, ссылки, жирный/курсив, hr, формулы-как-текст.
   Раньше конспекты рендерились на сервере, а ответы ИИ — «мини-MD»
   без таблиц; теперь один код на всё клиентское.
   ============================================================ */
"use strict";

import { esc } from "./util.js";

/* ---------- инлайн ---------- */
function inline(s) {
  let t = esc(s);

  // защищаем код-спаны от дальнейших замен
  const codes = [];
  t = t.replace(/`([^`]+)`/g, (_, c) => {
    codes.push(c);
    return `\u0000${codes.length - 1}\u0000`;
  });

  // формулы оставляем как есть, но моно-шрифтом
  t = t.replace(/\$([^$\n]+)\$/g, '<span class="math">$1</span>');

  // ссылки [текст](url)
  t = t.replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g,
    '<a href="$2" target="_blank" rel="noopener">$1</a>');

  t = t.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  t = t.replace(/(^|[\s(])\*([^*\n]+)\*/g, "$1<em>$2</em>");
  t = t.replace(/~~([^~]+)~~/g, "<del>$1</del>");

  return t.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${codes[+i]}</code>`);
}

const isTableSep = (line) => /^\s*\|?[\s:|-]+\|[\s:|-]*$/.test(line) && line.includes("-");
const cells = (line) => line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());

/* ---------- блоки ---------- */
export function renderMD(src) {
  const lines = String(src ?? "").replace(/\r/g, "").split("\n");
  const out = [];

  // список ul/ol с поддержкой одного уровня вложенности по отступу
  const listRe = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;

  let i = 0;
  while (i < lines.length) {
    const line = lines[i];

    if (!line.trim()) { i++; continue; }

    // код-блок
    if (line.trim().startsWith("```")) {
      const lang = line.trim().slice(3).trim();
      const buf = [];
      i++;
      while (i < lines.length && !lines[i].trim().startsWith("```")) buf.push(lines[i++]);
      i++;
      const raw = buf.join("\n");
      out.push(
        `<div class="codeblock"><div class="code-lang">${esc(lang || "code")}` +
        `<button class="copy-btn" data-copy="${esc(raw)}" type="button" aria-label="Скопировать код">` +
        `<i class="ph ph-clipboard-text"></i></button></div>` +
        `<pre><code>${esc(raw)}</code></pre></div>`);
      continue;
    }

    // заголовок
    const h = line.match(/^(#{1,4})\s+(.+)$/);
    if (h) {
      const lvl = Math.min(h[1].length + 1, 5);
      out.push(`<h${lvl}>${inline(h[2])}</h${lvl}>`);
      i++;
      continue;
    }

    // hr
    if (/^(---|\*\*\*|___)\s*$/.test(line)) { out.push("<hr>"); i++; continue; }

    // таблица
    if (line.trim().startsWith("|") && i + 1 < lines.length && isTableSep(lines[i + 1])) {
      const head = cells(line);
      i += 2;
      const rows = [];
      while (i < lines.length && lines[i].trim().startsWith("|")) rows.push(cells(lines[i++]));
      const thead = "<tr>" + head.map((c) => `<th>${inline(c)}</th>`).join("") + "</tr>";
      const tbody = rows.map((r) =>
        "<tr>" + r.map((c) => `<td>${inline(c)}</td>`).join("") + "</tr>").join("");
      out.push(`<div class="tablewrap"><table><thead>${thead}</thead><tbody>${tbody}</tbody></table></div>`);
      continue;
    }

    // цитата
    if (line.trim().startsWith(">")) {
      const buf = [];
      while (i < lines.length && lines[i].trim().startsWith(">")) {
        buf.push(lines[i].trim().replace(/^>\s?/, ""));
        i++;
      }
      out.push(`<blockquote>${inline(buf.join(" "))}</blockquote>`);
      continue;
    }

    // список
    if (listRe.test(line)) {
      const ordered = /^\s*\d/.test(line);
      const items = [];
      let depth = 0;
      while (i < lines.length) {
        const m = lines[i].match(listRe);
        if (!m) {
          // продолжение пункта (висячая строка) — приклеиваем к предыдущему
          if (items.length && lines[i].trim() && !/^\s{2,}/.test(lines[i]) === false) {
            items[items.length - 1] += " " + inline(lines[i].trim());
            i++;
            continue;
          }
          break;
        }
        const indent = m[1].length;
        const lvl = indent >= 2 ? 1 : 0;
        if (lvl > depth) depth = lvl;
        items.push({ lvl, html: inline(m[3]) });
        i++;
      }
      const render = (lvl) => {
        const tag = ordered ? "ol" : "ul";
        const body = items
          .filter((it) => it.lvl === lvl)
          .map((it) => `<li>${it.html}</li>`)
          .join("");
        const nested = lvl === 0 && items.some((it) => it.lvl > 0) ? render(lvl + 1) : "";
        return `<${tag}>${body}${nested}</${tag}>`;
      };
      out.push(render(0));
      continue;
    }

    // абзац: собираем до пустой строки
    const buf = [];
    while (i < lines.length && lines[i].trim() &&
           !listRe.test(lines[i]) && !/^(#{1,4})\s/.test(lines[i]) &&
           !lines[i].trim().startsWith("```") && !lines[i].trim().startsWith(">") &&
           !lines[i].trim().startsWith("|") && !/^(---|\*\*\*|___)\s*$/.test(lines[i])) {
      buf.push(lines[i++]);
    }
    if (buf.length) out.push(`<p>${inline(buf.join("<br>"))}</p>`);
  }

  return out.join("");
}

/** Разбивает длинный ответ для «печатания» — не рвёт слова. */
export function chunkText(text, size = 3) {
  const parts = String(text).match(new RegExp(`[\\s\\S]{1,${size}}`, "g"));
  return parts || [];
}