"""Агент-менеджер: свободный диалог + инструменты (Moodle/конспекты/действия).

Инструментный слой = mcp_bridge (те же функции, что экспортируют
MCP-серверы konspekt.mcp_server_moodle / mcp_server_konspect):
одна логика для Telegram-бота и для внешних MCP-клиентов.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from .config import cfg
from .db import db
from .ai.client import ai
from . import mcp_bridge as bridge
from . import moodle_actions
from . import tools as _tools

log = logging.getLogger("konspect.agent")

TOOL_SCHEMAS: list[dict] = [
    {"type": "function", "function": {
        "name": "moodle_health",
        "description": "Состояние Moodle: сессия, пользователь, статистика.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "moodle_courses",
        "description": "Курсы студента: id, название, слежка.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "moodle_activities",
        "description": "Активности курса (лекции BBB, задания, файлы, форумы).",
        "parameters": {"type": "object", "properties": {
            "course_id": {"type": "integer"},
        }, "required": ["course_id"]},
    }},
    {"type": "function", "function": {
        "name": "moodle_assignments",
        "description": "Задания/лабы: имя, КУРС, дедлайн, состояние, url. "
                       "Фильтры course_id / only_unsubmitted.",
        "parameters": {"type": "object", "properties": {
            "course_id": {"type": "integer"},
            "only_unsubmitted": {"type": "boolean"},
        }},
    }},
    {"type": "function", "function": {
        "name": "read_page",
        "description": "Прочитать страницу Moodle по URL — текст описания.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string"},
        }, "required": ["url"]},
    }},
    {"type": "function", "function": {
        "name": "meeting_status",
        "description": "Состояние лекции BBB (cmid): идёт? join-url? записи?",
        "parameters": {"type": "object", "properties": {
            "cmid": {"type": "integer"},
        }, "required": ["cmid"]},
    }},
    {"type": "function", "function": {
        "name": "scan_moodle",
        "description": "Полный скан Moodle: новое на курсах.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "get_schedule",
        "description": "Расписание: today/tomorrow/дата YYYY-MM-DD.",
        "parameters": {"type": "object", "properties": {
            "day": {"type": "string"},
        }},
    }},
    {"type": "function", "function": {
        "name": "watch_course",
        "description": "Включить/выключить слежку за курсом.",
        "parameters": {"type": "object", "properties": {
            "course_id": {"type": "integer"},
            "enabled": {"type": "boolean"},
        }, "required": ["course_id", "enabled"]},
    }},
    {"type": "function", "function": {
        "name": "add_reminder",
        "description": "Создать напоминание. when: '18:30' | 'завтра 9:00' | "
                       "'18:30 25.09' | unix. repeat (опц.): daily/weekly/weekdays — "
                       "повторять. Пользователь может скинуть текст с просьбой "
                       "'напомни завтра' — сам вытащи время и текст из сообщения.",
        "parameters": {"type": "object", "properties": {
            "when": {"type": "string", "description": "когда напомнить"},
            "text": {"type": "string", "description": "текст напоминания"},
            "repeat": {"type": "string", "enum": ["", "daily", "weekly", "weekdays"],
                       "description": "повторение"},
        }, "required": ["when", "text"]},
    }},
    {"type": "function", "function": {
        "name": "list_reminders",
        "description": "Активные напоминания пользователя (для 'покажи напоминания').",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "delete_reminder",
        "description": "Удалить напоминание по id (из list_reminders).",
        "parameters": {"type": "object", "properties": {
            "id": {"type": "integer"},
        }, "required": ["id"]},
    }},
    {"type": "function", "function": {
        "name": "conspect_status",
        "description": "Готовые конспекты + необработанные записи лекций.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "read_conspect",
        "description": "Прочитать готовый конспект (name из conspect_status).",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"},
        }, "required": ["name"]},
    }},
    {"type": "function", "function": {
        "name": "analyze_assignment",
        "description": "Разобрать задание (cmid): требования, ЧТО ПРИКРЕПИТЬ, шаги, камни.",
        "parameters": {"type": "object", "properties": {
            "cmid": {"type": "integer"},
        }, "required": ["cmid"]},
    }},
    {"type": "function", "function": {
        "name": "write_assignment_answer",
        "description": "Готовый ответ на задание (cmid). Если ответ УЖЕ сгенерирован — "
                       "мгновенно отдаёт готовый файл (не переделывает; повторный "
                       "запрос = снова кинуть тот же файл). force=true — переделать. "
                       "При замечаниях проверки — авто-исправляет и перепроверяет. "
                       "Файл прилетает пользователю в чат.",
        "parameters": {"type": "object", "properties": {
            "cmid": {"type": "integer"},
            "force": {"type": "boolean", "description": "переделать заново"},
        }, "required": ["cmid"]},
    }},
    {"type": "function", "function": {
        "name": "do_lab",
        "description": "ВЫПОЛНИТЬ лабораторную работу: создаёт HTML-файл по варианту "
                       "и PDF-отчёт (титульный лист, цель, ход работы, выводы). "
                       "Возвращает пути к файлам — отправь их пользователю через send_file.",
        "parameters": {"type": "object", "properties": {
            "cmid": {"type": "integer", "description": "id задания из Moodle"},
            "variant_num": {"type": "integer", "description": "номер варианта (по умолчанию 1)"},
        }, "required": ["cmid"]},
    }},
    {"type": "function", "function": {
        "name": "submit_assignment_file",
        "description": "ДЕЙСТВИЕ: загрузить файл в черновик сдачи задания. "
                       "Сначала покажи пользователю что будешь делать.",
        "parameters": {"type": "object", "properties": {
            "cmid": {"type": "integer"},
            "file_path": {"type": "string"},
        }, "required": ["cmid", "file_path"]},
    }},
    {"type": "function", "function": {
        "name": "check_submission",
        "description": "Проверить: сдано ли задание (cmid).",
        "parameters": {"type": "object", "properties": {
            "cmid": {"type": "integer"},
        }, "required": ["cmid"]},
    }},
    {"type": "function", "function": {
        "name": "process_recording",
        "description": "Конспект записи лекции BBB (playback url): транскрипт+"
                       "слайды+конспект+презентация. Идемпотентно.",
        "parameters": {"type": "object", "properties": {
            "playback_url": {"type": "string"},
            "course_name": {"type": "string"},
            "lecture_title": {"type": "string"},
            "force": {"type": "boolean"},
        }, "required": ["playback_url"]},
    }},
    {"type": "function", "function": {
        "name": "shell_exec",
        "description": "Выполнить shell-команду (полный доступ к системе). "
                       "Примеры: 'ls -la data/', 'cat .env', 'rm -rf data/old', "
                       "'systemctl restart konspect', 'ffmpeg ...', 'git status'. "
                       "Только whitelist-команды, запись в проект-директорию.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string"},
            "cwd": {"type": "string", "description": "рабочая директория"},
            "timeout": {"type": "integer"},
        }, "required": ["command"]},
    }},
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Прочитать файл (текст) с номерами строк.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "max_lines": {"type": "integer"},
        }, "required": ["path"]},
    }},
    {"type": "function", "function": {
        "name": "write_file",
        "description": "Записать/создать файл (только в проект и ~/Downloads).",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        }, "required": ["path", "content"]},
    }},
    {"type": "function", "function": {
        "name": "list_dir",
        "description": "Список файлов/папок. recursive=True — дерево.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "recursive": {"type": "boolean"},
        }},
    }},
    {"type": "function", "function": {
        "name": "search_files",
        "description": "Поиск по содержимому файлов (grep, regex).",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string"},
            "path": {"type": "string"},
            "file_glob": {"type": "string"},
        }, "required": ["pattern"]},
    }},
    {"type": "function", "function": {
        "name": "delete_path",
        "description": "Удалить файл/папку (rm -rf). Только разрешённые зоны.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
        }, "required": ["path"]},
    }},
    {"type": "function", "function": {
        "name": "move_path",
        "description": "Переместить/переименовать файл или папку.",
        "parameters": {"type": "object", "properties": {
            "src": {"type": "string"},
            "dst": {"type": "string"},
        }, "required": ["src", "dst"]},
    }},
    {"type": "function", "function": {
        "name": "system_info",
        "description": "Информация о системе: OS, CPU, RAM, диск, load.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "manage_model",
        "description": "Whisper-модели: download/list/delete.",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["download", "list", "delete"]},
            "model_name": {"type": "string"},
        }, "required": ["action"]},
    }},
    {"type": "function", "function": {
        "name": "send_photo",
        "description": "Отправить картинку пользователю в чат (слайд конспекта, "
                       "скриншот). path — локальный путь к png/jpg.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "caption": {"type": "string"},
        }, "required": ["path"]},
    }},
    {"type": "function", "function": {
        "name": "send_file",
        "description": "Отправить файл пользователю в чат (pdf, конспект, ответ).",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "caption": {"type": "string"},
        }, "required": ["path"]},
    }},
    {"type": "function", "function": {
        "name": "list_conspect_slides",
        "description": "Слайды готового конспекта (пути к png) — можно отправить "
                       "юзеру через send_photo.",
        "parameters": {"type": "object", "properties": {
            "conspect_id": {"type": "string", "description": "id из conspect_status"},
        }, "required": ["conspect_id"]},
    }},
    {"type": "function", "function": {
        "name": "web_search",
        "description": "Поиск в интернете (DuckDuckGo). Для любых вопросов вне "
                       "Moodle: определения, даты, факты, «что такое X».",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"},
            "max_results": {"type": "integer"},
        }, "required": ["query"]},
    }},
    {"type": "function", "function": {
        "name": "read_url",
        "description": "Прочитать ЛЮБУЮ страницу по URL (веб-статьи, вики, pdf) — текст.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string"},
        }, "required": ["url"]},
    }},
    {"type": "function", "function": {
        "name": "notes",
        "description": "Заметки менеджера: сохранить/показать/удалить. "
                       "action: list | add | delete. Пользователь говорит "
                       "«запиши...» — сохраняй заметку.",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["list", "add", "delete"]},
            "text": {"type": "string"},
            "tags": {"type": "string", "description": "через запятую"},
            "note_id": {"type": "integer"},
        }},
    }},
    {"type": "function", "function": {
        "name": "calculate",
        "description": "Калькулятор: арифметика, степени. Безопасный парсинг.",
        "parameters": {"type": "object", "properties": {
            "expression": {"type": "string", "description": "например 2**10 + 15*3"},
        }, "required": ["expression"]},
    }},
    {"type": "function", "function": {
        "name": "watch_url",
        "description": "Следить за веб-страницей: при изменении бот уведомит "
                       "(проверка каждые ~10 мин). Любые URL вне Moodle.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string"},
            "note": {"type": "string", "description": "зачем следим"},
        }, "required": ["url"]},
    }},
]


async def _t_watch_course(course_id: int, enabled: bool) -> dict:
    db.watch(course_id, enabled=enabled)
    return {"ok": True, "course_id": course_id, "watched": enabled}


async def _t_add_reminder(when: str, text: str,
                          repeat: str = "") -> dict:
    """Напоминание. when: ЧЧ:ММ | ЧЧ:ММ ДД.ММ | 'завтра ЧЧ:ММ' | unix.
    repeat: '' (один раз) | 'daily' | 'weekly' | 'weekdays'."""
    import re
    ts = None
    w = when.lower().strip()
    # завтра/послезавтра
    mday = re.match(r"(завтра|послезавтра|сегодня)?\s*(\d{1,2}):(\d{2})"
                    r"(?:\s+(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?)?", w)
    if when.isdigit() and len(when) > 9:
        ts = int(when)
    elif mday:
        hh, mm = int(mday.group(2)), int(mday.group(3))
        d = datetime.now()
        if mday.group(1) == "завтра":
            d = d.fromordinal(d.toordinal() + 1)
        elif mday.group(1) == "послезавтра":
            d = d.fromordinal(d.toordinal() + 2)
        if mday.group(4):
            try:
                d = d.replace(day=int(mday.group(4)), month=int(mday.group(5)))
                if mday.group(6):
                    y = int(mday.group(6))
                    d = d.replace(year=2000 + y if y < 100 else y)
            except ValueError:
                pass
        d = d.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if d <= datetime.now() and not mday.group(1) and not mday.group(4):
            d = d.fromordinal(d.toordinal() + 1)
        ts = int(d.timestamp())
    if not ts:
        return {"ok": False, "error": f"не понял время «{when}» (примеры: 18:30, "
                                      f"завтра 9:00, 18:30 25.09)"}
    rid = db.add_reminder(cfg.owner_chat_id, ts, text, kind="plain")
    # повторяющееся: кладём конфиг, планировщик пересоздаст после отправки
    if repeat in ("daily", "weekly", "weekdays"):
        db.kv_set(f"repeat:{rid}", {"repeat": repeat, "text": text,
                                    "base_ts": ts})
    due = datetime.fromtimestamp(ts).strftime("%d.%m %H:%M")
    return {"ok": True, "id": rid, "due": due,
            "repeat": repeat or None,
            "note": f"напомню {due}" + (f" (каждый{' день' if repeat == 'daily' else ''})"
                                        if repeat else "")}


# ------------------------------------------------- системные инструменты
from . import mcp_server_system as _sys

async def _t_shell_exec(command: str, cwd: str = "", timeout: int = 60) -> dict:
    return await _sys.shell_exec(command, cwd, timeout)

async def _t_read_file(path: str, max_lines: int = 500) -> dict:
    return await _sys.read_file(path, max_lines)

async def _t_write_file(path: str, content: str) -> dict:
    return await _sys.write_file(path, content)

async def _t_list_dir(path: str = ".", recursive: bool = False) -> dict:
    return await _sys.list_dir(path, recursive)

async def _t_search_files(pattern: str, path: str = ".",
                          file_glob: str = "*") -> dict:
    return await _sys.search_files(pattern, path, file_glob)

async def _t_delete_path(path: str) -> dict:
    return await _sys.delete_path(path)

async def _t_move_path(src: str, dst: str) -> dict:
    return await _sys.move_path(src, dst)

async def _t_system_info() -> dict:
    return await _sys.system_info()

async def _t_manage_model(action: str, model_name: str = "small") -> dict:
    return await _sys.manage_model(action, model_name)


async def _t_send_photo(path: str, caption: str = "") -> dict:
    """Внутренний тул: путь прокидывается колбэком в чат."""
    # сам путь валидируем, отправка через chat_send_photo из ask()
    from pathlib import Path
    p = Path(path)
    if not p.exists() or p.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
        return {"error": f"нет картинки: {path}"}
    return {"photo_path": str(p), "caption": caption[:900],
            "note": "фото уйдёт через chat-колбэк"}


async def _t_send_file(path: str, caption: str = "") -> dict:
    from pathlib import Path
    p = Path(path)
    if not p.exists():
        return {"error": f"нет файла: {path}"}
    return {"file_path": str(p), "caption": caption[:900],
            "note": "файл уйдёт через chat-колбэк (file_path уже это делает)"}


async def _t_list_slides(conspect_id: str) -> dict:
    from pathlib import Path
    from .config import cfg
    root = Path(cfg.data_dir) / "conspects" / conspect_id / "slides"
    if not root.exists():
        return {"error": "слайдов нет"}
    slides = sorted(root.glob("*.png"))
    return {"slides": [str(p) for p in slides[:50]],
            "hint": "отправляй через send_photo по одному или несколько"}


async def _t_list_reminders() -> dict:
    rows = db.reminders()
    out = []
    for r in rows[:20]:
        rep = db.kv_get(f"repeat:{r['id']}")
        out.append({"id": r["id"],
                    "due": datetime.fromtimestamp(r["due_ts"]).strftime("%d.%m %H:%M"),
                    "text": r["text"][:80],
                    "repeat": (rep or {}).get("repeat") if rep else None})
    return {"reminders": out}


async def _t_delete_reminder(id: int) -> dict:
    db.delete_reminder(int(id))
    return {"ok": True, "deleted": id}


async def _t_analyze_assignment(cmid: int) -> dict:
    from .moodle import moodle
    from .ai.assignments import analyze_assignment
    info = await moodle.fetch_assignment(cmid)
    analysis = await analyze_assignment(info)
    struct = analysis["analysis"].get("structure", {})
    pit = analysis["analysis"].get("pitfalls", {})
    courses = {c["id"]: c["name"] for c in db.courses()}
    return {"course": courses.get(info.course_id, ""),
            "name": info.name, "intro": info.intro[:2000],
            "deadline": (datetime.fromtimestamp(info.deadline_ts).strftime("%d.%m %H:%M")
                         if info.deadline_ts else None),
            "requirements": struct.get("requirements", [])[:12],
            "what_to_submit": struct.get("deliverables", [])[:12],
            "steps": struct.get("steps", [])[:12],
            "pitfalls": pit.get("pitfalls", [])[:6],
            "hint": "what_to_submit = ЧТО ИМЕННО ПРИКРЕПИТЬ. Скажи это пользователю."}


async def _t_do_lab(cmid: int, variant_num: int = 1) -> dict:
    from .lab import do_lab
    result = await do_lab(cmid, variant_num)
    if "error" in result:
        return result
    # отправляем файлы пользователю
    from .tg_notify import send_document
    await send_document(cfg.owner_chat_id, result["html"],
                        caption=f"HTML-файл (вариант {result['variant']})")
    await send_document(cfg.owner_chat_id, result["report"],
                        caption="PDF-отчёт (титульный лист, цель, ход работы, выводы)")
    return {**result, "sent": True,
            "note": "Файлы отправлены пользователю в чат"}


async def _t_write_assignment_answer(cmid: int, force: bool = False) -> dict:
    """Готовый ответ на задание. Кэшируется: если answer.md уже есть —
    сразу отдаёт файл, не генерит заново (force=True — переделать).
    Если проверка вернула замечания — один раз доправливает сама."""
    from .moodle import moodle
    from .ai.assignments import analyze_assignment, generate_answer, check_answer
    from pathlib import Path
    import json as _json

    path = Path(cfg.data_dir) / "assignments" / str(cmid) / "answer.md"
    asg = db.assignment(cmid)
    courses = {c["id"]: c["name"] for c in db.courses()}
    course = courses.get((asg or {}).get("course_id", 0), "")

    # 1. уже есть готовый ответ — просто отдать
    if path.exists() and not force:
        text = path.read_text(encoding="utf-8")
        verdict_note = ""
        if asg and asg.get("verdict"):
            verdict_note = f"\nВердикт прошлой проверки: {asg['verdict'][:300]}"
        return {
            "file_path": str(path),
            "cached": True,
            "file_send_note": "ФАЙЛ УЖЕ ОТПРАВЛЕН ПОЛЬЗОВАТЕЛЮ В ЧАТ (это готовый ответ)",
            "what_to_submit": _json.loads(asg["what_to_submit"]) if asg and asg.get("what_to_submit") else [],
            "deadline": (datetime.fromtimestamp(asg["deadline_ts"]).strftime("%d.%m %H:%M")
                         if asg and asg.get("deadline_ts") else None),
            "answer_preview": text[:1500] + ("\n… (полный текст в файле)" if len(text) > 1500 else ""),
            "hint": "Ответ уже был сгенерирован раньше — файл отправлен. "
                    "Если нужен новый вариант — попроси переделать (force).",
        }

    # 2. генерация
    info = await moodle.fetch_assignment(cmid)
    analysis = await analyze_assignment(info)
    answer = await generate_answer(info, analysis)
    verdict = await check_answer(info, answer, analysis)
    attempts = 1

    # 3. авто-доправка: если проверка нашла замечания — исправляем один раз
    if verdict != "ok" and verdict != "проверка не удалась, смотри вручную":
        try:
            fixed = await ai.chat([
                {"role": "system", "content":
                    "Ты исправляешь учебное задание по замечаниям проверяющего. "
                    "Верни ПОЛНЫЙ исправленный ответ целиком, без мета-комментариев."},
                {"role": "user", "content": (
                    f"Замечания проверяющего:\n{verdict}\n\n"
                    f"Текущий ответ:\n{answer[:25000]}\n\n"
                    f"Требования/что сдать:\n"
                    f"{_json.dumps(analysis.get('analysis', {}).get('structure', {}), ensure_ascii=False)[:4000]}\n\n"
                    "Исправь и верни полный ответ."
                )},
            ])
            if len(fixed) > len(answer) * 0.7:
                answer = fixed
                verdict2 = await check_answer(info, answer, analysis)
                verdict = f"{verdict}\n→ после авто-исправления: {verdict2}"
                attempts = 2
        except Exception as e:  # noqa: BLE001
            log.warning("auto-fix failed: %s", e)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# {info.name}\n\n{answer}", encoding="utf-8")
    struct = analysis.get("analysis", {}).get("structure", {})
    deliverables = struct.get("deliverables", [])[:6]
    db.update_assignment(cmid, state="ready", answer_path=str(path),
                         what_to_submit=str(deliverables).replace("'", '"'),
                         verdict=verdict[:1000])
    return {
        "file_path": str(path),
        "cached": False,
        "attempts": attempts,
        "file_send_note": "ФАЙЛ УЖЕ ОТПРАВЛЕН ПОЛЬЗОВАТЕЛЮ В ЧАТ",
        "verdict": verdict,
        "what_to_submit": deliverables,
        "deadline": (datetime.fromtimestamp(info.deadline_ts).strftime("%d.%m %H:%M")
                     if info.deadline_ts else None),
        "answer_preview": answer[:2000],
    }


async def _t_submit_assignment_file(cmid: int, file_path: str) -> dict:
    return await moodle_actions.assignment_submit_file(cmid, file_path)


async def _t_check_submission(cmid: int) -> dict:
    return await moodle_actions.check_submission_status(cmid)


async def _t_process_recording(playback_url: str, course_name: str = "",
                               lecture_title: str = "", force: bool = False) -> dict:
    from .ai.pipeline import process_recording
    result = await process_recording(playback_url, course_name=course_name,
                                     lecture_title=lecture_title, force=force)
    result.pop("recording", None)
    return result


HANDLERS: dict[str, Callable[..., Awaitable[dict]]] = {
    "moodle_health": bridge.moodle_health,
    "moodle_courses": bridge.moodle_courses,
    "moodle_activities": bridge.moodle_activities,
    "moodle_assignments": bridge.moodle_assignments,
    "read_page": bridge.moodle_read_page,
    "meeting_status": bridge.moodle_meeting_status,
    "scan_moodle": bridge.moodle_scan,
    "get_schedule": bridge.moodle_schedule,
    "watch_course": _t_watch_course,
    "add_reminder": _t_add_reminder,
    "list_reminders": _t_list_reminders,
    "delete_reminder": _t_delete_reminder,
    "conspect_status": bridge.conspect_status,
    "read_conspect": bridge.conspect_read,
    "analyze_assignment": _t_analyze_assignment,
    "write_assignment_answer": _t_write_assignment_answer,
    "do_lab": _t_do_lab,
    "submit_assignment_file": _t_submit_assignment_file,
    "check_submission": _t_check_submission,
    "process_recording": _t_process_recording,
    "web_search": _tools.web_search,
    "shell_exec": _t_shell_exec,
    "read_file": _t_read_file,
    "write_file": _t_write_file,
    "list_dir": _t_list_dir,
    "search_files": _t_search_files,
    "delete_path": _t_delete_path,
    "move_path": _t_move_path,
    "system_info": _t_system_info,
    "manage_model": _t_manage_model,
    "send_photo": _t_send_photo,
    "send_file": _t_send_file,
    "list_conspect_slides": _t_list_slides,
    "read_url": _tools.read_url,
    "notes": _tools.notes,
    "calculate": _tools.calculate,
    "watch_url": _tools.watch_url,
}

CONFIRM_REQUIRED = {"submit_assignment_file"}
MAX_TURNS = 8

# Предохранители: без них один прогон в веб-панели может висеть очень долго
# (агент уходит сканировать весь Moodle). Пользователь всегда может нажать
# «стоп», но и сервер не должен работать бесконечно.
AGENT_MAX_SECONDS = 600      # общий потолок на один прогон
AGENT_CALL_TIMEOUT = 180     # потолок на один запрос к модели

# Человеческие подписи для веб-панели: что агент делает прямо сейчас.
# group — для иконки и цвета карточки действия.
TOOL_LABELS: dict[str, dict] = {
    "moodle_health": {"label": "Проверяю Moodle", "group": "moodle"},
    "moodle_courses": {"label": "Смотрю курсы", "group": "moodle"},
    "moodle_activities": {"label": "Смотрю активности курса", "group": "moodle"},
    "moodle_assignments": {"label": "Собираю задания", "group": "moodle"},
    "read_page": {"label": "Читаю страницу Moodle", "group": "moodle"},
    "meeting_status": {"label": "Проверяю лекцию", "group": "moodle"},
    "scan_moodle": {"label": "Сканирую Moodle", "group": "moodle"},
    "get_schedule": {"label": "Смотрю расписание", "group": "moodle"},
    "watch_course": {"label": "Меняю слежку за курсом", "group": "moodle"},
    "add_reminder": {"label": "Ставлю напоминание", "group": "remind"},
    "list_reminders": {"label": "Смотрю напоминания", "group": "remind"},
    "delete_reminder": {"label": "Удаляю напоминание", "group": "remind"},
    "conspect_status": {"label": "Смотрю конспекты", "group": "conspect"},
    "read_conspect": {"label": "Читаю конспект", "group": "conspect"},
    "list_conspect_slides": {"label": "Смотрю слайды", "group": "conspect"},
    "analyze_assignment": {"label": "Разбираю задание", "group": "study"},
    "write_assignment_answer": {"label": "Пишу ответ на задание", "group": "study"},
    "do_lab": {"label": "Делаю лабораторную", "group": "study"},
    "submit_assignment_file": {"label": "Отправляю файл в Moodle", "group": "study"},
    "check_submission": {"label": "Проверяю сдачу", "group": "study"},
    "process_recording": {"label": "Обрабатываю запись лекции", "group": "study"},
    "web_search": {"label": "Ищу в интернете", "group": "web"},
    "read_url": {"label": "Читаю ссылку", "group": "web"},
    "watch_url": {"label": "Слежу за страницей", "group": "web"},
    "shell_exec": {"label": "Выполняю команду", "group": "system"},
    "read_file": {"label": "Читаю файл", "group": "system"},
    "write_file": {"label": "Пишу файл", "group": "system"},
    "list_dir": {"label": "Смотрю папку", "group": "system"},
    "search_files": {"label": "Ищу файлы", "group": "system"},
    "delete_path": {"label": "Удаляю", "group": "system"},
    "move_path": {"label": "Перемещаю", "group": "system"},
    "system_info": {"label": "Смотрю систему", "group": "system"},
    "manage_model": {"label": "Управляю моделями", "group": "system"},
    "send_photo": {"label": "Готовлю фото", "group": "system"},
    "send_file": {"label": "Готовлю файл", "group": "system"},
    "notes": {"label": "Работаю с заметками", "group": "misc"},
    "calculate": {"label": "Считаю", "group": "misc"},
}

# Аргументы, которые не показываем целиком (слишком длинные / неинтересные)
ARG_HIDE = {"content", "max_lines", "recursive"}


def _args_summary(args: dict) -> str:
    """Короткая строка аргументов для карточки действия."""
    parts = []
    for k, v in (args or {}).items():
        if k in ARG_HIDE:
            continue
        s = str(v)
        if len(s) > 60:
            s = s[:57] + "…"
        s = s.replace("\n", " ")
        parts.append(s if k in ("query", "url", "path", "command", "name", "text")
                     else f"{k}={s}")
        if len(parts) >= 3:
            break
    return " · ".join(parts)[:160]


def _result_preview(res: Any) -> str:
    """Короткая выжимка результата инструмента для карточки."""
    if not isinstance(res, dict):
        return str(res)[:200]
    if "error" in res:
        return f"ошибка: {str(res['error'])[:180]}"
    for key in ("answer", "summary", "text", "result", "content", "message", "output"):
        if key in res and res[key]:
            return str(res[key])[:300].replace("\n", " ")
    # словари/списки — показываем количество
    bits = []
    for k, v in res.items():
        if isinstance(v, list):
            bits.append(f"{k}: {len(v)}")
        elif isinstance(v, (str, int, float, bool)):
            s = str(v)
            if len(s) <= 40:
                bits.append(f"{k}: {s}")
        if len(bits) >= 4:
            break
    return " · ".join(bits)[:240] or "готово"


# Инструменты, которые СОЗДАЮТ файл, и под каким ключом он лежит.
# read_file/list_dir тоже отдают «path», но это не артефакт — его нельзя
# выдавать в панель как «агент сделал файл».
ARTIFACT_TOOLS: dict[str, tuple[str, str]] = {
    "write_file": ("path", "file"),
    "send_file": ("file_path", "file"),
    "send_photo": ("photo_path", "photo"),
    "do_lab": ("file_path", "file"),
    "write_assignment_answer": ("file_path", "file"),
}


def _artifacts_from(tool_name: str, res: Any) -> list[tuple[str, str]]:
    """Что из результата инструмента стоит показать как готовый файл."""
    if not isinstance(res, dict) or "error" in res:
        return []
    known = ARTIFACT_TOOLS.get(tool_name)
    if known:
        val = res.get(known[0])
        return [(str(val), known[1])] if val else []
    out = []
    for key, kind in (("file_path", "file"), ("photo_path", "photo")):
        if res.get(key):
            out.append((str(res[key]), kind))
    return out


def _publish_artifact(src: str) -> dict | None:
    """Файл, созданный агентом, → в общую папку, откуда его скачает браузер.

    Веб-панель не может отдать произвольный путь с диска, поэтому копируем
    в data/agent_out и возвращаем публичное имя + url.
    """
    try:
        p = Path(src).expanduser()
        if not p.is_file():
            return None
        out_dir = cfg.data_dir / "agent_out"
        out_dir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^\w.\-]+", "_", p.name, flags=re.UNICODE)[:80] or "file"
        stored = f"{int(time.time())}_{safe}"
        shutil.copy2(p, out_dir / stored)
        return {"name": p.name, "url": f"/api/agent/file/{stored}"}
    except Exception as e:  # noqa: BLE001
        log.warning("artifact %s: %s", src, e)
        return None


class AgentSession:
    """Диалог с памятью на чат."""

    def __init__(self) -> None:
        self.histories: dict[int, list[dict]] = {}

    def _trim(self, chat_id: int) -> None:
        hist = self.histories.get(chat_id, [])
        if not hist:
            return
        hist[0] = self._system_msg()
        self.histories[chat_id] = [hist[0]] + hist[-30:]

    async def ask(self, chat_id: int, user_text: str,
                  confirm_cb=None, chat_send_file=None,
                  chat_send_photo=None) -> tuple[str, str | None]:
        """Спросить агента. Возвращает (ответ, file_path).

        chat_send_file / chat_send_photo — отправка в чат во время работы."""
        self.histories.setdefault(chat_id, [self._system_msg()])
        self.histories[chat_id][0] = self._system_msg()
        hist = self.histories[chat_id]
        hist.append({"role": "user", "content": user_text})

        for _ in range(MAX_TURNS):
            try:
                resp = await ai.client.chat.completions.create(
                    model=cfg.openai_model, messages=hist,
                    tools=TOOL_SCHEMAS, tool_choice="auto",
                )
            except Exception as e:  # noqa: BLE001
                # фолбэк: без tools (некоторые модели/зеркала)
                try:
                    resp = await ai.client.chat.completions.create(
                        model=cfg.openai_model, messages=hist,
                    )
                except Exception as e2:  # noqa: BLE001
                    return f"ИИ недоступен: {e2}", None
                answer = resp.choices[0].message.content or ""
                hist.append({"role": "assistant", "content": answer})
                self._trim(chat_id)
                return answer, None

            msg = resp.choices[0].message
            tool_calls = getattr(msg, "tool_calls", None)

            if not tool_calls:
                answer = msg.content or ""
                hist.append({"role": "assistant", "content": answer})
                self._trim(chat_id)
                return answer, None

            hist.append({
                "role": "assistant", "content": msg.content or "",
                "tool_calls": [{
                    "id": tc.id, "type": "function",
                    "function": {"name": tc.function.name,
                                 "arguments": tc.function.arguments},
                } for tc in tool_calls],
            })
            # ПАРАЛЛЕЛЬНОЕ выполнение независимых тулов (кроме требующих подтверждения)
            plain_calls = []
            confirm_calls = []
            for tc in tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                if tc.function.name in CONFIRM_REQUIRED and confirm_cb is not None:
                    confirm_calls.append((tc, args))
                else:
                    plain_calls.append((tc, args))

            # независимые — вместе
            if plain_calls:
                results = await asyncio.gather(
                    *[self._run_tool(tc.function.name, args)
                      for tc, args in plain_calls],
                    return_exceptions=True)
                for (tc, args), res in zip(plain_calls, results):
                    if isinstance(res, Exception):
                        tool_result = {"error": str(res)[:300]}
                    else:
                        tool_result = res
                    log.info("tool: %s(%s) → %s", tc.function.name,
                             str(args)[:80],
                             "ok" if not isinstance(res, dict) or "error" not in res
                             else "ERR")
                    # файл-артефакт — сразу в чат
                    fp = tool_result.get("file_path") if isinstance(tool_result, dict) else None
                    if fp and chat_send_file:
                        try:
                            await chat_send_file(fp)
                        except Exception as e:  # noqa: BLE001
                            log.warning("file send: %s", e)
                    # фото-артефакт — тоже
                    pp = tool_result.get("photo_path") if isinstance(tool_result, dict) else None
                    if pp and chat_send_photo:
                        try:
                            await chat_send_photo(pp, tool_result.get("caption", ""))
                        except Exception as e:  # noqa: BLE001
                            log.warning("photo send: %s", e)
                    hist.append({
                        "role": "tool", "tool_call_id": tc.id,
                        "content": json.dumps(tool_result, ensure_ascii=False)[:12000],
                    })

            # требующие подтверждения — по очереди
            for tc, args in confirm_calls:
                name = tc.function.name
                log.info("tool(confirm): %s(%s)", name, args)
                ok = await confirm_cb(
                    f"⚠️ Действие на Moodle: загрузить файл в задание "
                    f"№{args.get('cmid')}? Файл: {args.get('file_path')}")
                if not ok:
                    tool_result = {"skipped": "пользователь отменил"}
                else:
                    tool_result = await self._run_tool(name, args)
                fp = tool_result.get("file_path") if isinstance(tool_result, dict) else None
                if fp and chat_send_file:
                    try:
                        await chat_send_file(fp)
                    except Exception as e:  # noqa: BLE001
                        log.warning("file: %s", e)
                hist.append({
                    "role": "tool", "tool_call_id": tc.id,
                    "content": json.dumps(tool_result, ensure_ascii=False)[:12000],
                })

        final = await ai.chat(hist + [{"role": "user", "content":
                                       "Подведи итог коротко."}])
        hist.append({"role": "assistant", "content": final})
        self._trim(chat_id)
        return final, None

    async def _run_tool(self, name: str, args: dict) -> dict:
        handler = HANDLERS.get(name)
        if not handler:
            return {"error": f"неизвестный инструмент {name}"}
        try:
            return await handler(**args)
        except Exception as e:  # noqa: BLE001
            log.exception("tool %s failed", name)
            return {"error": str(e)[:500]}

    # ---------------------------------------------------------------- поток
    async def run_stream(self, session_id: str, user_text: str,
                         *, allow_confirm: bool = False,
                         context_hint: str = ""):
        """Агентный цикл как поток событий — для живой веб-панели.

        Отдаёт словари:
          start · turn · reasoning · tool_start · tool_end · delta ·
          artifact · done · error

        Отличие от ask(): видно, ЧТО агент делает и О ЧЁМ думает, прямо
        во время работы, а не после неё.
        """
        t0 = time.time()
        self.histories.setdefault(session_id, [self._system_msg()])
        self.histories[session_id][0] = self._system_msg()
        hist = self.histories[session_id]

        prompt = user_text
        if context_hint:
            prompt = f"{user_text}\n\n[Контекст, открытый у пользователя: {context_hint}]"
        hist.append({"role": "user", "content": prompt})

        yield {"type": "start", "model": cfg.openai_model}

        answer = ""
        for turn in range(1, MAX_TURNS + 1):
            # вышли за общий потолок времени — не бросаем работу молча,
            # а подводим итог по тому, что уже успели
            if time.time() - t0 > AGENT_MAX_SECONDS:
                yield {"type": "notice", "text":
                       "Задача идёт долго — остановился и подвожу итог по тому, "
                       "что уже удалось выяснить."}
                break

            yield {"type": "turn", "n": turn}

            content_acc = ""
            reasoning_acc = ""
            calls: dict[int, dict] = {}
            used_tools = False

            try:
                stream = await ai.client.chat.completions.create(
                    model=cfg.openai_model, messages=hist,
                    tools=TOOL_SCHEMAS, tool_choice="auto", stream=True,
                    timeout=AGENT_CALL_TIMEOUT,
                )
            except Exception as e:  # noqa: BLE001
                # зеркало/модель без tools — отвечаем простым чатом
                log.warning("agent stream без tools: %s", e)
                try:
                    async for piece in ai.chat_stream(hist):
                        answer += piece
                        yield {"type": "delta", "text": piece}
                except Exception as e2:  # noqa: BLE001
                    yield {"type": "error", "error": f"ИИ недоступен: {e2}"}
                    return
                hist.append({"role": "assistant", "content": answer})
                self._trim(session_id)
                yield {"type": "done", "answer": answer, "turns": turn,
                       "ms": int((time.time() - t0) * 1000)}
                return

            try:
                async for chunk in stream:
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    piece = getattr(delta, "reasoning_content", None)
                    if not piece:
                        extra = getattr(delta, "model_extra", None) or {}
                        piece = extra.get("reasoning_content") or extra.get("reasoning")
                    if piece:
                        reasoning_acc += piece
                        yield {"type": "reasoning", "text": piece}

                    text = getattr(delta, "content", None)
                    if text:
                        content_acc += text
                        # печатаем сразу в поле ответа: если окажется, что это
                        # была преамбула перед инструментами — перенесём выше
                        # событием demote (см. ниже)
                        yield {"type": "delta", "text": text}

                    for tc in (getattr(delta, "tool_calls", None) or []):
                        used_tools = True
                        idx = getattr(tc, "index", 0) or 0
                        slot = calls.setdefault(idx, {"id": "", "name": "", "args": ""})
                        if getattr(tc, "id", None):
                            slot["id"] = tc.id
                        fn = getattr(tc, "function", None)
                        if fn is not None:
                            if getattr(fn, "name", None):
                                slot["name"] = fn.name
                            if getattr(fn, "arguments", None):
                                slot["args"] += fn.arguments
            except asyncio.CancelledError:
                # клиент нажал «стоп» — сохраняем то, что успели
                if content_acc.strip():
                    hist.append({"role": "assistant", "content": content_acc})
                    self._trim(session_id)
                raise

            if reasoning_acc:
                yield {"type": "reasoning_done", "text": reasoning_acc}

            # ---------- нет инструментов → это финальный ответ ----------
            if not used_tools or not calls:
                answer = content_acc.strip()
                if not answer:
                    yield {"type": "error",
                           "error": "Модель вернула пустой ответ. Попробуй ещё раз."}
                    return
                hist.append({"role": "assistant", "content": answer})
                self._trim(session_id)
                yield {"type": "done", "answer": answer, "turns": turn,
                       "ms": int((time.time() - t0) * 1000)}
                return

            # ---------- инструменты ----------
            # то, что уже напечаталось, было преамбулой — уводим в «вслух»
            if content_acc.strip():
                yield {"type": "demote"}

            parsed: list[tuple[dict, dict]] = []
            for slot in calls.values():
                if not slot["name"]:
                    continue
                try:
                    args = json.loads(slot["args"] or "{}")
                except json.JSONDecodeError:
                    args = {}
                parsed.append((slot, args))

            hist.append({
                "role": "assistant", "content": content_acc or "",
                "tool_calls": [{
                    "id": s["id"] or f"call_{i}", "type": "function",
                    "function": {"name": s["name"], "arguments": s["args"] or "{}"},
                } for i, (s, _) in enumerate(parsed)],
            })

            meta = TOOL_LABELS.get
            for slot, args in parsed:
                yield {
                    "type": "tool_start",
                    "id": slot["id"] or slot["name"],
                    "name": slot["name"],
                    "label": (meta(slot["name"]) or {}).get("label", slot["name"]),
                    "group": (meta(slot["name"]) or {}).get("group", "misc"),
                    "args": args,
                    "args_summary": _args_summary(args),
                }

            async def _timed(slot, args):
                t = time.time()
                if slot["name"] in CONFIRM_REQUIRED and not allow_confirm:
                    # опасные действия (сдача в Moodle) из веба не запускаем:
                    # подтверждения в браузере нет, а испортить сдачу нельзя
                    return ({"error": "Это действие требует подтверждения — "
                                      "попроси бота в Telegram."}, 0)
                res = await self._run_tool(slot["name"], args)
                return res, int((time.time() - t) * 1000)

            results = await asyncio.gather(
                *[_timed(s, a) for s, a in parsed], return_exceptions=True)

            for (slot, args), item in zip(parsed, results):
                if isinstance(item, Exception):
                    res, ms = {"error": str(item)[:300]}, 0
                else:
                    res, ms = item
                ok = not (isinstance(res, dict) and "error" in res)
                yield {
                    "type": "tool_end",
                    "id": slot["id"] or slot["name"],
                    "name": slot["name"],
                    "ok": ok,
                    "ms": ms,
                    "preview": _result_preview(res),
                }
                # файлы-артефакты → в панель, а не в Telegram
                for src, kind in _artifacts_from(slot["name"], res):
                    art = _publish_artifact(src)
                    if art:
                        yield {"type": "artifact", "kind": kind,
                               "name": art["name"], "url": art["url"],
                               "caption": res.get("caption", "")}
                hist.append({
                    "role": "tool", "tool_call_id": slot["id"] or slot["name"],
                    "content": json.dumps(res, ensure_ascii=False)[:12000],
                })

        # закончились ходы или время — подводим итог тем, что есть
        yield {"type": "turn", "n": MAX_TURNS + 1}
        answer = ""
        try:
            async for piece in ai.chat_stream(hist + [
                    {"role": "user", "content":
                     "Подведи итог коротко по тому, что уже выяснено."}]):
                answer += piece
                yield {"type": "delta", "text": piece}
        except Exception as e:  # noqa: BLE001
            yield {"type": "error", "error": f"Не удалось подвести итог: {e}"}
            return
        hist.append({"role": "assistant", "content": answer})
        self._trim(session_id)
        yield {"type": "done", "answer": answer, "turns": MAX_TURNS,
               "ms": int((time.time() - t0) * 1000)}

    def reset(self, session_id: str) -> None:
        """Забыть диалог (кнопка «новый чат»)."""
        self.histories.pop(session_id, None)

    def _system_msg(self) -> dict:
        now = datetime.now()
        courses = db.courses()
        course_list = "\n".join(f"- [{c['id']}] {c['name']}"
                                + ("" if db.is_watched(c["id"]) else " (слежка ВЫКЛ)")
                                for c in courses[:25])
        today = db.schedule_day(now.strftime("%Y-%m-%d"))
        sched = "\n".join(f"- {r.get('time_start')} {r.get('title')} "
                          f"{r.get('place') or ''}" for r in today[:8])
        return {"role": "system", "content": f"""Ты — персональный менеджер-ассистент студента ВГУ ({_moodle_user()}).
