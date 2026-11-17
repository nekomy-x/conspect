"""Мост агент → MCP-серверы: инструменты MCP как обычные функции для LLM.

Агент (bot.agent) ходит в mcp_server_moodle и mcp_server_konspect не через
stdio-подпроцессы (тяжело на каждый запрос), а прямыми вызовами их
python-функций — те же тулы, что видит внешний MCP-клиент.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime

from .db import db

log = logging.getLogger("konspect.mcp_bridge")


def _ts(v) -> str:
    try:
        return datetime.fromtimestamp(int(v)).strftime("%d.%m.%Y %H:%M") if v else ""
    except Exception:  # noqa: BLE001
        return ""


# --- Moodle (обёртки над общими модулями — единая логика с MCP-сервером) ---

async def moodle_health() -> dict:
    from .moodle import moodle
    alive = await moodle.alive_simple()
    courses = db.courses()
    asg = db.assignments()
    return {"session_alive": alive, "user": moodle.user_name,
            "courses": len(courses),
            "watched": sum(1 for c in courses if db.is_watched(c["id"])),
            "assignments": len(asg),
            "unsubmitted": sum(1 for a in asg if a.get("state") != "submitted")}


async def moodle_courses() -> dict:
    return {"courses": [{"id": c["id"], "name": c["name"],
                         "watched": db.is_watched(c["id"])}
                        for c in db.courses()]}


async def moodle_activities(course_id: int) -> dict:
    rows = db.activities(course_id=course_id)
    return {"activities": [
        {"cmid": r["id"], "type": r["modname"], "name": r["name"],
         "url": r["url"], "section": r.get("section")} for r in rows]}


async def moodle_assignments(course_id: int | None = None,
                             only_unsubmitted: bool = False) -> dict:
    now = int(time.time())
    courses_ = {c["id"]: c["name"] for c in db.courses()}
    rows = db.assignments()
    if course_id:
        rows = [a for a in rows if a["course_id"] == course_id]
    if only_unsubmitted:
        rows = [a for a in rows if a.get("state") != "submitted"]
    out = []
    for a in rows:
        out.append({"cmid": a["activity_id"], "name": a["name"],
                    "course": courses_.get(a["course_id"], ""),
                    "deadline": _ts(a.get("deadline_ts")),
                    "overdue": bool(a.get("deadline_ts") and a["deadline_ts"] < now),
                    "old": bool(a.get("deadline_ts") and
                                a["deadline_ts"] < now - 365 * 86400),
                    "state": a.get("state"), "url": a["url"]})
    out.sort(key=lambda x: (bool(x["old"]), 0))
    return {"assignments": out[:60]}


async def moodle_read_page(url: str) -> dict:
    from . import moodle_actions
    return await moodle_actions.open_page(url)


async def moodle_meeting_status(cmid: int) -> dict:
    from .moodle import moodle
    info = await moodle.fetch_bbb(cmid)
    mi = info.get("meeting_info") or {}
    return {"name": info.get("title"),
            "running": mi.get("statusrunning"),
            "can_join": mi.get("canjoin"),
            "join_url": info.get("join_url"),
            "status_message": mi.get("statusmessage"),
            "recordings": [{"title": r["title"], "url": r["playback_url"]}
                           for r in info.get("recordings", [])]}


async def moodle_scan() -> dict:
    from .scanner import scan_all
    return await scan_all()


async def moodle_schedule(day: str = "today") -> dict:
    from datetime import timedelta
    if day == "today":
        key = datetime.now().strftime("%Y-%m-%d")
    elif day == "tomorrow":
        key = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    else:
        key = day
    rows = db.schedule_day(key)
    return {"day": key, "lessons": [
        {"time": r.get("time_start"), "end": r.get("time_end"),
         "title": r.get("title"), "place": r.get("place"),
         "kind": r.get("kind")} for r in rows]}


# --- Konspect ---

async def conspect_status() -> dict:
    from pathlib import Path
    from .config import cfg
    root = Path(cfg.data_dir) / "conspects"
    conspects = []
    if root.exists():
        for d in sorted(root.iterdir()):
            md = d / "conspect.md"
            if md.exists():
                # читаемое имя — заголовок конспекта (папки теперь по ID записи)
                try:
                    head = md.read_text(encoding="utf-8")[:400]
                    import re as _re
                    t = (_re.search(r"^#\s*(.+)$", head, _re.M) or
                         _re.search(r"\*\*(.+?)\*\*", head))
                    title = t.group(1).strip() if t else d.name
                except Exception:  # noqa: BLE001
                    title = d.name
                conspects.append({"id": d.name, "title": title[:80],
                                  "size_kb": md.stat().st_size // 1024,
                                  "presentation": (d / "presentation.html").exists()})
    return {"conspects": conspects,
            "unprocessed_recordings": [
                {"title": r["title"], "url": r["playback_url"]}
                for r in db.unprocessed_recordings()]}


async def conspect_read(name: str) -> dict:
    """name = id (из conspect_status) ИЛИ часть названия — найдём по обоим."""
    from pathlib import Path
    from .config import cfg
    root = Path(cfg.data_dir) / "conspects"
    md = root / name / "conspect.md"
    if not md.exists():
        # ищем по вхождению в заголовок
        for d in root.iterdir():
            f = d / "conspect.md"
            if not f.exists():
                continue
            head = f.read_text(encoding="utf-8")[:400]
            if name.lower() in head.lower() or name.lower() in d.name.lower():
                md = f
                break
    if not md.exists():
        return {"error": f"конспект не найден: {name}"}
    return {"text": md.read_text(encoding="utf-8")[:30000]}


async def conspect_read_presentation(name: str) -> dict:
    """Структура слайдов презентации конспекта (без html)."""
    import json
    from pathlib import Path
    from .config import cfg
    p = Path(cfg.data_dir) / "conspects" / name / "presentation.html"
    if not p.exists():
        return {"error": f"презентации нет: {name}"}
    import re
    html = p.read_text(encoding="utf-8")
    heads = re.findall(r"<h2>([^<]+)</h2>", html)
    return {"slides_headings": heads}
