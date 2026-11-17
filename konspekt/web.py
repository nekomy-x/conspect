"""Веб-хаб учебы: новый интерфейс — тёмная тема, левая панель, AI-помощник.

Стек: Starlette + uvicorn (встроены в проект), статика отдаётся из
konspekt/webstatic. Фронтенд — единый SPA-шелл (app.js), страницы рендерятся
на клиенте из JSON-API. AI-эндпоинты завёрнуты в asyncio.gather поверх
клиента из ai.client.

Страницы (клиентский роутинг #/...):
- #/            дашборд: сегодня, дедлайны, последние конспекты
- #/conspects   библиотека конспектов (по курсам)
- #/conspect/id конспект: TOC, выделение → AI, заметки, квизы, флеш-карточки
- #/slides/id   слайды лекции
- #/assignments задания
- #/assignment/id разбор задания
- #/schedule    расписание (неделями)
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import secrets
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import unquote

from starlette.applications import Starlette
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .config import cfg

log = logging.getLogger("konspect.web")

PORT = int(__import__("os").getenv("WEB_PORT", "8080"))
CONSPECTS_DIR = cfg.data_dir / "conspects"
ASSIGNMENTS_DIR = cfg.data_dir / "assignments"
STATIC_DIR = Path(__file__).parent / "webstatic"


# ------------------------------------------------------------------ auth
import os as _os

WEB_PASSWORD = _os.getenv("WEB_PASSWORD", "")  # пароль из .env
_sessions: dict[str, float] = {}  # token -> expiry ts
SESSION_TTL = 14 * 86400  # 2 недели


def _check_password(pw: str) -> bool:
    if not WEB_PASSWORD:
        return True  # без пароля в конфиге — доступ открыт
    return hmac.compare_digest(pw, WEB_PASSWORD)


def _make_session() -> str:
    token = secrets.token_urlsafe(32)
    _sessions[token] = datetime.now().timestamp() + SESSION_TTL
    # чистим протухшие
    now = datetime.now().timestamp()
    for t in list(_sessions):
        if _sessions[t] < now:
            del _sessions[t]
    return token


def _valid_session(request) -> bool:
    if not WEB_PASSWORD:
        return True
    token = request.cookies.get("hub_session", "")
    exp = _sessions.get(token, 0)
    if exp < datetime.now().timestamp():
        _sessions.pop(token, None)
        return False
    return True


LOGIN_HTML = """<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Учебный хаб — вход</title>
<style>
:root { --bg:#0d1226; --card:#161d3a; --acc:#8b5cf6; --tx:#eef1ff; --err:#ff6b6b; }
* { box-sizing:border-box; margin:0; }
body { background:var(--bg); color:var(--tx); min-height:100vh;
       display:flex; align-items:center; justify-content:center;
       font:16px/1.6 'Segoe UI',system-ui,sans-serif;
       background-image:radial-gradient(ellipse 80% 50% at 50% -20%,
                       rgba(139,92,246,.25), transparent); }
.card { background:var(--card); border-radius:24px; padding:3rem;
        width:min(380px, 92vw); box-shadow:0 20px 60px rgba(0,0,0,.5);
        animation:fadeUp .6s ease; }
@keyframes fadeUp { from { opacity:0; transform:translateY(24px); }
                    to { opacity:1; transform:translateY(0); } }
h1 { font-size:1.5rem; text-align:center; margin-bottom:.5rem; }
.sub { text-align:center; color:#8a91b4; font-size:.9rem; margin-bottom:2rem; }
input { width:100%; padding:.9rem 1.1rem; border-radius:14px; border:2px solid #2a3358;
        background:#0d1226; color:var(--tx); font-size:1rem; outline:none;
        transition:border .2s; }
input:focus { border-color:var(--acc); }
button { width:100%; padding:.9rem; margin-top:1.2rem; border:none; border-radius:14px;
         background:linear-gradient(135deg,#8b5cf6,#6366f1); color:#fff;
         font-size:1rem; font-weight:600; cursor:pointer; transition:transform .15s; }
button:hover { transform:translateY(-2px); }
.err { color:var(--err); text-align:center; margin-top:1rem; font-size:.9rem;
       display:none; }
</style></head>
<body>
<div class="card">
  <h1>🎓 Учебный хаб</h1>
  <p class="sub">Введите пароль для доступа</p>
  <form method="POST" action="/login">
    <input type="password" name="password" placeholder="Пароль" autofocus required>
    <button type="submit">Войти</button>
    <p class="err" id="e">Неверный пароль</p>
  </form>
</div>
<script>
  if (location.search.includes("wrong")) document.getElementById("e").style.display = "block";
</script>
</body></html>"""


async def login_page(request):
    if _valid_session(request):
        return Response(status_code=302, headers={"location": "/"})
    return Response(LOGIN_HTML, media_type="text/html; charset=utf-8")


async def login_submit(request):
    form = await request.form()
    pw = form.get("password", "")
    if _check_password(pw):
        token = _make_session()
        resp = Response(status_code=302, headers={"location": "/"})
        resp.set_cookie("hub_session", token, max_age=SESSION_TTL,
                        httponly=True, samesite="lax")
        return resp
    return Response(status_code=302, headers={"location": "/login?wrong=1"})


async def logout(request):
    token = request.cookies.get("hub_session", "")
    _sessions.pop(token, None)
    resp = Response(status_code=302, headers={"location": "/login"})
    resp.delete_cookie("hub_session")
    return resp


from starlette.middleware.base import BaseHTTPMiddleware


class AuthMiddleware(BaseHTTPMiddleware):
    """Всё, кроме /login и /static закрыто паролем (если задан)."""

    async def dispatch(self, request, call_next):
        if not WEB_PASSWORD:
            return await call_next(request)
        path = request.url.path
        if path in ("/login",) or path.startswith("/static"):
            return await call_next(request)
        if not _valid_session(request):
            if path.startswith("/api/"):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            return Response(status_code=302, headers={"location": "/login"})
        return await call_next(request)

WEEKDAYS = ["понедельник", "вторник", "среда", "четверг",
            "пятница", "суббота", "воскресенье"]


# ------------------------------------------------------------------ markdown
def md_to_html(text: str) -> str:
    """Простой Markdown → HTML: заголовки, списки, таблицы, код, цитаты.

    Без внешних зависимостей; экранирует HTML-спецсимволы.
    """
    import html as h

    def esc(s: str) -> str:
        return h.escape(s, quote=False)

    def safe_url(url: str) -> str:
        u = url.strip()
        return u if re.match(r"^(https?:|mailto:|/|#)", u, re.I) else "#"

    def inline(s: str) -> str:
        s = esc(s)
        s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"(?<!\w)\*([^*\n]+)\*(?!\w)", r"<em>\1</em>", s)
        s = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)",
                   lambda m: f'<a href="{safe_url(m.group(2))}" target="_blank" '
                             f'rel="noopener">{m.group(1)}</a>', s)
        return s

    lines = text.split("\n")
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]

        # код-блок
        if line.strip().startswith("```"):
            lang = line.strip()[3:].strip()
            buf = []
            i += 1
            while i < n and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1
            out.append(
                f'<div class="codeblock"><div class="code-lang">{esc(lang) or "code"}</div>'
                f"<pre><code>{esc(chr(10).join(buf))}</code></pre></div>")
            continue

        # заголовки
        m = re.match(r"^(#{1,4})\s+(.+)$", line)
        if m:
            lvl = min(len(m.group(1)) + 1, 5)
            out.append(f"<h{lvl}>{inline(m.group(2).strip())}</h{lvl}>")
            i += 1
            continue

        # hr
        if re.match(r"^(---|\*\*\*|___)\s*$", line):
            out.append("<hr>")
            i += 1
            continue

        # таблица
        if line.strip().startswith("|") and i + 1 < n and re.match(r"^\s*\|[\s:|-]+\|\s*$", lines[i + 1]):
            header = [c.strip() for c in line.strip().strip("|").split("|")]
            i += 2
            rows = []
            while i < n and lines[i].strip().startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            thead = "<tr>" + "".join(f"<th>{inline(c)}</th>" for c in header) + "</tr>"
            tbody = "".join("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>" for r in rows)
            out.append(f"<div class='tablewrap'><table><thead>{thead}</thead><tbody>{tbody}</tbody></table></div>")
            continue

        # цитата
        if line.strip().startswith(">"):
            buf = []
            while i < n and lines[i].strip().startswith(">"):
                buf.append(lines[i].strip().lstrip(">").strip())
                i += 1
            out.append(f"<blockquote>{inline(' '.join(buf))}</blockquote>")
            continue

        # списки
        m = re.match(r"^\s*[-*]\s+(.+)$", line)
        if m:
            items = []
            while i < n:
                mm = re.match(r"^\s*[-*]\s+(.+)$", lines[i])
                if not mm:
                    break
                items.append(f"<li>{inline(mm.group(1).strip())}</li>")
                i += 1
            out.append(f"<ul>{''.join(items)}</ul>")
            continue

        m = re.match(r"^\s*\d+[.)]\s+(.+)$", line)
        if m:
            items = []
            while i < n:
                mm = re.match(r"^\s*\d+[.)]\s+(.+)$", lines[i])
                if not mm:
                    break
                items.append(f"<li>{inline(mm.group(1).strip())}</li>")
                i += 1
            out.append(f"<ol>{''.join(items)}</ol>")
            continue

        # пустая строка
        if not line.strip():
            i += 1
            continue

        # абзац (склеиваем строки)
        buf = [line.strip()]
        i += 1
        while i < n and lines[i].strip() and not re.match(
                r"^(#{1,4}\s|```|\s*[-*]\s|\s*\d+[.)]\s|>|\||---)", lines[i]):
            buf.append(lines[i].strip())
            i += 1
        out.append(f"<p>{inline(' '.join(buf))}</p>")

    return "\n".join(out)


def md_first_title(md_path: Path) -> str:
    try:
        text = md_path.read_text(encoding="utf-8")
    except OSError:
        return ""
    m = re.search(r"^#\s+(.+)$", text, re.M)
    if m:
        return m.group(1).strip()
    # если заголовка нет — первая непустая строка
    for line in text.split("\n"):
        if line.strip() and not line.startswith(">"):
            return line.strip()[:70]
    return ""


def conspect_course(slug: str) -> str:
    """Название курса для конспекта: из БД записей (playback_url содержит slug)."""
    try:
        from .db import db
        with db._conn() as c:  # noqa: SLF001 — утилитарный обходной путь
            recs = [dict(r) for r in c.execute("SELECT * FROM recordings")]
        for r in recs:
            if slug in r.get("playback_url", ""):
                cid = r.get("course_id")
                for c in db.courses():
                    if c["id"] == cid:
                        return _clean_ws(c["name"])
    except Exception:  # noqa: BLE001
        pass
    return ""


# ------------------------------------------------------------------ data api
def md_sections(text: str, limit: int = 80) -> list[dict]:
    """Подразделы конспекта (h2/h3) — для дерева тем в левой панели.

    Читалка строит TOC из уже отрендеренного HTML, а дереву нужны заголовки
    заранее и для ВСЕХ конспектов сразу — разбираем markdown на месте.
    """
    out: list[dict] = []
    for m in re.finditer(r"^(#{2,3})\s+(.+)$", text, re.M):
        title = re.sub(r"[`*_]", "", m.group(2))
        title = _clean_ws(title)
        if title:
            out.append({"level": len(m.group(1)), "title": title[:140]})
        if len(out) >= limit:
            break
    return out


def get_conspects() -> list[dict]:
    out = []
    if CONSPECTS_DIR.exists():
        for d in sorted(CONSPECTS_DIR.iterdir()):
            md = d / "conspect.md"
            if not (md.exists() and d.is_dir()):
                continue
            text = ""
            try:
                text = md.read_text(encoding="utf-8")
            except OSError:
                pass
            title = md_first_title(md)
            slides_dir = d / "slides"
            n_slides = len(list(slides_dir.glob("*.png"))) if slides_dir.exists() else 0
            # первая строка-цитата как «эксперза»
            quote = ""
            qm = re.search(r"^>\s*[«\"](.+?)[»\"]", text, re.M)
            if qm:
                quote = qm.group(1)[:120]
            mtime = md.stat().st_mtime
            words = len(text.split())
            out.append({
                "id": d.name,
                "title": title[:80] or d.name[:40],
                "course": conspect_course(d.name),
                "size_kb": md.stat().st_size // 1024,
                "words": words,
                "reading_min": max(1, round(words / 180)),
                "sections": md_sections(text),
                "has_presentation": (d / "presentation.html").exists(),
                "n_slides": n_slides,
                "has_transcript": (d / "transcript.txt").exists(),
                "mtime": int(mtime),
                "quote": quote,
            })
    out.sort(key=lambda x: -x["mtime"])
    return out


def get_assignments(active_only: bool = True) -> list[dict]:
    from .db import db
    now = datetime.now().timestamp()
    courses = {c["id"]: c["name"] for c in db.courses()}
    rows = []
    for a in db.assignments():
        dl = a.get("deadline_ts") or 0
        if active_only:
            # только реальные: с дедлайном за последние 30 дней и позже
            if not dl or dl < now - 30 * 86400:
                continue
        state = a.get("state", "draft")
        left = dl - now if dl else 0
        rows.append({
            "cmid": a["activity_id"],
            "name": _clean_ws(a["name"]).replace("_", " ")[:90],
            "course": _clean_ws(courses.get(a.get("course_id", 0), "")),
            "deadline_ts": dl,
            "deadline": datetime.fromtimestamp(dl).strftime("%d.%m %H:%M") if dl else "",
            "status": ("done" if state in ("submitted", "ready") else
                       "overdue" if left < 0 else
                       "urgent" if left < 86400 * 3 else "ok"),
            "left_days": max(0, int(left // 86400)),
            "left_hours": max(0, int(left // 3600)),
            "state": state,
            "has_answer": (a.get("answer_path") is not None) or bool(a.get("answer")),
        })
    rows.sort(key=lambda x: x["deadline_ts"] or 0)
    return rows



def _clean_ws(s: str) -> str:
    """Схлопнуть лишние пробелы и убрать пробел перед запятой."""
    s = re.sub(r"\s+", " ", s or "").strip()
    s = re.sub(r"\s+,", ",", s)
    return s


def _clean_place(s: str) -> str:
    """Нормализовать «ауд. 40» → «ауд. 40», «ауд.40» → «ауд. 40»."""
    s = _clean_ws(s)
    s = re.sub(r"\bауд\.?\s*", "ауд. ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def schedule_weeks(weeks: int = 3) -> list[dict]:
    """Пары на N недель вперёд, сгруппированные по дням."""
    from .db import db
    today = date.today()
    days = []
    seen_days: set[str] = set()
    for r in db.schedule_upcoming(today.isoformat()):
        seen_days.add(r["day"])
    # заполняем пустые дни недели тоже (для сетки)
    d = today
    while d < today + timedelta(days=weeks * 7):
        days.append(d.isoformat())
        d += timedelta(days=1)
    out = []
    for ds in days:
        rows, parity = db.schedule_day_parity(ds) if ds >= today.isoformat() else ([], "")
        dt = date.fromisoformat(ds)
        out.append({
            "date": ds,
            "weekday": WEEKDAYS[dt.weekday()],
            "is_today": ds == today.isoformat(),
            "is_past": ds < today.isoformat(),
            "items": [
                {
                    "time": f"{r.get('time_start', '')}–{r.get('time_end', '')}".strip("–"),
                    "title": _clean_ws(r["title"]),
                    "place": _clean_place(r.get("place", "")),
                    "kind": r.get("kind", "other"),
                } for r in rows
            ],
        })
    return out


# ------------------------------------------------------------------ ai api
async def api_ai_explain(request):
    """Пояснение выделенного текста конспекта."""
    try:
        body = await request.json()
        text = (body.get("text") or "").strip()
        cid = (body.get("conspect_id") or "").strip()
        mode = body.get("mode", "explain")
        if not text:
            return JSONResponse({"error": "нет текста"}, status_code=400)

        # контекст: ближайшие строки конспекта вокруг выделения
        context = ""
        if cid:
            md = CONSPECTS_DIR / cid / "conspect.md"
            if md.exists():
                full = md.read_text(encoding="utf-8")
                idx = full.find(text[:40])
                if idx >= 0:
                    context = full[max(0, idx - 600):idx + len(text) + 600]

        from .ai.client import ai
        if mode == "simple":
            instruction = ("Объясни этот фрагмент лекции максимально просто, как для "
                           "первокурсника, на конкретных бытовых примерах. 2-4 абзаца.")
        elif mode == "exam":
            instruction = ("Это выделенный фрагмент конспекта. Сформулируй по нему 3 "
                           "вероятных экзаменационных вопроса с краткими ответами "
                           "(формат: **Вопрос** — ответ).")
        elif mode == "flash":
            instruction = ("Сделай из этого фрагмента 3 флеш-карточки для запоминания. "
                           "Формат каждой: **Вопрос?** → ответ одной строкой.")
        else:
            instruction = ("Поясни выделенный фрагмент лекции: что это значит, зачем "
                           "нужно, где применяется. Кратко и по делу, 2-4 абзаца. "
                           "Markdown разрешён.")

        prompt = (f"Конспект: {conspect_course(cid)}\n\n"
                  f"=== Контекст вокруг фрагмента ===\n{context[:1500]}\n\n"
                  f"=== Выделенный фрагмент ===\n{text[:2000]}\n\n"
                  f"Задача: {instruction}")
        answer = await asyncio.wait_for(ai.chat([
            {"role": "system", "content":
                "Ты — терпеливый преподаватель. Отвечай по-русски, ясно, без воды."},
            {"role": "user", "content": prompt},
        ]), 240)
        return JSONResponse({"answer": answer})
    except asyncio.TimeoutError:
        return JSONResponse({"error": "нейронка задумалась надолго — попробуй ещё раз"},
                            status_code=504)
    except Exception as e:  # noqa: BLE001
        log.warning("ai/explain: %s", e)
        return JSONResponse({"error": f"ошибка: {e}"}, status_code=500)


def _conspect_context(cid: str) -> str | None:
    md = CONSPECTS_DIR / cid / "conspect.md"
    if not cid or not md.exists():
        return None
    try:
        text = md.read_text(encoding="utf-8")[:28000]
    except OSError:
        return None
    return ("Ты — помощник по конкретной лекции. Отвечай ТОЛЬКО по материалу "
            "конспекта ниже; если ответа там нет — честно скажи об этом. "
            "Отвечай по-русски, Markdown разрешён.\n\n"
            f"=== КОНСПЕКТ ===\n{text}")


def _assignment_context(cmid: int) -> str | None:
    from .db import db
    a = db.assignment(cmid)
    if not a:
        return None
    courses = {c["id"]: c["name"] for c in db.courses()}
    parts = [f"Задание: {_clean_ws(a['name'])}",
             f"Курс: {_clean_ws(courses.get(a.get('course_id', 0), ''))}"]
    dl = a.get("deadline_ts")
    if dl:
        parts.append("Срок: " + datetime.fromtimestamp(dl).strftime("%d.%m.%Y %H:%M"))
    if a.get("intro"):
        parts.append("Условие:\n" + str(a["intro"])[:6000])
    what = a.get("what_to_submit")
    if what:
        try:
            items = json.loads(what) if isinstance(what, str) else what
            if items:
                parts.append("Что сдать:\n" + "\n".join(f"- {w}" for w in items))
        except Exception:  # noqa: BLE001
            pass
    ans = ASSIGNMENTS_DIR / str(cmid) / "answer.md"
    if ans.exists():
        try:
            parts.append("Черновик ответа:\n" + ans.read_text(encoding="utf-8")[:8000])
        except OSError:
            pass
    return ("Ты — помощник по конкретному учебному заданию. Помогай понять условие, "
            "объясняй, что требуется сдать, проверяй черновик ответа. "
            "Отвечай по-русски, Markdown разрешён.\n\n"
            "=== ЗАДАНИЕ ===\n" + "\n\n".join(parts))


def _global_context() -> str:
    """Сводка по учёбе — контекст для вопросов вне конспекта.

    Тексты конспектов сюда НЕ попадают: только расписание, дедлайны и
    оглавления. Этого хватает на «что у меня на завтра», «что горит»,
    «по каким темам есть конспекты», и не раздувает промпт.
    """
    from .db import db
    conspects = get_conspects()
    assignments = get_assignments()
    today_iso = date.today().isoformat()
    lines = [f"Сегодня: {today_iso} ({WEEKDAYS[date.today().weekday()]})"]

    try:
        rows, parity = db.schedule_day_parity(today_iso)
        today_txt = "; ".join(
            f"{r.get('time_start', '')}–{r.get('time_end', '')} {_clean_ws(r['title'])}"
            for r in rows
        )
        lines.append("Пары сегодня: " + (today_txt or "нет"))
        lines.append("Неделя: " + ("чётная" if parity == "even" else "нечётная"))
    except Exception:  # noqa: BLE001
        pass

    try:
        days = schedule_weeks(2)
        upcoming = [
            f"{d['date']} ({d['weekday']}): " + "; ".join(
                f"{it['time']} {it['title']}" for it in d["items"])
            for d in days[1:8] if d.get("items")
        ]
        if upcoming:
            lines.append("Ближайшие дни:\n" + "\n".join(upcoming))
    except Exception:  # noqa: BLE001
        pass

    if assignments:
        lines.append("Задания:")
        for a in assignments[:25]:
            lines.append(f"- [{a['status']}] {a['name']} ({a['course'] or 'без курса'})"
                         f" — срок: {a['deadline'] or 'не указан'}")
    if conspects:
        lines.append("Конспекты в базе:")
        for c in conspects[:40]:
            secs = "; ".join(s["title"] for s in c["sections"][:6])
            lines.append(f"- {c['course'] or 'без курса'} · {c['title']}"
                         f" ({c['n_slides']} сл.)" + (f" — разделы: {secs}" if secs else ""))

    return ("Ты — учебный ассистент студента. Ниже сводка по его учёбе: расписание, "
            "дедлайны, библиотека конспектов. Отвечай по-русски, кратко и по делу, "
            "Markdown разрешён. Если данных в сводке нет — честно скажи об этом.\n\n"
            "=== СВОДКА ===\n" + "\n".join(lines))


def build_ai_messages(question: str, history: list, *, kind: str = "conspect",
                      cid: str = "", cmid: int | None = None) -> list[dict] | None:
    """Собирает сообщения для LLM. None — если контекст не найден."""
    if kind == "conspect":
        system = _conspect_context(cid)
    elif kind == "assignment":
        system = _assignment_context(cmid) if cmid else None
    else:
        system = _global_context()
    if system is None:
        return None

    msgs: list[dict] = [{"role": "system", "content": system}]
    for h in history[-6:]:
        role = "assistant" if h.get("role") == "assistant" else "user"
        content = (h.get("content") or "")[:1500]
        if content:
            msgs.append({"role": role, "content": content})
    msgs.append({"role": "user", "content": question[:2000]})
    return msgs


def _sse(payload: dict) -> bytes:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()


async def api_ai_ask(request):
    """Вопрос по конспекту / заданию / сводке (без стриминга — фолбэк)."""
    try:
        body = await request.json()
        question = (body.get("question") or "").strip()
        ctx = body.get("context") or {}
        kind = (ctx.get("kind") or "conspect").strip()
        cid = (ctx.get("id") or body.get("conspect_id") or "").strip()
        cmid = ctx.get("cmid") or body.get("cmid")
        history = body.get("history") or []
        if not question:
            return JSONResponse({"error": "нет вопроса"}, status_code=400)

        msgs = build_ai_messages(question, history, kind=kind, cid=cid,
                                 cmid=int(cmid) if cmid else None)
        if msgs is None:
            return JSONResponse({"error": "контекст не найден"}, status_code=404)

        from .ai.client import ai
        answer = await asyncio.wait_for(ai.chat(msgs), 240)
        return JSONResponse({"answer": answer})
    except asyncio.TimeoutError:
        return JSONResponse({"error": "Нейросеть не ответила за 4 минуты. Попробовать снова?"},
                            status_code=504)
    except Exception as e:  # noqa: BLE001
        log.warning("ai/ask: %s", e)
        return JSONResponse({"error": "Не удалось получить ответ. Попробовать снова?"},
                            status_code=500)


async def api_ai_ask_stream(request):
    """SSE-поток: ответ печатается по мере генерации, клиент может оборвать."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "плохой запрос"}, status_code=400)

    question = (body.get("question") or "").strip()
    ctx = body.get("context") or {}
    kind = (ctx.get("kind") or "conspect").strip()
    cid = (ctx.get("id") or body.get("conspect_id") or "").strip()
    cmid = ctx.get("cmid") or body.get("cmid")
    history = body.get("history") or []
    if not question:
        return JSONResponse({"error": "нет вопроса"}, status_code=400)

    msgs = build_ai_messages(question, history, kind=kind, cid=cid,
                             cmid=int(cmid) if cmid else None)
    if msgs is None:
        return JSONResponse({"error": "контекст не найден"}, status_code=404)

    from .ai.client import ai

    async def gen():
        yield _sse({"type": "start"})
        sent = 0
        try:
            async for ev in ai.chat_stream_events(msgs):
                if ev["type"] == "delta":
                    sent += 1
                yield _sse(ev)
            if not sent:
                # модель не отдала текст (бывает у reasoning-моделей при
                # обрыве) — добираем обычным запросом, чтобы не было пустоты
                answer = await asyncio.wait_for(ai.chat(msgs), 240)
                yield _sse({"type": "delta", "text": answer})
            yield _sse({"type": "done"})
        except asyncio.CancelledError:
            # клиент закрыл соединение (кнопка «стоп») — молча выходим
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("ai/ask/stream: %s", e)
            yield _sse({"type": "error",
                        "error": "Не удалось получить ответ. Попробовать снова?"})

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    })


async def api_ai_quiz(request):
    """Генерация квиза по конспекту (JSON с вопросами)."""
    try:
        body = await request.json()
        cid = (body.get("conspect_id") or "").strip()
        topic = (body.get("topic") or "").strip()
        if not cid:
            return JSONResponse({"error": "нет конспекта"}, status_code=400)
        md = CONSPECTS_DIR / cid / "conspect.md"
        if not md.exists():
            return JSONResponse({"error": "конспект не найден"}, status_code=404)
        text = md.read_text(encoding="utf-8")
        if topic:
            # ищем раздел по теме
            idx = text.lower().find(topic.lower())
            if idx >= 0:
                text = text[max(0, idx - 200):idx + 3000]

        from .ai.client import ai
        raw = await asyncio.wait_for(ai.chat([
            {"role": "system", "content": "Ты — составитель тестов. Отвечай только JSON."},
            {"role": "user", "content": (
                f"Конспект (фрагмент):\n{text[:15000]}\n\n"
                "Составь 5 вопросов с 4 вариантами ответа по ЭТОМУ материалу. "
                'Верни JSON: {"questions": [{"q": "...", "options": ["...", "...", "...", "..."], '
                '"correct": 0}, ...]}. correct — индекс правильного варианта (0-3). '
                "Вопросы на понимание, не на запоминание формулировок."
            )},
        ], json_mode=True), 240)
        import json as _json
        try:
            data = _json.loads(raw)
        except _json.JSONDecodeError:
            m = re.search(r"\{.*\}", raw, re.S)
            data = _json.loads(m.group(0)) if m else {"questions": []}
        return JSONResponse(data)
    except asyncio.TimeoutError:
        return JSONResponse({"error": "нейронка задумалась надолго — попробуй ещё раз"},
                            status_code=504)
    except Exception as e:  # noqa: BLE001
        log.warning("ai/quiz: %s", e)
        return JSONResponse({"error": f"ошибка: {e}"}, status_code=500)


# --------------------------------------------------------------- агент
def _agent_session_id(request, body: dict | None = None) -> str:
    """Ключ памяти агента: свой у каждого браузера."""
    if body and body.get("session"):
        return "web:" + str(body["session"])[:64]
    tok = request.cookies.get("hub_session") or ""
    if tok:
        return "web:" + tok[:40]
    return "web:anon"


async def api_agent_tools(request):
    """Список инструментов агента — для подсказок в панели."""
    from .agent import TOOL_SCHEMAS, TOOL_LABELS
    out = []
    for t in TOOL_SCHEMAS:
        fn = t["function"]
        meta = TOOL_LABELS.get(fn["name"], {})
        out.append({
            "name": fn["name"],
            "label": meta.get("label", fn["name"]),
            "group": meta.get("group", "misc"),
            "description": (fn.get("description") or "").split(".")[0][:120],
        })
    return JSONResponse({"tools": out})


async def api_agent_reset(request):
    """Забыть диалог с агентом (кнопка «новый чат»)."""
    body = {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        pass
    from .agent import agent
    agent.reset(_agent_session_id(request, body))
    return JSONResponse({"ok": True})


async def api_agent_file(request):
    """Скачать файл, который сделал агент (только из data/agent_out)."""
    name = unquote(request.path_params["name"])
    base = (cfg.data_dir / "agent_out").resolve()
    path = (base / name).resolve()
    if not str(path).startswith(str(base)) or not path.is_file():
        return Response(status_code=404)
    return FileResponse(path, filename=path.name,
                        media_type="application/octet-stream")


async def api_agent_stream(request):
    """Живой поток работы агента: размышления, вызовы инструментов, ответ.

    SSE, а не WebSocket: тот же эффект (события идут в браузер по мере
    появления), но проходит через nginx и не требует отдельного протокола.
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "плохой запрос"}, status_code=400)

    question = (body.get("question") or "").strip()
    if not question:
        return JSONResponse({"error": "нет вопроса"}, status_code=400)

    sid = _agent_session_id(request, body)
    ctx = body.get("context") or {}
    hint = ""
    if ctx.get("title"):
        kind = {"conspect": "лекция", "assignment": "задание"}.get(ctx.get("kind"), "")
        hint = f"{kind} «{ctx['title']}»".strip()

    from .agent import agent

    async def gen():
        yield _sse({"type": "start"})
        try:
            async for ev in agent.run_stream(sid, question, context_hint=hint):
                yield _sse(ev)
        except asyncio.CancelledError:
            # браузер закрыл поток (кнопка «стоп») — не шумим в логах
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("agent/stream: %s", e)
            yield _sse({"type": "error",
                        "error": "Агент не смог завершить работу. Попробовать снова?"})

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    })


async def api_ai_summary(request):
    """Краткое резюме конспекта (tldr)."""
    try:
        body = await request.json()
        cid = (body.get("conspect_id") or "").strip()
        md = CONSPECTS_DIR / cid / "conspect.md"
        if not md.exists():
            return JSONResponse({"error": "конспект не найден"}, status_code=404)
        text = md.read_text(encoding="utf-8")[:20000]
        from .ai.client import ai
        answer = await asyncio.wait_for(ai.chat([
            {"role": "system", "content": "Ты — редактор. Отвечай Markdown, по-русски."},
            {"role": "user", "content": (
                f"Сделай выжимку конспекта для повторения перед экзаменом:\n\n{text}\n\n"
                "Формат: 5-8 пунктов « главное, каждый — одна строка; "
                "затем блок «» с 3-5 датами/цифрами/терминами, которые точно спросят."
            )},
        ]), 240)
        return JSONResponse({"answer": answer})
    except asyncio.TimeoutError:
        return JSONResponse({"error": "таймаут"}, status_code=504)
    except Exception as e:  # noqa: BLE001
        log.warning("ai/summary: %s", e)
        return JSONResponse({"error": f"ошибка: {e}"}, status_code=500)


# ------------------------------------------------------------------ json api
async def api_conspects(request):
    return JSONResponse({"items": get_conspects()})


async def api_conspect(request):
    cid = request.path_params["cid"]
    md = CONSPECTS_DIR / cid / "conspect.md"
    if not md.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    text = md.read_text(encoding="utf-8")
    slides_dir = CONSPECTS_DIR / cid / "slides"
    n_slides = len(list(slides_dir.glob("*.png"))) if slides_dir.exists() else 0
    return JSONResponse({
        "id": cid,
        "title": md_first_title(md),
        "course": conspect_course(cid),
        "html": md_to_html(text),
        "n_slides": n_slides,
        "has_presentation": (CONSPECTS_DIR / cid / "presentation.html").exists(),
        "has_transcript": (CONSPECTS_DIR / cid / "transcript.txt").exists(),
        "size_kb": md.stat().st_size // 1024,
    })


async def api_slides(request):
    cid = request.path_params["cid"]
    slides_dir = CONSPECTS_DIR / cid / "slides"
    if not slides_dir.exists():
        return JSONResponse({"items": []}, status_code=404)
    pngs = sorted(slides_dir.glob("*.png"))
    return JSONResponse({"items": [
        {"n": i, "url": f"/media/conspects/{cid}/slides/{p.name}"}
        for i, p in enumerate(pngs, 1)
    ]})


async def api_assignments(request):
    return JSONResponse({"items": get_assignments()})


async def api_assignment(request):
    cmid = int(request.path_params["cmid"])
    from .db import db
    a = db.assignment(cmid)
    if not a:
        return JSONResponse({"error": "not found"}, status_code=404)
    courses = {c["id"]: c["name"] for c in db.courses()}

    # разбор из answer.md
    answer_html = ""
    answer_md = ASSIGNMENTS_DIR / str(cmid) / "answer.md"
    if answer_md.exists():
        answer_html = md_to_html(answer_md.read_text(encoding="utf-8"))

    what_html = ""
    what = a.get("what_to_submit")
    if what:
        try:
            items = json.loads(what) if isinstance(what, str) else what
            if items:
                what_html = md_to_html("\n".join(f"- {w}" for w in items))
        except Exception:  # noqa: BLE001
            pass

    intro_html = ""
    if a.get("intro"):
        intro_raw = a["intro"]
        # Moodle-описание — часто простыня без переносов: режем на предложения,
        # каждые 3-4 — абзац; метаданные файлов выносим отдельными строками
        import re as _re
        # метаданные вида «файл.pdf 16 сентября 2026, 08:10» — в отдельные строки
        # дефис с разрывом из Moodle («вы- деление» → «вы-деление»)
        intro_raw = _re.sub(r"(\w)-\s+(\w)", r"\1-\2", intro_raw)
        intro_raw = _re.sub(r"(\S+\.(?:pdf|docx?|xlsx?|png|jpe?g|zip)\s+\d{1,2}\s+[а-я]+\s+\d{4}(?:,?\s*\d{2}:\d{2})?)",
                            r"\n\n\1\n\n", intro_raw)
        # длинную простыню режем на предложения
        if "." in intro_raw and "\n" not in intro_raw.strip():
            sents = _re.split(r"(?<=[.!?])\s+", intro_raw.strip())
            # группируем по 3 предложения в абзац
            paras = []
            for i in range(0, len(sents), 3):
                paras.append(" ".join(sents[i:i + 3]))
            intro_raw = "\n\n".join(paras)
        intro_html = md_to_html(intro_raw)

    dl = a.get("deadline_ts")
    state = a.get("state", "draft")
    now = datetime.now().timestamp()
    left = (dl - now) if dl else 0
    return JSONResponse({
        "cmid": cmid,
        "name": a["name"],
        "course": _clean_ws(courses.get(a.get("course_id", 0), "")),
        "url": a.get("url") or f"{cfg.moodle_base}/mod/assign/view.php?id={cmid}",
        "deadline_ts": dl or 0,
        "deadline": datetime.fromtimestamp(dl).strftime("%d.%m.%Y %H:%M") if dl else "",
        "status": ("done" if state in ("submitted", "ready") else
                   "overdue" if left < 0 else
                   "urgent" if left < 86400 * 3 else "ok"),
        "state": state,
        "intro_html": intro_html,
        "what_html": what_html,
        "answer_html": answer_html,
    })


async def api_schedule(request):
    return JSONResponse({"days": schedule_weeks(4)})


async def api_stats(request):
    """Дашборд: всё в одном запросе."""
    conspects = get_conspects()
    assignments = get_assignments()
    today_iso = date.today().isoformat()
    from .db import db
    today_rows, parity = db.schedule_day_parity(today_iso)
    now = datetime.now().timestamp()
    upcoming = [a for a in assignments
                if a["status"] not in ("done", "overdue")][:6]
    # «скоро сдавать» = только будущие и срочные; просроченные — отдельный блок
    deadlines = [a for a in assignments if a["status"] == "urgent"][:5]
    overdue = [a for a in assignments if a["status"] == "overdue"][:4]
    return JSONResponse({
        "today": {
            "date": today_iso,
            "weekday": WEEKDAYS[date.today().weekday()],
            "parity": "чётная" if parity == "even" else "нечётная",
            "items": [
                {"time": f"{r.get('time_start', '')}–{r.get('time_end', '')}".strip("–"),
                 "title": _clean_ws(r["title"]), "place": _clean_place(r.get("place", "")),
                 "kind": r.get("kind")}
                for r in today_rows
            ],
        },
        "counts": {
            "conspects": len(conspects),
            "slides": sum(c["n_slides"] for c in conspects),
            "assignments_open": sum(1 for a in assignments if a["status"] not in ("done", "overdue")),
            "urgent": sum(1 for a in assignments if a["status"] == "urgent"),
        },
        "recent_conspects": conspects[:6],
        "upcoming": upcoming,
        "deadlines": deadlines,
        "overdue": overdue,
        "now": int(now),
    })


# ------------------------------------------------------------------ media
async def serve_media(request):
    """Статика media: /media/conspects/<slug>/slides/x.png и presentation.html."""
    rel = unquote(request.path_params["path"])
    file_path = (cfg.data_dir / "conspects" / rel).resolve()
    base = (cfg.data_dir / "conspects").resolve()
    if not str(file_path).startswith(str(base)) or not file_path.exists():
        return Response(status_code=404)
    suffix = file_path.suffix.lower()
    ctype = {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".html": "text/html; charset=utf-8", ".md": "text/markdown; charset=utf-8",
        ".txt": "text/plain; charset=utf-8", ".srt": "text/plain; charset=utf-8",
    }.get(suffix, "application/octet-stream")
    return FileResponse(file_path, media_type=ctype)


# ------------------------------------------------------------------ frontend
async def index(request):
    return FileResponse(STATIC_DIR / "index.html")


async def health(request):
    return JSONResponse({"ok": True})


def build_app() -> Starlette:
    routes = [
        Route("/login", login_page, methods=["GET"]),
        Route("/login", login_submit, methods=["POST"]),
        Route("/logout", logout, methods=["GET", "POST"]),
        Route("/", index),
        Route("/api/stats", api_stats),
        Route("/api/conspects", api_conspects),
        Route("/api/conspect/{cid}", api_conspect),
        Route("/api/slides/{cid}", api_slides),
        Route("/api/assignments", api_assignments),
        Route("/api/assignment/{cmid}", api_assignment),
        Route("/api/schedule", api_schedule),
        Route("/api/ai/explain", api_ai_explain, methods=["POST"]),
        Route("/api/ai/ask", api_ai_ask, methods=["POST"]),
        Route("/api/ai/ask/stream", api_ai_ask_stream, methods=["POST"]),
        Route("/api/ai/quiz", api_ai_quiz, methods=["POST"]),
        Route("/api/ai/summary", api_ai_summary, methods=["POST"]),
        Route("/api/agent/stream", api_agent_stream, methods=["POST"]),
        Route("/api/agent/reset", api_agent_reset, methods=["POST"]),
        Route("/api/agent/tools", api_agent_tools),
        Route("/api/agent/file/{name}", api_agent_file),
        Route("/media/conspects/{path:path}", serve_media),
        Mount("/static", app=StaticFiles(directory=str(STATIC_DIR)), name="static"),
    ]
    app = Starlette(routes=routes)
    app.add_middleware(AuthMiddleware)  # пароль на весь хаб
    return app


app = build_app()


def run(port: int | None = None) -> None:
    """Запуск веб-хаба (блокирующий). Для фонового старта — run_async."""
    import uvicorn
    uvicorn.run(build_app(), host="0.0.0.0", port=port or PORT, log_level="warning")


async def run_async(port: int | None = None) -> None:
    """Фоновый запуск внутри event-loop бота."""
    import uvicorn
    config = uvicorn.Config(build_app(), host="0.0.0.0", port=port or PORT,
                            log_level="warning", lifespan="off")
    server = uvicorn.Server(config)
    server.config.setup_event_loop()
    await server.serve()


if __name__ == "__main__":
    run()
