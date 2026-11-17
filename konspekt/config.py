"""Конфигурация приложения (env + sqlite)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


@dataclass
class Config:
    # telegram
    bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    owner_chat_id: int = _int("OWNER_CHAT_ID", 0)
    allowed_chat_ids: list[int] = field(default_factory=list)

    # moodle
    moodle_base: str = os.getenv("MOODLE_BASE_URL", "https://edu.vsu.ru").rstrip("/")
    moodle_session_cookie: str = os.getenv("MOODLE_SESSION_COOKIE", "")
    moodle_username: str = os.getenv("MOODLE_USERNAME", "")
    moodle_password: str = os.getenv("MOODLE_PASSWORD", "")

    # openai
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    openai_base_url: str = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-5.4")
    openai_vision_model: str = os.getenv("OPENAI_VISION_MODEL", "gpt-5.4")

    # app
    data_dir: Path = BASE_DIR / os.getenv("DATA_DIR", "data")
    tz: str = os.getenv("TZ", "Europe/Moscow")
    scan_interval: int = _int("SCAN_INTERVAL", 300)
    log_level: str = os.getenv("LOG_LEVEL", "INFO")

    # образ жизни: сон, засыпание (минут), путь до универа (минут)
    sleep_hours: float = float(os.getenv("SLEEP_HOURS", "8") or 8)
    fall_asleep_minutes: int = _int("FALL_ASLEEP_MINUTES", 30)
    commute_minutes: int = _int("COMMUTE_MINUTES", 120)

    # url bbb-сервера записей (вычисляется из moodle_base по умолчанию)
    @property
    def bbb_base(self) -> str:
        return os.getenv("BBB_BASE_URL", "https://bbb.edu.vsu.ru").rstrip("/")

    # URL веб-сервера конспектов (сайт с анимированными страницами)
    @property
    def web_url(self) -> str:
        return os.getenv("WEB_URL", "http://localhost:8080").rstrip("/")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "konspekt.db"

    def allowed_ids(self) -> list[int]:
        ids = set()
        if self.owner_chat_id:
            ids.add(self.owner_chat_id)
        raw = os.getenv("ALLOWED_CHAT_IDS", "")
        for part in raw.replace(";", ",").split(","):
            part = part.strip()
            if part.lstrip("-").isdigit():
                ids.add(int(part))
        return sorted(ids)

    def kv_owner(self, chat_id: int) -> None:
        """Сохраняет владельца, если он не был задан в env."""
        if not self.owner_chat_id:
            self.owner_chat_id = chat_id
            os.environ["OWNER_CHAT_ID"] = str(chat_id)
    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "media").mkdir(exist_ok=True)
        (self.data_dir / "conspects").mkdir(exist_ok=True)
        (self.data_dir / "assignments").mkdir(exist_ok=True)


cfg = Config()
cfg.ensure_dirs()


def _restore_owner() -> None:
    """Владелец из БД, если env пуст — агент/planner слали в 0 без этого."""
    if not cfg.owner_chat_id:
        try:
            from .db import db
            v = db.kv_get("owner_chat_id")
            if v:
                cfg.owner_chat_id = int(v)
        except Exception:  # noqa: BLE001
            pass


_restore_owner()
