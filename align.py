"""Сопоставление транскрипта лектора с таймлайном слайдов."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from extract import SlideTimeline
from transcribe import TranscriptSegment


@dataclass
class SlideChunk:
    """Один слайд с привязанным транскриптом — блок для генерации конспекта."""
    slide_num: int
    start_sec: float
    end_sec: float
    slide_text: str                     # текст с самого слайда (из textfiles)
    transcript_text: str = ""           # что говорил лектор на этот слайд
    transcript_segments: list[TranscriptSegment] = field(default_factory=list)
    png_path: Optional[Path] = None
    svg_path: Optional[Path] = None
    duration_sec: float = 0

    @property
    def duration_min(self) -> float:
        return self.duration_sec / 60


@dataclass
class AlignedData:
    """Полные данные после сопоставления."""
    record_id: str
    chunks: list[SlideChunk]
    full_transcript: str                # весь транскрипт текстом (для контекста LLM)
    meeting_name: str = ""
    course_name: str = ""

    @property
    def total_slides(self) -> int:
        return len(self.chunks)

    @property
    def slides_with_content(self) -> int:
        return sum(1 for c in self.chunks if c.slide_text.strip() or c.transcript_text.strip())

    @property
    def slides_with_transcript(self) -> int:
        return sum(1 for c in self.chunks if c.transcript_text.strip())


def align(
    timeline: list[SlideTimeline],
    segments: list[TranscriptSegment],
    record_id: str = "",
    meeting_name: str = "",
    course_name: str = "",
) -> AlignedData:
    """
    Привязывает сегменты транскрипта к слайдам по таймкодам.

    Алгоритм: для каждого сегмента транскрипта находим активный слайд
    (start <= seg.start < end) и добавляем текст к нему.
    Сегменты без активного слайда (до первого / после последнего) идут
    в ближайший по времени слайд.
    """
    print(f"\n📐 Сопоставление транскрипта со слайдами...")

    if not timeline:
        print("   ⚠ Таймлайн пуст — нет слайдов")
        return AlignedData(record_id=record_id, chunks=[], full_transcript="")

    # Создаём placeholders для каждого слайда
    chunks: list[SlideChunk] = [
        SlideChunk(
            slide_num=s.slide_num,
            start_sec=s.start_sec,
            end_sec=s.end_sec,
            slide_text=s.slide_text,
            png_path=s.png_file,
            svg_path=s.svg_file,
            duration_sec=s.end_sec - s.start_sec,
        )
        for s in timeline
    ]

    # Привязываем сегменты транскрипта к слайдам
    for seg in segments:
        chunk = _find_active_chunk(chunks, seg.start)
        if chunk:
            chunk.transcript_segments.append(seg)
            chunk.transcript_text += seg.text + " "

    # Форматируем транскрипты
    for chunk in chunks:
        chunk.transcript_text = chunk.transcript_text.strip()

    # Собираем полный транскрипт
    full_transcript = "\n".join(
        f"[{int(s.start // 60):02d}:{int(s.start % 60):02d}] {s.text}"
        for seg_list in (c.transcript_segments for c in chunks)
        for s in seg_list
    )

    stats_with_slide = sum(1 for c in chunks if c.slide_text.strip())
    stats_with_trans = sum(1 for c in chunks if c.transcript_text.strip())
    print(f"   ✓ Сопоставлено:")
    print(f"     Всего слайдов: {len(chunks)}")
    print(f"     С текстом слайда: {stats_with_slide}")
    print(f"     С речью лектора: {stats_with_trans}")

    return AlignedData(
        record_id=record_id,
        chunks=chunks,
        full_transcript=full_transcript,
        meeting_name=meeting_name,
        course_name=course_name,
    )


def _find_active_chunk(chunks: list[SlideChunk], timestamp: float) -> SlideChunk:
    """Находит слайд, активный в данный момент."""
    # Бинарный поиск, потом fallback на ближайший
    lo, hi = 0, len(chunks) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if timestamp < chunks[mid].start_sec:
            hi = mid - 1
        elif timestamp >= chunks[mid].end_sec:
            lo = mid + 1
        else:
            return chunks[mid]

    # Если не нашли — привязываем к ближайшему
    if lo == 0:
        return chunks[0]
    if lo >= len(chunks):
        return chunks[-1]
    # Ближайший из двух
    return chunks[lo] if abs(chunks[lo].start_sec - timestamp) < abs(chunks[lo - 1].start_sec - timestamp) else chunks[lo - 1]


# ── Форматирование для LLM-промпта ─────────────────────────────────────────

def format_chunk_for_llm(chunk: SlideChunk, slide_num_global: int, total: int) -> str:
    """Форматирует один слайд для вставки в LLM-промпт."""
    lines = []
    t_start = f"{int(chunk.start_sec // 60):02d}:{int(chunk.start_sec % 60):02d}"
    t_end = f"{int(chunk.end_sec // 60):02d}:{int(chunk.end_sec % 60):02d}"

    lines.append(f"=== СЛАЙД {chunk.slide_num} ({t_start}–{t_end}, {chunk.duration_min:.1f} мин) [{slide_num_global}/{total}] ===")
    if chunk.png_path:
        lines.append(f"[Файл слайда: {chunk.png_path.name}]")
    if chunk.slide_text:
        lines.append(f"ТЕКСТ СЛАЙДА:")
        lines.append(chunk.slide_text)
    if chunk.transcript_text:
        lines.append(f"\nРЕЧЬ ЛЕКТОРА:")
        lines.append(chunk.transcript_text)
    else:
        lines.append(f"\n(Лектор ничего не говорил на этом слайде)")
    lines.append("")
    return "\n".join(lines)


def format_aligned_for_llm(data: AlignedData, max_slides: int = 12) -> list[str]:
    """
    Разбивает aligned data на чанки по max_slides слайдов для map-reduce.
    Возвращает список строк, каждая — часть лекции для LLM.
    """
    chunks = data.chunks
    if not chunks:
        return []

    result = []
    for i in range(0, len(chunks), max_slides):
        batch = chunks[i:i + max_slides]
        batch_text = []
        for j, chunk in enumerate(batch):
            batch_text.append(format_chunk_for_llm(chunk, i + j + 1, len(chunks)))
        result.append("\n".join(batch_text))

    return result


def format_aligned_summary(data: AlignedData) -> str:
    """Краткая сводка (оглавление) для LLM-промпта — чтобы он знал всю лекцию целиком."""
    lines = [f"Лекция: {data.meeting_name}"]
    if data.course_name:
        lines.append(f"Курс: {data.course_name}")
    lines.append(f"Всего слайдов: {data.total_slides}")
    lines.append(f"С речью лектора: {data.slides_with_transcript}")
    lines.append("")

    lines.append("ОГЛАВЛЕНИЕ:")
    for c in data.chunks:
        t_start = f"{int(c.start_sec // 60):02d}:{int(c.start_sec % 60):02d}"
        preview = c.slide_text[:60].replace("\n", " ") if c.slide_text else "(пустой)"
        has_speech = "🎤" if c.transcript_text else "—"
        lines.append(f"  Слайд {c.slide_num}: {t_start} {has_speech} | {preview}")

    return "\n".join(lines)