Сегодня: {now.strftime('%Y-%m-%d %H:%M (%A)')}, таймзона Москва.

КУРСЫ СТУДЕНТА (id для инструментов):
{course_list or '(не отсканированы — вызови scan_moodle)'}

РАСПИСАНИЕ НА СЕГОДНЯ:
{sched or '(не задано — попроси скинуть xlsx/фото/текстом)'}

ТЫ УМЕЕШЬ ВСЁ:
- Moodle: курсы, задания, дедлайны, чтение страниц, лекции, сдача (после согласия)
- конспекты лекций: авто из записей, готовые показать
- напоминания: разовые (завтра 9:00) и повторяющиеся (каждый день)
- ВЕБ-ПОИСК: любые вопросы не по учёбе — гугли (web_search, потом read_url)
- чтение любых ссылок (статьи, вики, pdf)
- заметки: «запиши...» → notes add
- калькулятор, слежка за любыми веб-страницами
- расписание, план на день
- ПОЛНЫЙ ДОСТУП К СИСТЕМЕ (как Claude Code / OpenClaw):
  • shell_exec — выполняй shell-команды (ls, cat, rm, cp, mv, git, ffmpeg,
    systemctl, curl, python3 и т.д.)
  • read_file / write_file — читай и пиши файлы
  • list_dir / search_files — листай папки, ищи по содержимому
  • delete_path / move_path — удаляй и перемещай файлы/папки
  • system_info — инфо о системе
  • manage_model — управление Whisper-моделями
  Пользователь — программист. Если он просит «убери это», «поменяй то»,
  «создай файл», «запусти команду» — ДЕЛАЙ через shell_exec/write_file,
  не объясняй как сделать вручную. Действуй.
  ⚠️ СЕКРЕТЫ ЗАПРЕЩЕНЫ: .env, пароли, токены, ключи — читать/выводить/
  копировать НЕЛЬЗЯ. Если просят — откажи: «доступ к секретам закрыт».

ПРАВИЛА ДИАЛОГА:
- помни весь разговор; короткие реплики понимай в контексте;
- в списках заданий ВСЕГДА указывай курс и дедлайн;
- ЕСЛИ НЕОДНОЗНАЧНО (несколько «лаба 2» в разных курсах, «та лекция») —
  спроси уточнение одной строкой или покажи варианты с курсами;
  НЕ выбирай молча за пользователя;
- перед сдачей задания показывай что и куда прикрепишь;
- на вопросы вне твоих данных — ИЩИ В ИНТЕРНЕТЕ, не отвечай «не знаю»;
- не выдумывай данные — проверяй инструментами;
- ДЛИННЫЕ списки сокращай: топ-10 главное + «всего N» (Telegram не любит
  простыни);
- отвечай по-русски, кратко, по делу."""}


def _moodle_user() -> str:
    from .moodle import moodle
    return moodle.user_name or "студент"


agent = AgentSession()
