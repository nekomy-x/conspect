"""Универсальные инструменты агента-менеджера: веб-поиск, чтение любых URL,
заметки, калькулятор, слежка за страницами.

Поиск — DuckDuckGo html (POST, без ключей).
Чтение URL — httpx + BeautifulSoup → текст (Moodle, вики, статьи).
Заметки — таблица kv БД (persist).
"""
from __future__ import annotations

import ast
import logging
import re
import time
from datetime import datetime
from urllib.parse import urljoin, quote_plus

import httpx
from bs4 import BeautifulSoup

from .db import db

log = logging.getLogger("konspect.tools")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36")


# ------------------------------------------------------------------ поиск
async def web_search(query: str, max_results: int = 5) -> dict:
    """Поиск в интернете (DuckDuckGo). Возвращает заголовки+ссылки+сниппеты."""
    async with httpx.AsyncClient(timeout=25, headers={
            "User-Agent": UA, "Referer": "https://html.duckduckgo.com/"},
            follow_redirects=True) as c:
        r = await c.post("https://html.duckduckgo.com/html/", data={"q": query})
    if r.status_code != 200:
        return {"error": f"поиск недоступен ({r.status_code})"}
    soup = BeautifulSoup(r.text, "html.parser")
    results = []
    seen = set()
    for res in soup.select(".result")[:max(max_results, 1) * 2]:
        a = res.select_one(".result__a")
        if not a:
            continue
        href = a.get("href", "")
        # ddg redirect: /l/?uddg=<real>
        m = re.search(r"uddg=([^&]+)", href)
        if m:
            from urllib.parse import unquote
            href = unquote(m.group(1))
        title = a.get_text(strip=True)
        if not title or href in seen or "duckduckgo" in href:
            continue
        seen.add(href)
        snip_el = res.select_one(".result__snippet")
        results.append({"title": title[:100], "url": href,
                        "snippet": (snip_el.get_text(" ", strip=True)
                                    if snip_el else "")[:250]})
        if len(results) >= max_results:
            break
    return {"query": query, "results": results}


# ---------------------------------------------------------------- чтение URL
async def read_url(url: str) -> dict:
    """Прочитать любую страницу по URL → текст (статьи, вики, Moodle...)."""
    if not url.startswith("http"):
        url = "https://" + url
    try:
        async with httpx.AsyncClient(timeout=30, headers={"User-Agent": UA},
                                     follow_redirects=True) as c:
            r = await c.get(url)
    except Exception as e:  # noqa: BLE001
        return {"error": f"не открылся: {e}"}
    if r.status_code != 200:
        return {"error": f"HTTP {r.status_code}"}
    ctype = r.headers.get("content-type", "")
    if "pdf" in ctype:
        try:
            import pymupdf
            import io
            doc = pymupdf.open(stream=r.content, filetype="pdf")
            text = "\n".join(p.get_text() for p in doc[:30])
            return {"url": url, "type": "pdf", "text": text[:30000]}
        except Exception:  # noqa: BLE001
            return {"error": "pdf не прочитался"}
    if "html" not in ctype and "text" not in ctype:
        return {"url": url, "type": ctype, "text": "(не текстовый контент)"}
    soup = BeautifulSoup(r.text, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
        tag.decompose()
    title = soup.title.string if soup.title else ""
    body = soup.body or soup
    text = body.get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return {"url": url, "title": title[:150], "text": text[:30000]}


# ------------------------------------------------------------------- заметки
async def notes(action: str = "list", text: str = "", tags: str = "",
                note_id: int | None = None) -> dict:
    """Заметки менеджера: list / add / delete. Хранятся в БД."""
    all_notes = db.kv_get("notes", [])
    if not isinstance(all_notes, list):
        all_notes = []
    if action == "add":
        if not text:
            return {"error": "пустой текст"}
        note = {"id": (max((n["id"] for n in all_notes), default=0) + 1),
                "text": text[:2000], "tags": [t for t in tags.split(",") if t.strip()],
                "created": datetime.now().strftime("%d.%m %H:%M")}
        all_notes.append(note)
        db.kv_set("notes", all_notes)
        return {"ok": True, "note": note}
    if action == "delete" and note_id:
        all_notes = [n for n in all_notes if n["id"] != int(note_id)]
        db.kv_set("notes", all_notes)
        return {"ok": True, "deleted": note_id}
    # list
    return {"notes": [{"id": n["id"], "text": n["text"][:150],
                       "tags": n.get("tags", []), "created": n["created"]}
                      for n in all_notes[-30:]]}


# --------------------------------------------------------------- калькулятор
def _calc(expr: str) -> str:
    """Безопасный расчёт: только числа и операторы."""
    node = ast.parse(expr, mode="eval")
    allowed = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant,
               ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod,
               ast.Pow, ast.USub, ast.UAdd)
    for n in ast.walk(node):
        if not isinstance(n, allowed):
            raise ValueError("недопустимое выражение")
        if isinstance(n, ast.Constant) and not isinstance(n.value, (int, float)):
            raise ValueError("только числа")
    return str(eval(compile(node, "<calc>", "eval")))


async def calculate(expression: str) -> dict:
    try:
        result = _calc(expression)
        return {"expression": expression, "result": result}
    except Exception as e:  # noqa: BLE001
        return {"error": f"не посчиталось: {e}"}


# ------------------------------------------------------- слежка за страницей
async def watch_url(url: str, note: str = "") -> dict:
    """Следить за страницей: при изменении контента бот уведомит."""
    # читаем текущий хэш
    page = await read_url(url)
    if "error" in page:
        return page
    content_hash = str(hash(page.get("text", "")[:5000]))
    watches = db.kv_get("url_watches", [])
    if not isinstance(watches, list):
        watches = []
    watches = [w for w in watches if w["url"] != url]
    watches.append({"url": url, "hash": content_hash, "note": note[:200],
                    "last_check": int(time.time())})
    db.kv_set("url_watches", watches)
    return {"ok": True, "watching": url, "note": note[:100],
            "проверка": "каждые 10 минут"}


async def check_url_watches() -> list[dict]:
    """Проверить все отслеживаемые страницы (вызывается планировщиком)."""
    watches = db.kv_get("url_watches", [])
    changes = []
    for w in watches[:10]:
        try:
            page = await read_url(w["url"])
            h = str(hash(page.get("text", "")[:5000]))
            if h != w["hash"]:
                changes.append(w)
                w["hash"] = h
            w["last_check"] = int(time.time())
        except Exception as e:  # noqa: BLE001
            log.warning("watch %s: %s", w["url"], e)
    db.kv_set("url_watches", watches)
    return changes
