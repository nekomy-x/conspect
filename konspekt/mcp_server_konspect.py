"""MCP-сервер «konspect»: конспекты лекций и генерация ответов.

Инструменты: обработка записи BBB (транскрипт+слайды+конспект+презентация),
статус, анализ заданий, генерация ответа.

Запуск (stdio): .venv/bin/python -m konspekt.mcp_server_konspect
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from .config import cfg
from .db import db

log = logging.getLogger("konspect.mcp.conspect")
mcp = MCPServer("konspekt-conspect",
                description="Конспекты лекций BBB: транскрипт, слайды, "
                            "конспект, презентация; ответы на задания",
                version="0.1.0")


@mcp.tool()
async def conspect_status() -> dict:
    """Готовые конспекты + необработанные записи."""
    root = Path(cfg.data_dir) / "conspects"
    conspects = []
    if root.exists():
        for d in sorted(root.iterdir()):
            md = d / "conspect.md"
            if md.exists():
                conspects.append({
                    "name": d.name,
                    "conspect": str(md),
                    "presentation": (d / "presentation.html").exists(),
                    "size_kb": md.stat().st_size // 1024})
    return {"conspects": conspects,
            "unprocessed_recordings": [
                {"title": r["title"], "url": r["playback_url"]}
                for r in db.unprocessed_recordings()]}


@mcp.tool()
async def process_recording(playback_url: str, course_name: str = "",
                            lecture_title: str = "", force: bool = False) -> dict:
    """Полный конвейер: запись BBB → Whisper-транскрипт + слайды → конспект.md
    → presentation.html. Идемпотентно (готовый не трогает; force — переделать)."""
    from .ai.pipeline import process_recording
    result = await process_recording(playback_url, course_name=course_name,
                                     lecture_title=lecture_title, force=force)
    result.pop("recording", None)
    return result


@mcp.tool()
async def recording_info(playback_url: str) -> dict:
    """Метаданные записи: название, курс, длительность, слайды, видео."""
    from .media import fetch_recording_info
    info = await fetch_recording_info(playback_url)
    return {"name": info.get("name"), "course": info.get("context-name"),
            "duration_min": round(info.get("duration_s", 0) / 60),
            "slides": len(info.get("slides", [])),
            "has_video": bool(info.get("webcams_url"))}


@mcp.tool()
async def analyze_assignment(cmid: int) -> dict:
    """Разобрать задание (cmid): требования, что сдать, шаги, камни, вложения."""
    from .moodle import moodle
    from .ai.assignments import analyze_assignment
    info = await moodle.fetch_assignment(cmid)
    analysis = await analyze_assignment(info)
    struct = analysis["analysis"].get("structure", {})
    pit = analysis["analysis"].get("pitfalls", {})
    courses = {c["id"]: c["name"] for c in db.courses()}
    return {"course": courses.get(info.course_id, ""),
            "name": info.name, "intro": info.intro[:2000],
            "deadline": info.deadline_ts,
            "what_to_submit": struct.get("deliverables", [])[:12],
            "requirements": struct.get("requirements", [])[:12],
            "steps": struct.get("steps", [])[:12],
            "pitfalls": pit.get("pitfalls", [])[:6]}


@mcp.tool()
async def write_assignment_answer(cmid: int) -> dict:
    """Сгенерировать готовый ответ на задание (cmid) с автопроверкой."""
    from .moodle import moodle
    from .ai.assignments import (analyze_assignment, generate_answer,
                                 check_answer)
    info = await moodle.fetch_assignment(cmid)
    analysis = await analyze_assignment(info)
    answer = await generate_answer(info, analysis)
    verdict = await check_answer(info, answer, analysis)
    path = Path(cfg.data_dir) / "assignments" / str(cmid) / "answer.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# {info.name}\n\n{answer}", encoding="utf-8")
    db.update_assignment(cmid, state="ready", answer_path=str(path))
    return {"file_path": str(path), "verdict": verdict,
            "what_to_submit": analysis["analysis"].get(
                "structure", {}).get("deliverables", [])[:6],
            "answer_preview": answer[:2500]}


@mcp.tool()
async def conspect_file(name: str) -> dict:
    """Прочитать готовый конспект (name из conspect_status)."""
    md = Path(cfg.data_dir) / "conspects" / name / "conspect.md"
    if not md.exists():
        return {"error": f"конспект не найден: {name}"}
    return {"text": md.read_text(encoding="utf-8")[:40000]}


async def main() -> None:
    await mcp.run_stdio_async()


if __name__ == "__main__":
    logging.basicConfig(level=cfg.log_level)
    asyncio.run(main())
