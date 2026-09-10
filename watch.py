#!/usr/bin/env python3
"""
Watcher: следит за queue.txt и обрабатывает новые ссылки на лекции.

Использование:
  python watch.py              # демон: проверяет очередь каждые N секунд
  python watch.py --once       # обработать текущие ссылки и выйти
  python watch.py --interval 60
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

from config import load_config, ensure_dirs
from main import process_lecture


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="bbb-lectures-watch",
        description="Watcher для автоматической обработки лекций из queue.txt",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Обработать имеющиеся ссылки и выйти",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=60,
        help="Интервал проверки очереди в секундах (по умолчанию 60)",
    )
    return parser.parse_args()


def get_new_urls(cfg) -> list[str]:
    """Возвращает новые необработанные ссылки из queue.txt."""
    queue_file = cfg.queue_file
    if not queue_file.exists():
        return []

    from main import load_processed_ids
    processed = load_processed_ids(cfg)

    urls = []
    for line in queue_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line not in processed:
            urls.append(line)
    return urls


def mark_processed(queue_file: Path, url: str) -> None:
    """Помечает URL как обработанный в queue.txt (добавляет #)."""
    content = queue_file.read_text(encoding="utf-8")
    if url in content:
        content = content.replace(url, f"# {url}")
        queue_file.write_text(content, encoding="utf-8")


def run(once: bool = False, interval: int = 60) -> None:
    cfg = load_config()
    ensure_dirs(cfg)

    print("👁  Watcher запущен.")
    print(f"   Очередь: {cfg.queue_file}")
    print(f"   Обработанные: {cfg.processed_file}")
    print(f"   Режим: {'одноразовый (--once)' if once else f'демон (интервал {interval} сек)'}")
    print()

    while True:
        urls = get_new_urls(cfg)

        if not urls:
            if once:
                print("✅ Очередь пуста. Готово.")
                return
            print(f"⏳ Новых ссылок нет. Следующая проверка через {interval} сек...")
            time.sleep(interval)
            continue

        for url in urls[:5]:
            print(f"\n{'=' * 60}")
            print(f"🔗 Обработка: {url}")
            print(f"{'=' * 60}")

            try:
                result = process_lecture(url, cfg)
                if result:
                    mark_processed(cfg.queue_file, url)
                    print(f"✅ Обработано: {result}")
                else:
                    print("⚠  Лекция не обработана (пропущена или ошибка)")
            except Exception as e:
                import traceback
                print(f"❌ Ошибка при обработке: {e}")
                traceback.print_exc()
                # Помечаем как обработанную, чтобы не зациклиться на битой ссылке
                mark_processed(cfg.queue_file, url)

        if once:
            print("✅ Все ссылки обработаны.")
            return


def main():
    args = parse_args()
    run(once=args.once, interval=args.interval)


if __name__ == "__main__":
    main()
