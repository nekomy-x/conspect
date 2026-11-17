"""MCP-сервер «moodle»: универсальный доступ к edu.vsu.ru.

Инструменты MCP для любого клиента (Claude Desktop, агент, и т.п.):
курсы, активности, задания, чтение страниц, состояние BBB, сдача,
слежка, скан, расписание. Работает на cookie-сессии Moodle.

Запуск (stdio): .venv/bin/python -m konspekt.mcp_server_moodle
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta
from typing import Any

from mcp.server.mcpserver import MCPServer

from .config import cfg
from .db import db
from .moodle import moodle, MoodleAuthError

log = logging.getLogger("konspect.mcp.moodle")
mcp = MCPServer("konspekt-moodle",
                description="Moodle edu.vsu.ru: курсы, задания, лекции, "
                            "сдача, скан, расписание",
                version="0.1.0")


def _ts(v) -> str:
    try:
        return datetime.fromtimestamp(int(v)).strftime("%d.%m.%Y %H:%M") if v else ""
    except Exception:  # noqa: BLE001
        return ""


@mcp.tool()
async def health() -> dict:
    """Состояние Moodle: сессия жива, пользователь, статистика."""
    alive = await moodle.alive_simple()
    courses = db.courses()
    asg = db.assignments()
    return {"session_alive": alive, "user": moodle.user_name,
            "courses": len(courses),
            "watched": sum(1 for c in courses if db.is_watched(c["id"])),
            "assignments": len(asg),
            "unsubmitted": sum(1 for a in asg if a.get("state") != "submitted"),
            "unprocessed_recordings": len(db.unprocessed_recordings())}


@mcp.tool()
async def courses() -> dict:
    """Курсы студента: id, название, состояние слежки (watched)."""
    return {"courses": [{"id": c["id"], "name": c["name"],
                         "watched": db.is_watched(c["id"])}
                        for c in db.courses()]}


@mcp.tool()
async def activities(course_id: int) -> dict:
    """Активности курса: лекции BBB, задания, файлы, форумы (cmid, url)."""
    rows = db.activities(course_id=course_id)
    return {"activities": [
        {"cmid": r["id"], "type": r["modname"], "name": r["name"],
         "url": r["url"], "section": r.get("section")} for r in rows]}


@mcp.tool()
async def assignments(course_id: int | None = None,
                      only_unsubmitted: bool = False) -> dict:
    """Задания/лабы: имя, курс, дедлайн, состояние, url."""
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


@mcp.tool()
async def read_page(url: str) -> dict:
    """Прочитать страницу Moodle по URL — вернуть текст (описание задания/курса)."""
    from . import moodle_actions
    return await moodle_actions.open_page(url)


@mcp.tool()
async def course_page_content(course_id: int) -> dict:
    """Полное СОДЕРЖИМОЕ курса: все секции и активности со ссылками."""
    from .moodle import moodle
    acts = await moodle.fetch_activities(course_id)
    name = next((c["name"] for c in db.courses() if c["id"] == course_id), "")
    return {"course_id": course_id, "course_name": name,
            "activities": [{"cmid": a.cmid, "type": a.modname, "name": a.name,
                            "url": a.url, "section": a.section} for a in acts]}


@mcp.tool()
async def read_activity(cmid: int) -> dict:
    """Контент активности по cmid: задание (описание+вложения+сроки),
    лекция BBB (состояние+записи+презентация), форум/прочее (текст страницы)."""
    from .moodle import moodle
    act = db.activity(cmid)
    if not act:
        return {"error": f"активность {cmid} не найдена — сначала scan()"}
    mod = act["modname"]
    if mod == "assign":
        info = await moodle.fetch_assignment(cmid)
        return {"type": "assign", "name": info.name,
                "intro": info.intro, "deadline": info.deadline_ts,
                "submitted": info.submitted,
                "attachments": info.attachments,
                "full_text": info.text[:15000]}
    if mod == "bigbluebuttonbn":
        info = await moodle.fetch_bbb(cmid)
        mi = info.get("meeting_info") or {}
        return {"type": "bbb", "name": info.get("title"),
                "running": mi.get("statusrunning"),
                "can_join": mi.get("canjoin"),
                "join_url": info.get("join_url"),
                "presentation": info.get("presentation"),
                "recordings": info.get("recordings")}
    from . import moodle_actions
    return await moodle_actions.open_page(act["url"])


@mcp.tool()
async def grade_status(cmid: int) -> dict:
    """Оценка/статус сдачи задания + комментарии преподавателя (текст страницы)."""
    from . import moodle_actions
    return await moodle_actions.open_page(
        f"https://edu.vsu.ru/mod/assign/view.php?id={cmid}")


@mcp.tool()
async def my_grades(course_id: int) -> dict:
    """Журнал оценок студента по курсу."""
    from . import moodle_actions
    return await moodle_actions.open_page(
        f"https://edu.vsu.ru/grade/report/user/index.php?id={course_id}")


@mcp.tool()
async def dashboard() -> dict:
    """Личный кабинет /my/: дедлайны, новинки, форум-уведомления (текст)."""
    from . import moodle_actions
    return await moodle_actions.open_page("https://edu.vsu.ru/my/")


@mcp.tool()
async def meeting_status(cmid: int) -> dict:
    """Состояние лекции BBB: идёт, можно войти, join-url, записи."""
    info = await moodle.fetch_bbb(cmid)
    mi = info.get("meeting_info") or {}
    return {"name": info.get("title"),
            "running": mi.get("statusrunning"),
            "can_join": mi.get("canjoin"),
            "join_url": info.get("join_url"),
            "status_message": mi.get("statusmessage"),
            "opening": _ts(info.get("opening_ts")),
            "recordings": [{"title": r["title"], "url": r["playback_url"]}
                           for r in info.get("recordings", [])]}


@mcp.tool()
async def recordings(only_unprocessed: bool = False) -> dict:
    """Найденные записи лекций."""
    with db._conn() as c:  # noqa: SLF001
        q = "SELECT * FROM recordings"
        if only_unprocessed:
            q += " WHERE processed=0"
        q += " ORDER BY first_seen DESC LIMIT 50"
        rows = [dict(r) for r in c.execute(q)]
    return {"recordings": [{"title": r["title"], "url": r["playback_url"],
                            "processed": r["processed"],
                            "seen": _ts(r["first_seen"])} for r in rows]}


@mcp.tool()
async def watch_course(course_id: int, enabled: bool) -> dict:
    """Включить/выключить слежку за курсом."""
    db.watch(course_id, enabled=enabled)
    return {"ok": True, "course_id": course_id, "watched": enabled}


@mcp.tool()
async def scan() -> dict:
    """Полный скан Moodle: новые курсы/активности/записи/дедлайны."""
    from .scanner import scan_all
    return await scan_all()


@mcp.tool()
async def submit_file(cmid: int, file_path: str) -> dict:
    """ДЕЙСТВИЕ: загрузить файл в черновик сдачи задания (cmid)."""
    from . import moodle_actions
    return await moodle_actions.assignment_submit_file(cmid, file_path)


@mcp.tool()
async def check_submission(cmid: int) -> dict:
    """Проверить: сдано ли задание (cmid)."""
    from . import moodle_actions
    return await moodle_actions.check_submission_status(cmid)


@mcp.tool()
async def download(url: str, filename: str = "file") -> dict:
    """Скачать файл с Moodle (вложение задания и т.п.) в data/media."""
    from pathlib import Path
    dest = Path(cfg.data_dir) / "media" / filename
    await moodle.download(url, str(dest))
    return {"ok": True, "path": str(dest)}


@mcp.tool()
async def schedule(day: str = "today") -> dict:
    """Расписание: today / tomorrow / дата YYYY-MM-DD."""
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


async def main() -> None:
    await mcp.run_stdio_async()


if __name__ == "__main__":
    logging.basicConfig(level=cfg.log_level)
    asyncio.run(main())
