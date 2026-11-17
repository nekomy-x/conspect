"""Пайплайн конспектов: запись BBB → слайды + транскрипт → консект.md → презентация.

Шаги (каждый = запрос ИИ с проверкой):
  1. Скачивание структуры записи (слайды SVG→PNG, тексты) + аудио
  2. Whisper-транскрипт
  3. Конспект: выжимка со всеми важными моментами + цитаты лектора
  4. Проверка конспекта на полноту (контрольный запрос)
  5. Презентация: структура слайдов (JSON) → HTML-презентация
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path

from ..config import cfg
from ..media import (
    fetch_recording_info, download_file, download_slides,
    transcribe_async, transcribe_chunked, extract_audio, extract_video_frames,
    safe_name,
)
from .client import ai, run_step

log = logging.getLogger("konspect.conspect")

TRANSCRIPT_SYSTEM = "Ты — редактор стенограмм лекций. Пишешь плотно, без воды, сохраняешь факты, определения, примеры."

# глобальный замок: Whisper на CPU — ОДИН тяжёлый конвейер за раз на всю систему
# (иначе параллельные конспекты валят ноут по CPU/памяти)
_pipeline_lock = asyncio.Lock()


async def process_recording(playback_url: str, course_name: str = "",
                            lecture_title: str = "", work_dir: Path | None = None,
                            force: bool = False) -> dict:
    """Полный цикл обработки записи. Возвращает пути артефактов.

    Если conspect.md уже существует и force=False — ничего не переделывает,
    сразу возвращает готовые артефакты.
    """
    # кэш-проверка БЕЗ замка (мгновенный выход для готовых)
    info = await fetch_recording_info(playback_url)
    name = lecture_title or info.get("name") or "Лекция"
    course = course_name or info.get("context-name") or ""
    # слаг СТАБИЛЬНЫЙ: по ID записи — не зависит от переданных имён
    # (одна запись = одна папка, кэш не ломается)
    rec_id = info.get("id", "")
    slug = safe_name(f"{course}_{name}") if not rec_id else safe_name(rec_id)
    work = work_dir or (cfg.data_dir / "conspects" / slug)
    work.mkdir(parents=True, exist_ok=True)
    # человекочитаемый алиас для отображения
    result: dict = {"work_dir": str(work), "slug": slug,
                    "title": f"{course} — {name}", "recording": info}

    # уже сделано? — не трогаем
    existing_md = work / "conspect.md"
    if existing_md.exists() and not force:
        result["conspect"] = str(existing_md)
        result["already_done"] = True
        for key, fname in (("transcript", "transcript.txt"),
                           ("presentation", "presentation.html")):
            p = work / fname
            if p.exists():
                result[key] = str(p)
        if (work / "slides").exists():
            result["slides"] = str(work / "slides")
        log.info("конспект уже существует (%s) — пропускаю", existing_md)
        return result

    # тяжёлая часть — строго по очереди
    async with _pipeline_lock:

        # 1. слайды: презентация (SVG→PNG) или кадры из deskshare (шэринг экрана)
        slides: list[dict] = []
        if len(info.get("slides", [])) > 1:
            slides = await download_slides(info, work / "slides")
        elif info.get("deskshare_url") and not (work / "slides").exists():
            # лектор делился экраном (1 «слайд»-заглушка) — достаём кадры
            log.info("презентации нет — беру кадры из deskshare")
            ds = await download_file(info["deskshare_url"], work / "deskshare.webm")
            if ds:
                frames = await extract_video_frames(ds, work / "slides",
                                                    every=90, max_frames=50)
                slides = [{"n": i, "png": str(p), "text": "",
                           "t_in": i * 90, "t_out": i * 90 + 90}
                          for i, p in enumerate(frames, 1)]
        result["slides"] = slides

        # 2. аудио + транскрипт (по чанкам 10 мин — Whisper в память не грузит всё)
        transcript_path = work / "transcript.txt"
        if not transcript_path.exists():
            # пробуем webcams.webm, если нет — deskshare.webm (шаринг экрана)
            video_url = info.get("webcams_url") or info.get("deskshare_url")
            video_name = "webcams.webm" if info.get("webcams_url") else "deskshare.webm"
            if not video_url:
                result["error"] = "У записи нет аудио/видео (webcams/deskshare)"
                return result
            video = await download_file(video_url, work / video_name)
            if not video:
                result["error"] = "Не удалось скачать видео записи"
                return result
            audio = await extract_audio(video, work)
            transcript_path = await transcribe_chunked(audio, work)
        result["transcript"] = str(transcript_path)
        raw_text = transcript_path.read_text(encoding="utf-8")

        # 3. тексты слайдов одним блоком
        slides_text = "\n\n".join(
            f"=== Слайд {s['n']} ({int(s['t_in'])//60:02d}:{int(s['t_in'])%60:02d}–"
            f"{int(s['t_out'])//60:02d}:{int(s['t_out'])%60:02d}) ===\n{s['text']}"
            for s in slides if s.get("text"))
        if slides_text:
            (work / "slides_text.txt").write_text(slides_text, encoding="utf-8")

        # 4. конспект (многошагово, с проверкой)
        conspect = await generate_conspect(course, name, raw_text, slides_text)
        md_path = work / "conspect.md"
        md_path.write_text(conspect, encoding="utf-8")
        result["conspect"] = str(md_path)

        # 5. интерактивный обучающий курс (LiaScript: квизы, флеш-карточки)
        try:
            from .quiz import generate_interactive
            inter = await generate_interactive(md_path, f"{course} — {name}")
            if not inter.get("error"):
                result["course_html"] = inter["course_html"]
                result["course_md"] = inter["course_md"]
        except Exception as e:  # noqa: BLE001
            log.warning("interactive course: %s", e)

        # 6. презентация
        pres = await generate_presentation(course, name, conspect, work)
        result.update(pres)
        return result


    # ---------------------------------------------------------------------------


async def generate_conspect(course_name: str, lecture_title: str,
                            transcript: str, slides_text: str) -> str:
    """Конспект с контролем качества (краткая выжимка, но без потери сути)."""

    def build(feedback: str | None) -> list[dict]:
        msgs = [
            {"role": "system", "content": TRANSCRIPT_SYSTEM},
            {"role": "user", "content": (
                f"Курс: {course_name}\nЛекция: {lecture_title}\n\n"
                f"===СТЕНОГРАММА===\n{cut(transcript, 40000)}\n\n"
                + (f"===ТЕКСТ С СЛАЙДОВ===\n{cut(slides_text, 12000)}\n\n" if slides_text else "")
                + "Составь конспект лекции на русском:\n"
                "- структура по темам/разделам лекции;\n"
                "- все определения, формулы, теги, команды, примеры кода;\n"
                "- важные дословные формулировки лектора в виде цитат «...»;\n"
                "- ничего не выдумывай; если в стенограмме нет — не пиши.\n"
                "Формат — Markdown: ## заголовки разделов, списки, ```код```, "
                "> цитаты лектора."
            )},
        ]
        if feedback:
            msgs.append({"role": "user", "content": feedback})
        return msgs

    def validate(txt: str) -> str | None:
        if len(txt) < 600:
            return "слишком короткий конспект для лекции"
        if not re.search(r"^##\s", txt, re.M):
            return "нет структуры заголовков ##"
        return None

    conspect = await run_step("conspect", build, validate)

    # контрольный проход: проверка полноты
    try:
        check = await ai.chat_json([
            {"role": "system", "content": "Ты — контролёр качества конспектов. Отвечай только JSON."},
            {"role": "user", "content": (
                f"Стенограмма (фрагменты):\n{cut(transcript, 15000)}\n\n"
                f"Тексты слайдов:\n{cut(slides_text, 8000)}\n\n"
                f"Конспект:\n{cut(conspect, 15000)}\n\n"
                'Верни JSON {"missing": ["что упущено"], "verdict": "ok|fix"}'
            )},
        ])
        if check.get("verdict") == "fix" and check.get("missing"):
            addendum = await ai.chat([
                {"role": "system", "content": TRANSCRIPT_SYSTEM},
                {"role": "user", "content": (
                    f"В конспекте упущено: {'; '.join(check['missing'])}\n\n"
                    f"Стенограмма:\n{cut(transcript, 30000)}\n\n"
                    f"Тексты слайдов:\n{cut(slides_text, 8000)}\n\n"
                    f"Текущий конспект:\n{conspect}\n\n"
                    "Дополни конспект этими разделами и верни ПОЛНЫЙ улучшенный "
                    "Markdown-конспект целиком."
                )},
            ])
            if len(addendum) > len(conspect) * 0.8:
                conspect = addendum
    except Exception as e:  # noqa: BLE001
        log.warning("conspect QC failed: %s", e)
    return conspect


def cut(text: str, limit: int) -> str:
    """Равномерная вырезка: начало + середина + конец, чтобы не терять хвост."""
    if not text:
        return ""
    if len(text) <= limit:
        return text
    third = limit // 3
    head = text[:third]
    mid = text[len(text) // 2 - third // 2: len(text) // 2 + third // 2]
    tail = text[-third:]
    return (f"{head}\n[...пропуск...]\n{mid}\n[...пропуск...]\n{tail}"
            f"\n[Всего {len(text)} символов; обрезано для лимита]")


# ---------------------------------------------------------------------------


async def generate_presentation(course_name: str, lecture_title: str,
                                conspect: str, work_dir: Path) -> dict:
    """Слайды: JSON-структура → HTML-презентация (стрелки, анимации)."""
    def build(feedback: str | None) -> list[dict]:
        msgs = [
            {"role": "system", "content": "Ты — дизайнер обучающих презентаций. Отвечай только JSON."},
            {"role": "user", "content": (
                f"Курс: {course_name}. Лекция: {lecture_title}\n\nКонспект:\n{cut(conspect, 18000)}\n\n"
                "Составь презентацию по лекции: 10–16 слайдов.\n"
                'Верни JSON {"title": "...", "slides": [{"heading": "...", '
                '"bullets": ["..."], "quote": "цитата лектора или null", '
                '"code": "код или null", "note": "заметка докладчику"}]}'
                "\nПравила: на слайде 3–5 пунктов, каждый до 90 символов; "
                "формулы и код — в code; главный тезис — quote."
            )},
        ]
        if feedback:
            msgs.append({"role": "user", "content": feedback})
        return msgs

    def validate(txt: str) -> str | None:
        try:
            data = json.loads(txt)
            if not data.get("slides"):
                return "нет слайдов"
            if len(data["slides"]) < 6:
                return "слишком мало слайдов"
            return None
        except json.JSONDecodeError:
            return "не JSON"

    raw = await run_step("slides", build, validate, json_mode=True)
    data = json.loads(raw)
    html = render_presentation_html(data)
    html_path = work_dir / "presentation.html"
    html_path.write_text(html, encoding="utf-8")
    return {"presentation": str(html_path), "slides": data}


def render_presentation_html(data: dict) -> str:
    slides = data.get("slides", [])
    title = data.get("title", "Лекция")
    nav = "".join(f'<button class="dot" data-i="{i}"></button>' for i in range(len(slides)))
    sections = []
    for i, s in enumerate(slides):
        bullets = "".join(f"<li>{b}</li>" for b in s.get("bullets", []))
        quote = f'<blockquote>«{s["quote"]}»</blockquote>' if s.get("quote") else ""
        code = (f"<pre><code>{s['code']}</code></pre>" if s.get("code") else "")
        note = f'<div class="note">{s["note"]}</div>' if s.get("note") else ""
        sections.append(
            f'<section class="slide" data-i="{i}">'
            f'<div class="num">{i + 1}/{len(slides)}</div>'
            f'<h2>{s.get("heading", "")}</h2><ul>{bullets}</ul>'
            f"{quote}{code}{note}</section>"
        )
    return f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
:root{{--bg:#0f1220;--card:#171b2e;--acc:#7c9cff;--tx:#e8ecff;--mut:#8a91b4}}
*{{box-sizing:border-box;margin:0}}
body{{background:var(--bg);color:var(--tx);font:16px/1.6 'Segoe UI',system-ui,sans-serif;
  height:100vh;overflow:hidden;display:flex;flex-direction:column;align-items:center;justify-content:center}}
h1{{font-size:1.6rem;margin-bottom:.4rem;color:var(--acc)}}
.slide{{display:none;background:var(--card);border-radius:18px;padding:2.2rem 2.6rem;
  max-width:860px;width:92vw;box-shadow:0 18px 60px rgba(0,0,0,.45);animation:in .45s ease;
  position:relative}}
.slide.active{{display:block}}
@keyframes in{{from{{opacity:0;transform:translateY(14px) scale(.98)}}to{{opacity:1;transform:none}}}}
h2{{color:var(--acc);margin-bottom:1rem;font-size:1.35rem}}
ul{{list-style:none}} li{{margin:.55rem 0;padding-left:1.3rem;position:relative}}
li::before{{content:'→';position:absolute;left:0;color:var(--acc)}}
blockquote{{margin:1rem 0;padding:.8rem 1.2rem;border-left:4px solid var(--acc);
  background:rgba(124,156,255,.08);border-radius:0 10px 10px 0;font-style:italic;color:var(--mut)}}
pre{{background:#0b0e18;padding:1rem;border-radius:10px;overflow:auto;margin-top:1rem;font-size:.9rem}}
code{{font-family:'JetBrains Mono',monospace;color:#9ff0c8}}
.note{{margin-top:1rem;font-size:.85rem;color:var(--mut)}}
.num{{position:absolute;top:1rem;right:1.4rem;color:var(--mut);font-size:.8rem}}
#dots{{display:flex;gap:.5rem;margin-top:1.2rem}}
.dot{{width:10px;height:10px;border-radius:50%;border:0;background:var(--mut);cursor:pointer;opacity:.4}}
.dot.on{{background:var(--acc);opacity:1;transform:scale(1.25)}}
.nav{{position:fixed;bottom:18px;display:flex;gap:1rem}}
.nav button{{background:var(--card);color:var(--tx);border:1px solid #2a3050;border-radius:10px;
  padding:.5rem 1.2rem;cursor:pointer;font-size:1rem}}
.nav button:hover{{border-color:var(--acc)}}
</style></head><body>
<h1>{title}</h1>
{''.join(sections)}
<div id="dots">{nav}</div>
<div class="nav"><button onclick="go(-1)">←</button><button onclick="go(1)">→</button></div>
<script>
let i=0;const S=document.querySelectorAll('.slide'),D=document.querySelectorAll('.dot');
function show(n){{i=(n+S.length)%S.length;S.forEach(s=>s.classList.toggle('active',+s.dataset.i===i));
D.forEach(d=>d.classList.toggle('on',+d.dataset.i===i));}}
function go(d){{show(i+d)}} document.addEventListener('keydown',e=>{{
if(e.key==='ArrowRight'||e.key===' ')go(1);if(e.key==='ArrowLeft')go(-1);}});
D.forEach(d=>d.onclick=()=>show(+d.dataset.i));show(0);
</script></body></html>"""
