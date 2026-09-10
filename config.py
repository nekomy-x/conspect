"""Конфигурация пайплайна: загрузка .env и пути к файлам."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Загружаем .env из корня проекта (и из текущей директории)
PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")
load_dotenv()  # также пробуем системный .env


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _get_bool(name: str, default: bool = False) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Config:
    # --- LLM (OpenAI-совместимый API) ---
    llm_api_key: str = field(default_factory=lambda: _get("LLM_API_KEY"))
    llm_base_url: str = field(default_factory=lambda: _get("LLM_BASE_URL", "https://api.openai.com/v1"))
    llm_model: str = field(default_factory=lambda: _get("LLM_MODEL", "gpt-4o"))
    llm_max_tokens: int = field(default_factory=lambda: int(_get("LLM_MAX_TOKENS", "8192")))

    # --- Whisper ---
    whisper_model: str = field(default_factory=lambda: _get("WHISPER_MODEL", "medium"))
    whisper_language: str = field(default_factory=lambda: _get("WHISPER_LANGUAGE", "ru"))

    # --- Moodle (опциональная авторизация для edu.vsu.ru) ---
    moodle_cookies: str = field(default_factory=lambda: _get("MOODLE_COOKIES"))

    # --- Пути ---
    vault_dir: Path = field(default_factory=lambda: Path(_get("VAULT_DIR", "vault")))
    docs_dir: Path = field(default_factory=lambda: Path(_get("DOCS_DIR", "docs")))
    tmp_dir: Path = field(default_factory=lambda: Path(_get("TMP_DIR", "tmp")))
    site_dir: Path = field(default_factory=lambda: Path(_get("SITE_DIR", "site")))
    queue_file: Path = field(default_factory=lambda: Path(_get("QUEUE_FILE", "queue.txt")))
    processed_file: Path = field(default_factory=lambda: Path(_get("PROCESSED_FILE", "processed.json")))

    # --- Поведение ---
    build_site: bool = field(default_factory=lambda: _get_bool("BUILD_SITE", True))
    download_video: bool = field(default_factory=lambda: _get_bool("DOWNLOAD_VIDEO", True))

    # --- Курсы ВГУ (для тегов конспектов) ---
    course_name: str = field(default_factory=lambda: _get("COURSE_NAME", "Разработка и дизайн ИС"))


def load_config() -> Config:
    cfg = Config()
    if not cfg.llm_api_key:
        print("⚠️  ВНИМАНИЕ: LLM_API_KEY не задан в .env — генерация конспекта будет пропущена.")
        print("    Скопируйте .env.example в .env и вставьте ключ.")
    return cfg


def ensure_dirs(cfg: Config) -> None:
    """Создаёт все нужные директории."""
    for d in (cfg.vault_dir, cfg.docs_dir / "конспекты", cfg.docs_dir / "assets",
              cfg.tmp_dir, cfg.site_dir):
        d.mkdir(parents=True, exist_ok=True)