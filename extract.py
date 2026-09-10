"""Извлечение данных: аудио (ffmpeg), слайды (SVG→PNG), таймлайны, текст."""
from __future__ import annotations

import json
import re
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from html import unescape

# ── Минимальная длительность слайда (сек) ───────────────────────────────────
MIN_SLIDE_DURATION = 2.0  # Слайды короче 2 сек — промежуточные переходы


@dataclass
class SlideTimeline:
    """Один слайд в таймлайне."""
    slide_num: int
    start_sec: float
    end_sec: float
    text_file: Optional[Path] = None
    svg_file: Optional[Path] = None
    png_file: Optional[Path] = None
    slide_text: str = ""


@dataclass
class ExtractedData:
    """Все извлечённые данные из BBB-записи."""
    record_id: str
    audio_path: Optional[Path]
    timeline: list[SlideTimeline]
    slides_dir: Path          # папка с PNG-слайдами
    slides_text_dir: Path     # папка с текстами слайдов


# ── Аудио ────────────────────────────────────────────────────────────────────

def extract_audio(video_path: Path, out_dir: Path) -> Path:
    """Извлекает аудио из видеофайла (16kHz, mono WAV для Whisper)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    audio_path = out_dir / "audio.wav"
    if audio_path.exists() and audio_path.stat().st_size > 0:
        print(f"   ♫ Аудио уже извлечено: {audio_path.name}")
        return audio_path

    print(f"   ♫ Извлечение аудио из {video_path.name}...")
    cmd = [
        "ffmpeg", "-y", "-i", str(video_path),
        "-vn",              # без видео
        "-ar", "16000",     # 16kHz (оптимально для Whisper)
        "-ac", "1",         # моно
        "-acodec", "pcm_s16le",  # 16-bit PCM
        "-f", "wav",
        str(audio_path),
    ]
    subprocess.run(cmd, capture_output=True, check=True)

    size_mb = audio_path.stat().st_size / (1024 * 1024)
    print(f"   ✓ Аудио: {size_mb:.1f} МБ → {audio_path.name}")
    return audio_path


# ── Слайды (SVG → PNG) ──────────────────────────────────────────────────────

def convert_svg_to_png(svg_path: Path, out_path: Path, width: int = 1280) -> None:
    """Конвертирует SVG в PNG через rsvg-convert."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "rsvg-convert",
        "--width", str(width),
        str(svg_path),
        "--output", str(out_path),
    ]
    subprocess.run(cmd, capture_output=True, check=True)


def convert_slides(slide_svgs: list[Path], out_dir: Path) -> list[Path]:
    """Конвертирует все SVG-слайды в PNG. Возвращает список PNG-путей."""
    out_dir.mkdir(parents=True, exist_ok=True)
    png_paths = []

    for svg in slide_svgs:
        # slide1.svg → slide_001.png
        stem = svg.stem
        png_name = re.sub(r"slide(\d+)", lambda m: f"slide_{int(m.group(1)):03d}", stem) + ".png"
        png_path = out_dir / png_name

        if not png_path.exists() or png_path.stat().st_size == 0:
            try:
                convert_svg_to_png(svg, png_path)
            except subprocess.CalledProcessError:
                print(f"   ⚠ Не удалось конвертировать {svg.name}, пропускаю")
                continue

        png_paths.append(png_path)

    print(f"   🖼  Слайды PNG: {len(png_paths)} файлов")
    return png_paths


# ── Таймлайн слайдов ────────────────────────────────────────────────────────

def parse_timeline(shapes_svg_path: Path) -> list[tuple[float, int, str]]:
    """
    Парсит shapes.svg и возвращает таймлайн: [(start_sec, slide_num, href)].
    Берём только image-элементы со class="slide".
    """
    text = shapes_svg_path.read_text(encoding="utf-8")
    entries = []

    # Regex: <image ... in="секунды" ... href="...slide<N>.svg" ...>
    # или variations
    RE_IMG = re.compile(
        r'<image[^>]*(?:class="slide"|class=\'slide\')[^>]*'
        r'in="([\d.]+)"[^>]*href="([^"]*slide(\d+)\.svg)"',
    )
    RE_IMG_ALT = re.compile(
        r'<image[^>]*href="([^"]*slide(\d+)\.svg)"[^>]*'
        r'(?:class="slide"|class=\'slide\')[^>]*in="([\d.]+)"',
    )

    for m in RE_IMG.finditer(text):
        t = float(m.group(1))
        href = m.group(2)
        num = int(m.group(3))
        entries.append((t, num, href))

    for m in RE_IMG_ALT.finditer(text):
        href = m.group(1)
        num = int(m.group(2))
        t = float(m.group(3))
        entries.append((t, num, href))

    # Fallback: XML parser
    if not entries:
        try:
            ns_svg = "http://www.w3.org/2000/svg"
            ns_xlink = "http://www.w3.org/1999/xlink"
            root = ET.fromstring(text.encode() if isinstance(text, str) else text)
            for img in root.iter(f"{{{ns_svg}}}image"):
                href = (img.get(f"{{{ns_xlink}}}href") or img.get("href") or
                        img.get(f"{{{ns_xlink}}}href"))
                in_val = img.get("in") or img.get("data-in")
                if not href or not in_val:
                    continue
                m = re.search(r"slide(\d+)\.svg", href)
                if m:
                    entries.append((float(in_val), int(m.group(1)), href))
        except ET.ParseError:
            pass

    entries.sort(key=lambda x: (x[0], x[1]))
    return entries


