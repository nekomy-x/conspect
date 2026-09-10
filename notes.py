"""Форматирование и сохранение конспектов: Markdown (Obsidian) + site (MkDocs)."""
from __future__ import annotations

import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from html import unescape

from config import Config, ensure_dirs
from align import AlignedData, SlideChunk


# ── Типографика (лёгкий проход) ─────────────────────────────────────────────

def fix_typography(text: str) -> str:
    """
    Исправляет базовую типографику русского языка.

    Работает построчно и НЕ трогает:
    - YAML-фронтматтер (всё до первой пустой строки после ---)
    - fenced code blocks (```...```)
    - Markdown-списки, таблицы, blockquote, горизонтальные линии
    """
    lines = text.split("\n")
    in_frontmatter = text.startswith("---")
    in_code = False
    out_lines = []

    for line in lines:
        # ── пропуск структурных частей ──
        if line.startswith("---"):
            if in_frontmatter:
                in_frontmatter = False          # конец YAML
            out_lines.append(line)
            continue
        if in_frontmatter:
            out_lines.append(line)              # YAML — не трогаем
            continue

        if line.lstrip().startswith("```"):
            in_code = not in_code
            out_lines.append(line)
            continue
        if in_code:
            out_lines.append(line)
            continue

        stripped = line.lstrip()
        if stripped.startswith(("|", "-", "*", ">", "```")):
            out_lines.append(line)              # список/таблица/цитата — не трогаем
            continue

        # Пропускаем строки с HTML-тегами (атрибуты содержат кавычки, которые мы не трогаем)
        if re.search(r'<[a-z][^>]*>', line, re.IGNORECASE):
            out_lines.append(line)
            continue

        # ── типографика в прозе ──
        # "текст" → «текст»
        line = re.sub(r'"([^"\n]{1,300})"', lambda m: f'«{m.group(1)}»', line)
        # слово - слово → слово — слово (требуем непустые соседи,
        # чтобы не ломать дефисы внутри слов и не трогать коды/URL)
        line = re.sub(r'(?<=\S)\s+-\s+(?=\S)', ' — ', line)
        out_lines.append(line)

    return "\n".join(out_lines)


# ── Слайды → assets ─────────────────────────────────────────────────────────

def copy_slides_to_assets(
    aligned: AlignedData,
    vault_assets: Path,
    docs_assets: Path,
) -> dict[int, str]:
    """
    Копирует PNG-слайды в vault/assets/<recordId>/ и docs/assets/<recordId>/.
    Возвращает маппинг slide_num → relative_path (относительно docs/конспекты/).
    """
    record_id = aligned.record_id
    vault_dir = vault_assets / record_id
    docs_dir = docs_assets / record_id

    vault_dir.mkdir(parents=True, exist_ok=True)
    docs_dir.mkdir(parents=True, exist_ok=True)

    slide_map: dict[int, str] = {}
    for chunk in aligned.chunks:
        if not chunk.png_path or not chunk.png_path.exists():
            continue
        filename = f"slide_{chunk.slide_num:03d}.png"

        # Копируем в обе папки (если не дубликат)
        dst_vault = vault_dir / filename
        dst_docs = docs_dir / filename

        if not dst_vault.exists():
            shutil.copy2(chunk.png_path, dst_vault)
        if not dst_docs.exists():
            shutil.copy2(chunk.png_path, dst_docs)

        slide_map[chunk.slide_num] = f"../assets/{record_id}/{filename}"

    print(f"   🖼  Скопировано {len(slide_map)} слайдов в assets/")
    return slide_map


# ── Подстановка путей к слайдам в конспекте ─────────────────────────────────

def fix_slide_references(text: str, slide_map: dict[int, str]) -> str:
    """
    Заменяет ссылки на слайды в Markdown на относительные пути
    (slide_map задаёт пути для конкретного целевого формата).
    """
    for slide_num, rel_path in slide_map.items():
        # Obsidian-style: ![[slide_NNN.png]] или ![[slide_NNN|caption]]
        text = re.sub(
            rf'!\[\[slide[_]?{slide_num:03d}(?:\|[^\]]*)?\]\]',
            f'![Слайд {slide_num}]({rel_path})',
            text,
        )
        # Generic: ![slide_NNN.png](...) or ![Слайд NNN](...)
        text = re.sub(
            rf'!\[([^\]]*slide[_\s]*{slide_num:03d}[^\]]*)\]\([^)]*\)',
            f'![Слайд {slide_num}]({rel_path})',
            text,
        )
        # HTML img tags
        text = re.sub(
            rf'<img[^>]*src="[^"]*slide[_]?{slide_num:03d}\.png[^"]*"[^>]*>',
            f'![Слайд {slide_num}]({rel_path})',
            text,
        )

    return text


