"""Транскрипция аудио через mlx-whisper (Apple Silicon) или faster-whisper (CPU)."""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional


@dataclass
class TranscriptSegment:
    start: float    # секунды
    end: float
    text: str


def transcribe(
    audio_path: Path,
    model_name: str = "medium",
    language: str = "ru",
) -> list[TranscriptSegment]:
    """
    Транскрибирует аудиофайл. Пробует mlx-whisper, потом faster-whisper.
    Возвращает список сегментов с таймкодами.
    """
    print(f"\n🎙  Транскрипция ({model_name}, {language})...")
    print(f"   Файл: {audio_path.name} ({audio_path.stat().st_size / 1024 / 1024:.1f} МБ)")

    # Пробуем mlx-whisper (Apple Silicon)
    try:
        return _transcribe_mlx(audio_path, model_name, language)
    except (ImportError, Exception) as e:
        print(f"   ⚠ mlx-whisper недоступен ({e}), пробую faster-whisper...")

    # Фолбэк: faster-whisper (CPU)
    try:
        return _transcribe_faster(audio_path, model_name, language)
    except ImportError as e:
        raise RuntimeError(
            "Не установлена ни одна Whisper-библиотека.\n"
            "Установите: pip install mlx-whisper  (для Apple Silicon)\n"
            "       или: pip install faster-whisper  (для CPU)\n"
            f"Ошибка: {e}"
        )


def _transcribe_mlx(audio_path: Path, model_name: str, language: str) -> list[TranscriptSegment]:
    """Транскрипция через mlx-whisper (GPU на Apple Silicon)."""
    import mlx_whisper

    print("   🚀 mlx-whisper (Apple Silicon GPU)")
    result = mlx_whisper.transcribe(
        str(audio_path),
        path_or_hf_repo=f"mlx-community/whisper-{model_name}-mlx",
        language=language,
        word_timestamps=True,
        verbose=False,
    )

    segments = []
    for seg in result.get("segments", []):
        segments.append(TranscriptSegment(
            start=seg["start"],
            end=seg["end"],
            text=seg["text"].strip(),
        ))

    print(f"   ✓ mlx-whisper: {len(segments)} сегментов, {result.get('text', '')[:80]}...")
    return segments


def _transcribe_faster(audio_path: Path, model_name: str, language: str) -> list[TranscriptSegment]:
    """Транскрипция через faster-whisper (CPU)."""
    from faster_whisper import WhisperModel

    print("   💻 faster-whisper (CPU)")
    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    segments_iter, info = model.transcribe(
        str(audio_path),
        language=language,
        word_timestamps=True,
        beam_size=5,
        vad_filter=True,
    )

    segments = []
    for seg in segments_iter:
        segments.append(TranscriptSegment(
            start=seg.start,
            end=seg.end,
            text=seg.text.strip(),
        ))

    print(f"   ✓ faster-whisper: {len(segments)} сегментов, lang={info.language} ({info.language_probability:.0%})")
    return segments


# ── Сохранение / загрузка ───────────────────────────────────────────────────

def save_transcript(segments: list[TranscriptSegment], out_path: Path) -> None:
    """Сохраняет транскрипт в JSON."""
    data = [asdict(s) for s in segments]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"   💾 Транскрипт: {out_path.name} ({len(segments)} сегментов)")


def load_transcript(json_path: Path) -> list[TranscriptSegment]:
    """Загружает транскрипт из JSON."""
    data = json.loads(json_path.read_text(encoding="utf-8"))
    return [TranscriptSegment(**d) for d in data]


def format_transcript_full(segments: list[TranscriptSegment]) -> str:
    """Форматирует транскрипт в читаемый текст (для LLM-промпта)."""
    lines = []
    for s in segments:
        t_min = int(s.start // 60)
        t_sec = int(s.start % 60)
        lines.append(f"[{t_min:02d}:{t_sec:02d}] {s.text}")
    return "\n".join(lines)
