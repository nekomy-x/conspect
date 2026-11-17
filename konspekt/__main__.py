"""Скрипт входа: python -m konspekt -h"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .config import cfg
from .secret import sanitize_log


class SecretFilter(logging.Filter):
    """Маскирует секреты в каждом лог-сообщении."""
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = sanitize_log(record.msg)
        if record.args:
            record.args = tuple(
                sanitize_log(a) if isinstance(a, str) else a
                for a in record.args
            )
        return True


def main() -> None:
    parser = argparse.ArgumentParser(prog="konspekt", description="Moodle→TG ассистент")
    parser.add_argument("command", nargs="?", default="run", choices=["run", "scan", "courses", "test-ai"])
    args = parser.parse_args()

    logging.basicConfig(
        level=cfg.log_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # глобальный фильтр секретов
    logging.getLogger().addFilter(SecretFilter())

    if args.command == "run":
        from .bot import run
        asyncio.run(run())
    elif args.command == "scan":
        from .scanner import scan_all
        from .db import db
        s = asyncio.run(scan_all())
        print(s)
        for c in db.courses():
            print(f"  [{c['id']}] {c['name']}")
    elif args.command == "courses":
        from .moodle import moodle
        courses = asyncio.run(moodle.fetch_courses())
        for c in courses:
            print(f"[{c['id']}] {c['name']}")
    elif args.command == "test-ai":
        async def t() -> None:
            from .ai.client import ai
            r = await ai.chat([{"role": "user", "content": "Скажи одним словом: работает?"}])
            print("AI:", r)
        asyncio.run(t())


if __name__ == "__main__":
    main()
