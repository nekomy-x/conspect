"""Генерация статического сайта из конспектов через MkDocs Material."""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from config import Config

MK_DOCS_YML_TEMPLATE = """site_name: Конспекты лекций
site_description: Конспекты университетских лекций из записей BigBlueButton
site_url: ""

theme:
  name: material
  language: ru
  palette:
    - media: "(prefers-color-scheme: light)"
      scheme: default
      primary: indigo
      accent: indigo
      toggle:
        icon: material/weather-night
        name: Тёмная тема
    - media: "(prefers-color-scheme: dark)"
      scheme: slate
      primary: indigo
      accent: indigo
      toggle:
        icon: material/weather-sunny
        name: Светлая тема
  features:
    - navigation.instant
    - navigation.tracking
    - navigation.top
    - navigation.footer
    - search.suggest
    - search.highlight
    - content.code.copy
    - content.tabs.link
  icon:
    logo: material/book-open-page-variant
    repo: fontawesome/brands/github

plugins:
  - search:
      lang:
        - ru
        - en

markdown_extensions:
  - admonition
  - pymdownx.details
  - pymdownx.superfences:
      custom_fences:
        - name: mermaid
          class: mermaid
          format: !!python/name:pymdownx.superfences.fence_code_format
  - pymdownx.tabbed:
      alternate_style: true
  - pymdownx.highlight:
      anchor_linenums: true
  - pymdownx.inlinehilite
  - pymdownx.snippets
  - pymdownx.superfences
  - pymdownx.critic
  - pymdownx.caret
  - pymdownx.keys
  - pymdownx.mark
  - pymdownx.tilde
  - tables
  - footnotes
  - attr_list
  - md_in_html
  - toc:
      permalink: true
      toc_depth: 3

extra:
  generator: false

copyright: Сгенерировано автоматически
"""


def write_mkdocs_config(docs_dir: Path, cfg: Config) -> Path:
    """Пишет mkdocs.yml в корень проекта (nav MkDocs строит сам из docs/)."""
    cfg_path = Path(__file__).parent / "mkdocs.yml"
    cfg_path.write_text(MK_DOCS_YML_TEMPLATE, encoding="utf-8")
    print(f"   ⚙️  mkdocs.yml: {cfg_path}")
    return cfg_path


def build_site(docs_dir: Path, site_dir: Path, mkdocs_yml: Path) -> bool:
    """Запускает mkdocs build. Возвращает True при успехе."""
    print("\n🌐 Сборка статического сайта (MkDocs Material)...")

    # Проверяем, установлен ли mkdocs
    try:
        import mkdocs  # noqa: F401
    except ImportError:
        print("   ⚠ mkdocs не установлен. Установите: pip install mkdocs-material")
        print("     Сайт не собран, но Markdown-конспекты готовы в docs/ и vault/")
        return False

    # mkdocs build запускается из корня проекта (там mkdocs.yml)
    root = Path(__file__).parent
    cmd = [sys.executable, "-m", "mkdocs", "build", "-f", str(mkdocs_yml), "-d", str(site_dir)]

    try:
        result = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=300)
        if result.returncode == 0:
            print(f"   ✓ Сайт собран: {site_dir}")
            print(f"     Открыть: cd {root} && python -m http.server -d site 8000")
            return True
        else:
            print(f"   ⚠ Ошибка сборки: {result.stderr[-500:]}")
            return False
    except Exception as e:
        print(f"   ⚠ Ошибка сборки: {e}")
        return False


def build_index_and_site(cfg: Config) -> bool:
    """Полная генерация: обновляет индекс и собирает сайт."""
    from notes import update_docs_index

    # Обновляем индекс (список лекций)
    update_docs_index(cfg.docs_dir)

    # Пишем mkdocs.yml
    mkdocs_yml = write_mkdocs_config(cfg.docs_dir, cfg)

    # Собираем сайт
    return build_site(cfg.docs_dir, cfg.site_dir, mkdocs_yml)