# ── Генерация Markdown-файла ────────────────────────────────────────────────

def _make_frontmatter(
    aligned: AlignedData,
    cfg: Config,
    filename_stem: str,
) -> str:
    """Создаёт YAML-фронтматтер."""
    # Извлекаем основную тему из названия лекции
    topic = aligned.meeting_name.split(".")[0].strip() if aligned.meeting_name else "lecture"
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    tags = [
        cfg.course_name.lower().replace(" ", "-"),
        f"lecture/{topic.lower().replace(' ', '-')}",
    ]

    lines = [
        "---",
        "tags:",
        *[f"  - {t}" for t in tags],
        f"date: {date_str}",
        f'source: "https://bbb.edu.vsu.ru/playback/presentation/2.3/{aligned.record_id}"',
        "---",
        "",
    ]
    return "\n".join(lines)


def _make_filename(aligned: AlignedData) -> str:
    """Создаёт имя файла: дата — тема."""
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # Берём тему из названия (после "Лекция N." или первые 60 символов)
    name = aligned.meeting_name or aligned.record_id[:8]
    name = re.sub(r'^\s*\d+\.\s*', '', name)          # убираем "Лекция 1. "
    name = re.sub(r'[^\w\s\-а-яА-ЯёЁa-zA-Z0-9]+', '', name)  # чистим
    name = re.sub(r'\s+', ' ', name).strip()[:60]
    return f"{date_str} — {name}"


def save_notes(
    aligned: AlignedData,
    llm_content: str,
    cfg: Config,
) -> dict[str, Path]:
    """
    Сохраняет конспект:
    1. vault/<filename>.md — Obsidian-совместимый Markdown
    2. docs/конспекты/<filename>.md — для MkDocs

    Возвращает пути к файлам: {"vault": Path, "docs": Path}
    """
    ensure_dirs(cfg)

    filename = _make_filename(aligned)
    vault_assets = cfg.vault_dir / "assets"
    docs_assets = cfg.docs_dir / "assets"

    # 1. Копируем слайды
    slide_map = copy_slides_to_assets(aligned, vault_assets, docs_assets)

    # 2. Готовим содержимое
    # Два варианта путей к слайдам:
    #   docs:  ../assets/<id>/slide_NNN.png  (относительно docs/конспекты/)
    #   vault: assets/<id>/slide_NNN.png     (относительно vault/, без ../)
    vault_map = {n: p[3:] if p.startswith("../") else p for n, p in slide_map.items()}

    # Исправляем ссылки на слайды в формате docs (относительные пути)
    content = fix_slide_references(llm_content, slide_map)

    # Добавляем фронтматтер
    frontmatter = _make_frontmatter(aligned, cfg, filename)

    # Лёгкая типографика (только body, не YAML-фронтматтер)
    content = frontmatter + fix_typography(content)

    # 3. Сохраняем в docs (для MkDocs)
    docs_path = cfg.docs_dir / "конспекты" / f"{filename}.md"
    docs_path.parent.mkdir(parents=True, exist_ok=True)
    docs_path.write_text(content, encoding="utf-8")
    print(f"   📄 Docs: {docs_path}")

    # 4. Сохраняем в vault (Obsidian) — свои относительные пути к слайдам
    vault_content = frontmatter + fix_typography(fix_slide_references(llm_content, vault_map))
    vault_path = cfg.vault_dir / f"{filename}.md"
    vault_path.parent.mkdir(parents=True, exist_ok=True)
    vault_path.write_text(vault_content, encoding="utf-8")
    print(f"   📄 Vault: {vault_path}")

    return {"vault": vault_path, "docs": docs_path}


# ── Обновление индекса сайта ────────────────────────────────────────────────

def update_docs_index(docs_dir: Path) -> None:
    """Генерирует docs/index.md со списком всех конспектов."""
    notes_dir = docs_dir / "конспекты"
    if not notes_dir.exists():
        return

    md_files = sorted(notes_dir.glob("*.md"), reverse=True)
    lines = [
        "# Конспекты лекций",
        "",
        f"_Последнее обновление: {datetime.now(timezone.utc).strftime('%Y-%m-%d')}_",
        "",
        "## Список лекций",
        "",
    ]

    for f in md_files:
        # Читаем первые строки для заголовка
        content = f.read_text(encoding="utf-8")
        title_match = re.search(r'^# (.+)$', content, re.MULTILINE)
        title = title_match.group(1) if title_match else f.stem
        lines.append(f"- [{title}](конспекты/{f.name})")

    lines.extend([
        "",
        "---",
        "",
        "Конспекты сгенерированы автоматически из записей BigBlueButton.",
    ])

    index_path = docs_dir / "index.md"
    index_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"   📋 Индекс: {index_path}")
