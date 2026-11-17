"""Секретный модуль: учётные данные Moodle.

Пароль хранится в .env (MOODLE_USERNAME / MOODLE_PASSWORD).
Этот модуль — единственное место, где пароль читается.
В логах и контексте модели пароль НИКОГДА не появляется.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from .config import cfg

_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"

# поля, которые считаются секретными и маскируются везде
SECRET_KEYS = {"MOODLE_PASSWORD", "TELEGRAM_BOT_TOKEN", "OPENAI_API_KEY"}


def get_credentials() -> tuple[str, str]:
    """Возвращает (username, password) из .env. Пустые строки если нет."""
    return cfg.moodle_username.strip(), cfg.moodle_password.strip()


def has_credentials() -> bool:
    u, p = get_credentials()
    return bool(u and p)


def mask_secret(value: str) -> str:
    """Маскирует секрет для логов: первые 2 + *** + последние 2."""
    if not value or len(value) < 5:
        return "***"
    return value[:2] + "***" + value[-2:]


def sanitize_log(text: str) -> str:
    """Убирает секреты из строки перед логированием."""
    for key in SECRET_KEYS:
        val = os.getenv(key, "")
        if val and len(val) > 3:
            text = text.replace(val, mask_secret(val))
    # также маскируем MoodleSession cookie если встретится
    text = re.sub(r"MoodleSession=[A-Za-z0-9]+", "MoodleSession=***", text)
    return text


def write_env_value(key: str, value: str) -> None:
    """Обновляет значение в .env (для смены пароля и т.п.)."""
    lines = []
    found = False
    if _ENV_PATH.exists():
        for line in _ENV_PATH.read_text().splitlines():
            if line.startswith(f"{key}="):
                lines.append(f"{key}={value}")
                found = True
            else:
                lines.append(line)
    if not found:
        lines.append(f"{key}={value}")
    _ENV_PATH.write_text("\n".join(lines) + "\n")
    os.environ[key] = value
    # обновляем cfg
    if key == "MOODLE_USERNAME":
        cfg.moodle_username = value
    elif key == "MOODLE_PASSWORD":
        cfg.moodle_password = value