def build_timeline(
    entries: list[tuple[float, int, str]],
    textfiles: list[Path],
    slide_svgs: list[Path],
    png_paths: list[Path],
    total_duration: float = 0,
) -> list[SlideTimeline]:
    """
    Строит финальный таймлайн из entries: убирает кратковременные переходы,
    вычисляет end_sec для каждого слайда, привязывает файлы.
    """
    if not entries:
        return []

    # Убираем дубликаты (один и тот же slide_num с близкими таймкодами)
    deduped: list[tuple[float, int, str]] = []
    for t, num, href in sorted(entries):
        if deduped and deduped[-1][1] == num and abs(t - deduped[-1][0]) < MIN_SLIDE_DURATION:
            continue
        deduped.append((t, num, href))

    # Вычисляем end_sec
    timeline = []
    for i, (t, num, href) in enumerate(deduped):
        end = deduped[i + 1][0] if i + 1 < len(deduped) else (total_duration if total_duration > 0 else t + 300)

        # Пути к файлам
        text_path = None
        svg_path = None
        png_path = None

        for tf in textfiles:
            if tf.name == f"slide-{num}.txt":
                text_path = tf
                break

        for sv in slide_svgs:
            if sv.name == f"slide{num}.svg":
                svg_path = sv
                break

        for p in png_paths:
            if f"slide_{num:03d}" in p.name:
                png_path = p
                break

        # Читаем текст
        slide_text = ""
        if text_path and text_path.exists():
            slide_text = text_path.read_text(encoding="utf-8").strip()

        timeline.append(SlideTimeline(
            slide_num=num,
            start_sec=t,
            end_sec=end,
            text_file=text_path,
            svg_file=svg_path,
            png_file=png_path,
            slide_text=slide_text,
        ))

    print(f"   ⏱  Таймлайн: {len(timeline)} слайдов (после фильтрации переходов)")
    return timeline


# ── Сохранение/загрузка таймлайна ───────────────────────────────────────────

def save_timeline(timeline: list[SlideTimeline], out_path: Path) -> None:
    """Сохраняет таймлайн в JSON."""
    data = []
    for s in timeline:
        data.append({
            "slide_num": s.slide_num,
            "start_sec": s.start_sec,
            "end_sec": s.end_sec,
            "text_file": str(s.text_file) if s.text_file else None,
            "svg_file": str(s.svg_file) if s.svg_file else None,
            "png_file": str(s.png_file) if s.png_file else None,
            "slide_text": s.slide_text,
        })
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_timeline(json_path: Path) -> list[SlideTimeline]:
    """Загружает таймлайн из JSON."""
    data = json.loads(json_path.read_text(encoding="utf-8"))
    return [
        SlideTimeline(
            slide_num=d["slide_num"],
            start_sec=d["start_sec"],
            end_sec=d["end_sec"],
            text_file=Path(d["text_file"]) if d.get("text_file") else None,
            svg_file=Path(d["svg_file"]) if d.get("svg_file") else None,
            png_file=Path(d["png_file"]) if d.get("png_file") else None,
            slide_text=d.get("slide_text", ""),
        )
        for d in data
    ]


# ── Основная функция ────────────────────────────────────────────────────────

def extract_all(
    record_id: str,
    base_dir: Path,
    slide_svgs: list[Path],
    textfiles: list[Path],
    video_path: Optional[Path],
    duration_sec: float = 0,
) -> ExtractedData:
    """Полное извлечение: аудио, слайды PNG, таймлайн."""
    print(f"\n🔧 Извлечение данных для {record_id[:16]}...")

    # Директории
    audio_dir = base_dir / "audio"
    slides_png_dir = base_dir / "slides_png"
    slides_png_dir.mkdir(parents=True, exist_ok=True)

    # 1) Аудио
    audio_path = None
    if video_path and video_path.exists():
        audio_path = extract_audio(video_path, audio_dir)
    else:
        print("   ⚠ Видео не найдено — пропускаю извлечение аудио")

    # 2) Слайды SVG → PNG
    png_paths = convert_slides(slide_svgs, slides_png_dir) if slide_svgs else []

    # 3) Таймлайн
    entries = parse_timeline(base_dir / "shapes.svg")
    timeline = build_timeline(entries, textfiles, slide_svgs, png_paths, duration_sec)

    # Сохраняем таймлайн
    save_timeline(timeline, base_dir / "timeline.json")

    return ExtractedData(
        record_id=record_id,
        audio_path=audio_path,
        timeline=timeline,
        slides_dir=slides_png_dir,
        slides_text_dir=base_dir / "text",
    )
