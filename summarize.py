"""Генерация конспекта через LLM (OpenAI-compatible API)."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from openai import OpenAI

from align import AlignedData, SlideChunk, format_chunk_for_llm, format_aligned_for_llm, format_aligned_summary
from config import Config

PROMPT_DIR = Path(__file__).parent / "prompts"
SYSTEM_PROMPT_FILE = PROMPT_DIR / "note_system.md"

# Размер чанка (слайдов на один запрос к LLM)
CHUNK_SIZE = 10
# Максимальная длина текста для одного чанка (символов)
CHUNK_MAX_CHARS = 15000


def _load_system_prompt() -> str:
    """Загружает системный промпт из файла."""
    if SYSTEM_PROMPT_FILE.exists():
        return SYSTEM_PROMPT_FILE.read_text(encoding="utf-8")
    raise FileNotFoundError(f"Системный промпт не найден: {SYSTEM_PROMPT_FILE}")


def _build_user_prompt_chunk(
    chunk_text: str,
    chunk_summary_text: str,
    chunk_idx: int,
    total_chunks: int,
    meeting_name: str,
) -> str:
    """Формирует user-промпт для одного чанка."""
    lines = [
        f"Ты создаёшь конспект лекции «{meeting_name}».",
        f"Это часть {chunk_idx + 1} из {total_chunks} (/{total_chunks} — это полный контекст лекции).",
        "",
        "=== ОБЩИЙ КОНТЕКСТ (оглавление всей лекции) ===",
        chunk_summary_text,
        "",
        "=== МАТЕРИАЛ ДЛЯ КОНСПЕКТА ЭТОГО ЧАНКА ===",
        chunk_text,
        "",
        "Создай красивый конспект именно ЭТОЙ части лекции, с учётом общего контекста.",
        "Используй формат: интуиция → определение → пример → таблица → вопросы для самопроверки.",
        "Ссылки на другие разделы: [[название]].",
        "Встрой изображения слайдов если есть.",
        "Пиши на русском языке. Следуй правилам стиля из системного промпта.",
    ]
    return "\n".join(lines)


def _build_user_prompt_final(
    chunks_content: list[str],
    summary_text: str,
    meeting_name: str,
) -> str:
    """Формирует финальный user-промпт для merge-шага."""
    lines = [
        f"Ты создаёшь финальный конспект лекции «{meeting_name}».",
        "Тебе даны черновые конспекты отдельных частей. Твоя задача:",
        "1. Объедини их в единый связный документ.",
        "2. Добавь вступление (In a nutshell) и общую схему (Mermaid).",
        "3. Убери повторы, сохрани все ключевые факты.",
        "4. Добавь итоговую таблицу и вопросы для самопроверки.",
        "",
        "=== ОБЩИЙ КОНТЕКСТ ===",
        summary_text,
        "",
        "=== ЧЕРНОВИКИ ЧАНКОВ ===",
    ]
    for i, content in enumerate(chunks_content):
        lines.append(f"\n--- ЧАНК {i + 1} ---")
        lines.append(content[:CHUNK_MAX_CHARS])

    lines.append("")
    lines.append("Создай финальный красивый конспект. Выведи ТОЛЬКО Markdown-документ без комментариев.")
    return "\n".join(lines)


@dataclass
class LLMResponse:
    content: str
    model: str = ""
    tokens_used: int = 0


def _call_llm(
    client: OpenAI,
    model: str,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = 4096,
) -> LLMResponse:
    """Один запрос к LLM."""
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=max_tokens,
        temperature=0.3,
    )
    choice = resp.choices[0]
    usage = resp.usage
    return LLMResponse(
        content=choice.message.content or "",
        model=model,
        tokens_used=(usage.total_tokens if usage else 0),
    )


def generate_notes(
    aligned: AlignedData,
    cfg: Config,
) -> str:
    """
    Генерирует Markdown-конспект через LLM.

    Если лекция короткая (≤CHUNK_SIZE слайдов) — один запрос.
    Если длинная — map-reduce: черновики по чанкам → финальное объединение.
    """
    print(f"\n🤖 Генерация конспекта через {cfg.llm_model}...")

    if not cfg.llm_api_key:
        print("   ⚠ LLM_API_KEY не задан — генерирую конспект без LLM (только структура)")
        return _generate_notes_without_llm(aligned)

    # Пустой таймлайн (нет слайдов) — LLM нечего конспектировать, не даём галлюцинировать
    if not aligned.chunks:
        print("   ⚠ Нет слайдов для конспекта — возвращаю заглушку")
        return _generate_notes_without_llm(aligned)

    client = OpenAI(api_key=cfg.llm_api_key, base_url=cfg.llm_base_url)
    system_prompt = _load_system_prompt()

    # Заголовок / контекст всей лекции
    summary_text = format_aligned_summary(aligned)
    print(f"   📝 Лекция: {aligned.meeting_name}")
    print(f"   📑 Слайдов: {aligned.total_slides}")

    # Короткая лекция — один запрос
    if len(aligned.chunks) <= CHUNK_SIZE:
        user_prompt = _build_user_prompt_chunk(
            chunk_text="\n".join(
                format_chunk_for_llm(c, i + 1, len(aligned.chunks))
                for i, c in enumerate(aligned.chunks)
            ),
            chunk_summary_text=summary_text,
            chunk_idx=0,
            total_chunks=1,
            meeting_name=aligned.meeting_name,
        )

        resp = _call_llm(client, cfg.llm_model, system_prompt, user_prompt, cfg.llm_max_tokens)
        print(f"   ✓ Конспект сгенерирован ({resp.tokens_used} токенов)")
        return resp.content

    # Длинная лекция — map-reduce
    print(f"   📦 Разбиваю на {len(aligned.chunks)} чанков по {CHUNK_SIZE} слайдов...")

    # Map: черновики по чанкам
    chunk_drafts = []
    llm_chunks = format_aligned_for_llm(aligned, max_slides=CHUNK_SIZE)

    for i, chunk_text in enumerate(llm_chunks):
        print(f"   🔄 Чанк {i + 1}/{len(llm_chunks)}...")
        user_prompt = _build_user_prompt_chunk(
            chunk_text=chunk_text,
            chunk_summary_text=summary_text,
            chunk_idx=i,
            total_chunks=len(llm_chunks),
            meeting_name=aligned.meeting_name,
        )
        resp = _call_llm(client, cfg.llm_model, system_prompt, user_prompt, cfg.llm_max_tokens)
        chunk_drafts.append(resp.content)
        print(f"   ✓ Чанк {i + 1} готов ({resp.tokens_used} токенов)")

    # Reduce: объединение
    print(f"   🔗 Объединение {len(chunk_drafts)} черновиков...")
    final_prompt = _build_user_prompt_final(
        chunks_content=chunk_drafts,
        summary_text=summary_text,
        meeting_name=aligned.meeting_name,
    )
    resp = _call_llm(client, cfg.llm_model, system_prompt, final_prompt, cfg.llm_max_tokens)
    print(f"   ✓ Финальный конспект готов ({resp.tokens_used} токенов)")
    return resp.content


def _generate_notes_without_llm(aligned: AlignedData) -> str:
    """Генерирует структуру конспекта без LLM — как заглушка."""
    lines = [
        f"# {aligned.meeting_name}",
        "",
        f"> **In a nutshell:** (требуется LLM для генерации)",
        "",
        f"**Курс:** {aligned.course_name}",
        f"**Слайдов:** {aligned.total_slides}",
        "",
        "---",
        "",
        "## Оглавление",
        "",
    ]

    for c in aligned.chunks:
        preview = c.slide_text[:80].replace("\n", " ") if c.slide_text else "(пусто)"
        lines.append(f"### Слайд {c.slide_num} ({c.start_sec:.0f}с)")
        if c.slide_text:
            lines.append(c.slide_text)
        if c.transcript_text:
            lines.append(f"\n> Лектор: _{c.transcript_text[:200]}_")
        lines.append("")

    lines.extend([
        "---",
        "",
        "_Для полноценной генерации задайте LLM_API_KEY в .env файле._",
    ])

    return "\n".join(lines)
