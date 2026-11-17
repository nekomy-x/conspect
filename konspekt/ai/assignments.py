"""Анализ заданий (assign): вложения, требования, генерация ответа, память курса."""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import pymupdf

from ..config import cfg
from ..db import db
from ..moodle import moodle, AssignmentInfo
from .client import ai, analyze_document

log = logging.getLogger("konspect.assignments")


def pdf_text(path: Path | str, max_pages: int = 40) -> str:
    doc = pymupdf.open(str(path))
    parts = []
    for i, page in enumerate(doc):
        if i >= max_pages:
            parts.append("[... дальше обрезано ...]")
            break
        parts.append(page.get_text())
    return "\n".join(parts)


async def collect_assignment_text(info: AssignmentInfo) -> tuple[str, list[Path]]:
    """Текст задания + тексты всех вложений (PDF)."""
    texts = [f"Задание: {info.name}\n\n{info.intro}"]
    files: list[Path] = []
    att_dir = cfg.data_dir / "assignments" / str(info.cmid)
    att_dir.mkdir(parents=True, exist_ok=True)
    for att in info.attachments[:6]:
        name = att["name"]
        dest = att_dir / re.sub(r"[^\w.\-]", "_", name)[-80:]
        try:
            if not dest.exists():
                await moodle.download(att["url"], str(dest))
            files.append(dest)
            if dest.suffix.lower() == ".pdf":
                texts.append(f"===ФАЙЛ {name}===\n{pdf_text(dest)}")
        except Exception as e:  # noqa: BLE001
            log.warning("attachment %s failed: %s", name, e)
    return "\n\n".join(texts), files


async def analyze_assignment(info: AssignmentInfo) -> dict:
    """Многошаговый анализ задания + память курса. Возвращает структуру для БД/ТГ."""
    course_id = info.course_id
    course = next((c for c in db.courses() if c["id"] == course_id), None)
    course_name = course["name"] if course else f"Курс {course_id}"

    full_text, files = await collect_assignment_text(info)

    # память курса: титульные листы и прошлые лабы
    memory = db.memory(course_id)
    context = {
        "курс": course_name,
        "титульный_лист": memory.get("title_page_hint"),
        "прошлые_лабы": [f"{m.get('name')} — сдано {m.get('submitted')}" for m in memory.get("assignments", [])][-5:],
        "курс_обучения": memory.get("study_year", "2 курс"),
    }

    analysis = await analyze_document(
        purpose=f"Лабораторная/задание «{info.name}». Нужно понять: что требуется, "
                "какие шаги, что сдать, в каком формате.",
        doc_text=full_text,
        context=context,
    )

    # сохраняем в память курса
    db.memory_push_list(course_id, "assignments", {
        "cmid": info.cmid, "name": info.name, "deadline": info.deadline_ts,
    })
    return {"analysis": analysis, "files": [str(f) for f in files], "full_text": full_text}


async def generate_answer(info: AssignmentInfo, analysis: dict) -> str:
    """Генерирует готовый ответ/файл для сдачи с учётом памяти курса."""
    course = next((c for c in db.courses() if c["id"] == info.course_id), None)
    course_name = course["name"] if course else ""
    memory = db.memory(info.course_id)
    structure = json.dumps(analysis.get("analysis", {}).get("structure", {}), ensure_ascii=False)
    pitfalls = json.dumps(analysis.get("analysis", {}).get("pitfalls", {}), ensure_ascii=False)

    msg = await ai.chat([
        {"role": "system", "content":
            "Ты — студент, выполняющий учебное задание качественно. "
            "Пиши готовый к сдаче материал: точно по требованиям, с титульным контуром, "
            "без отсебятины против методички."},
        {"role": "user", "content": (
            f"Курс: {course_name} ({memory.get('study_year', '2 курс')})\n"
            f"Задание: {info.name}\n"
            f"Описание: {info.intro}\n\n"
            f"Анализ требований: {structure}\n"
            f"Подводные камни: {pitfalls}\n"
            f"Образец титульного листа: {memory.get('title_page_hint', 'нет')}\n\n"
            "Составь ПОЛНЫЙ ответ на задание — то, что студент перепишет и сдаст: "
            "тексты, код, таблицы, пояснения по шагам из хода работы. "
            "В начале — что именно и в каком виде прикрепить (какие файлы). "
            "Формат Markdown."
        )},
    ])
    return msg


async def check_answer(info: AssignmentInfo, answer: str, analysis: dict) -> str:
    """Проверка ответа по чек-листу требований. Возвращает текст замечаний или «ok»."""
    requirements = analysis.get("analysis", {}).get("structure", {}).get("requirements", [])
    try:
        verdict = await ai.chat_json([
            {"role": "system", "content": "Ты — проверяющий преподаватель. Отвечай только JSON."},
            {"role": "user", "content": (
                f"Требования: {json.dumps(requirements, ensure_ascii=False)}\n\n"
                f"Ответ студента:\n{answer[:20000]}\n\n"
                'Проверь каждое требование. Верни JSON {"verdict": "ok"|"fix", '
                '"problems": ["что не так"]}'
            )},
        ])
        if verdict.get("verdict") == "ok":
            return "ok"
        return "Замечания:\n- " + "\n- ".join(verdict.get("problems", []))
    except Exception as e:  # noqa: BLE001
        log.warning("check_answer failed: %s", e)
        return "проверка не удалась, смотри вручную"
