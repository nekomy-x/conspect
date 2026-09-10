#!/usr/bin/env python3
"""
bbb-lectures: Пайплайн «запись BBB → красивый конспект».

Использование:
  python main.py <url_or_file> [опции]
  python main.py --build-site
  python main.py --watch
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from config import Config, load_config, ensure_dirs


# ── Получение длительности аудио ─────────────────────────────────────────────

def get_audio_duration(video_path: Path) -> float:
    """Возвращает длительность аудио/видео в секундах через ffprobe."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries",
             "format=duration", "-of", "csv=p=0", str(video_path)],
            capture_output=True, text=True, timeout=30,
        )
        return float(result.stdout.strip())
    except Exception:
        return 0.0


# ── Обработка одной лекции ──────────────────────────────────────────────────

def process_lecture(
    source: str,
    cfg: Config,
    title: Optional[str] = None,
    skip_video: bool = False,
    build_site: bool = True,
) -> Optional[Path]:
    """
    Полный пайплайн: URL/файл → конспект.

    Args:
        source: BBB URL или путь к видеофайлу
        cfg: Конфигурация
        title: Название лекции (для локального файла)
        skip_video: Пропустить скачивание видео
        build_site: Собрать MkDocs-сайт после генерации

    Returns:
        Путь к Markdown-конспекту или None при ошибке.
    """
    from fetch import extract_record_id, fetch_recording, FetchedRecording
    from extract import extract_all, ExtractedData, load_timeline
    from transcribe import transcribe, save_transcript, load_transcript
    from align import align
    from summarize import generate_notes
    from notes import save_notes

    start_time = time.time()

    # Определяем: это URL или файл?
    is_local_file = Path(source).is_file()

    if is_local_file:
        print(f"\n📂 Локальный файл: {source}")
        # Для локального файла создаём mock-структуру
        video_path = Path(source)
        if not video_path.exists():
            print(f"❌ Файл не найден: {source}")
            return None
        record_id = video_path.stem[:40]
        base_dir = cfg.tmp_dir / record_id
        base_dir.mkdir(parents=True, exist_ok=True)
        duration = get_audio_duration(video_path)
        # Копируем видео в tmp для единообразия
        target_video = base_dir / "video" / "webcams.webm"
        target_video.parent.mkdir(parents=True, exist_ok=True)
        if not target_video.exists():
            shutil.copy2(video_path, target_video)

        # Создаём metadata-объект
        from fetch import RecordingMeta
        meta = RecordingMeta(record_id=record_id, meeting_name=title or video_path.stem)
        meta.duration_ms = int(duration * 1000)
        fetched = FetchedRecording(
            record_id=record_id,
            base_dir=base_dir,
            meta=meta,
            slides=[],
            metadata_xml=None,  # type: ignore
            shapes_svg=None,  # type: ignore
            textfiles=[],
            slide_svgs=[],
            video_path=target_video,
            deskshare_path=None,
        )
    else:
        # URL → скачиваем ассеты
        try:
            record_id = extract_record_id(source)
        except ValueError as e:
            print(f"❌ {e}")
            return None

        # Проверяем, не обрабатывали ли уже
        if record_id in load_processed_ids(cfg):
            print(f"ℹ  Лекция {record_id[:16]} уже обработана. Пропускаю (--force для повторной обработки).")
            return None

        # Скачиваем
        fetched = fetch_recording(
            record_id=record_id,
            tmp_dir=cfg.tmp_dir,
            download_video=cfg.download_video and not skip_video,
            cookies=_get_cookies(cfg),
        )
        meta = fetched.meta
        duration = meta.duration_ms / 1000 if meta.duration_ms else 0

    # Извлекаем аудио и слайды
    extracted = extract_all(
        record_id=fetched.record_id,
        base_dir=fetched.base_dir,
        slide_svgs=fetched.slide_svgs,
        textfiles=fetched.textfiles,
        video_path=fetched.video_path,
        duration_sec=duration,
    )

    # Транскрибируем (если есть аудио)
    segments = []
    transcript_path = fetched.base_dir / "transcript.json"
    if extracted.audio_path and extracted.audio_path.exists():
        if transcript_path.exists() and transcript_path.stat().st_size > 0:
            print(f"   📥 Загружаю кешированный транскрипт: {transcript_path.name}")
            segments = load_transcript(transcript_path)
        else:
            segments = transcribe(
                extracted.audio_path,
                model_name=cfg.whisper_model,
                language=cfg.whisper_language,
            )
            save_transcript(segments, transcript_path)
    else:
        print("   ⚠ Аудио не извлечено — транскрипция пропущена")

    # Сопоставляем транскрипт со слайдами
    aligned = align(
        timeline=extracted.timeline,
        segments=segments,
        record_id=fetched.record_id,
        meeting_name=meta.meeting_name,
        course_name=meta.course_name,
    )

    # Генерируем конспект через LLM
    llm_notes = generate_notes(aligned, cfg)

    # Сохраняем
    paths = save_notes(aligned, llm_notes, cfg)

    # Отмечаем как обработанную
    add_processed_id(cfg, fetched.record_id, meta.meeting_name)

    # Собираем сайт
    if build_site and cfg.build_site:
        from website import build_index_and_site
        build_index_and_site(cfg)

    elapsed = time.time() - start_time
    print(f"\n✅ Готово! {aligned.total_slides} слайдов за {elapsed:.0f} сек")
    print(f"   📄 Конспект: {paths['vault']}")
    if cfg.build_site:
        print(f"   🌐 Сайт: python -m http.server -d {cfg.site_dir} 8000")

    return paths["vault"]


# ── Обработка очереди ───────────────────────────────────────────────────────

def process_queue(cfg: Config, once: bool = False) -> None:
    """Обрабатывает ссылки из queue.txt."""
    queue_file = cfg.queue_file
    if not queue_file.exists():
        print(f"⚠ Файл очереди не найден: {queue_file}")
        print(f"  Создайте файл и добавьте ссылки, по одной на строку.")
        return

    processed_ids = load_processed_ids(cfg)
    while True:
        lines = queue_file.read_text(encoding="utf-8").splitlines()
        new_urls = [
            line.strip() for line in lines
            if line.strip() and not line.startswith("#") and line.strip() not in processed_ids
        ]

        if not new_urls:
            if once:
                print("✅ Очередь пуста, все ссылки обработаны.")
                return
            if not once:
                print(f"⏳ Новых ссылок нет. Проверю через 60 сек...")
                time.sleep(60)
                continue

        for url in new_urls[:10]:  # За раз — максимум 10
            print(f"\n{'='*60}")
            print(f"🔗 Обработка: {url}")
            print(f"{'='*60}")
            try:
                result = process_lecture(url, cfg)
                if result:
                    # Помечаем в queue.txt как обработанную
                    mark_queue_url_processed(queue_file, url)
                    processed_ids.add(url)
            except Exception as e:
                print(f"❌ Ошибка: {e}")
                import traceback
                traceback.print_exc()

        if once:
            return


def mark_queue_url_processed(queue_file: Path, url: str) -> None:
    """Добавляет # к обработанной ссылке в queue.txt."""
    content = queue_file.read_text(encoding="utf-8")
    content = content.replace(url, f"# {url}")
    queue_file.write_text(content, encoding="utf-8")


# ── processed.json ───────────────────────────────────────────────────────────

def load_processed_ids(cfg: Config) -> set[str]:
    """Загружает список обработанных recordId."""
    if cfg.processed_file.exists():
        try:
            data = json.loads(cfg.processed_file.read_text(encoding="utf-8"))
            return set(data.keys()) if isinstance(data, dict) else set(data)
        except Exception:
            return set()
    return set()


def add_processed_id(cfg: Config, record_id: str, meeting_name: str = "") -> None:
    """Добавляет recordId в processed.json."""
    data = {}
    if cfg.processed_file.exists():
        try:
            data = json.loads(cfg.processed_file.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data[record_id] = {"name": meeting_name, "processed_at": time.time()}
    cfg.processed_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _get_cookies(cfg: Config) -> Optional[dict]:
    """Загружает cookies для Moodle если настроены."""
    if not cfg.moodle_cookies:
        return None
    try:
        from fetch import _load_cookies
        return _load_cookies(cfg.moodle_cookies)
    except Exception:
        return None


# ── CLI ──────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="bbb-lectures",
        description="Пайплайн «запись BBB → красивый конспект»",
    )
    parser.add_argument(
        "source",
        nargs="?",
        help="BBB URL или путь к видеофайлу",
    )
    parser.add_argument(
        "--title",
        help="Название лекции (для локальных видеофайлов)",
    )
    parser.add_argument(
        "--fetch-only",
        action="store_true",
        help="Только скачать ассеты (без обработки)",
    )
    parser.add_argument(
        "--build-site",
        action="store_true",
        help="Собрать MkDocs-сайт из готовых конспектов",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Запустить watcher (обработка очереди queue.txt)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Для --watch: обработать и выйти",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Обработать даже если уже обработано",
    )
    parser.add_argument(
        "--no-site",
        action="store_true",
        help="Не собирать сайт после обработки",
    )
    parser.add_argument(
        "--model",
        help="Модель Whisper (override для WHISPER_MODEL)",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    cfg = load_config()

    if args.model:
        cfg = Config(**{**cfg.__dict__, "whisper_model": args.model})

    ensure_dirs(cfg)

    # --build-site без source: собираем из готовых конспектов
    if args.build_site and not args.source:
        from website import build_index_and_site
        build_index_and_site(cfg)
        return

    # --watch: обработка очереди
    if args.watch:
        process_queue(cfg, once=args.once)
        return

    # Нужен source
    if not args.source:
        print("❌ Укажите URL или путь к видеофайлу.")
        print("   Пример: python main.py https://bbb.edu.vsu.ru/playback/...")
        sys.exit(1)

    # --force: убираем из processed
    if args.force:
        try:
            from fetch import extract_record_id
            rid = extract_record_id(args.source)
            data = {}
            if cfg.processed_file.exists():
                try:
                    data = json.loads(cfg.processed_file.read_text(encoding="utf-8"))
                except Exception:
                    data = {}
            data.pop(rid, None)
            cfg.processed_file.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8",
            )
            print(f"ℹ  {rid[:16]} убран из processed.json (--force)")
        except Exception:
            pass

    if args.fetch_only:
        from fetch import extract_record_id, fetch_recording
        rid = extract_record_id(args.source)
        fetch_recording(rid, cfg.tmp_dir, download_video=cfg.download_video, cookies=_get_cookies(cfg))
    else:
        process_lecture(
            args.source,
            cfg,
            title=args.title,
            build_site=not args.no_site and cfg.build_site,
        )


if __name__ == "__main__":
    main()
