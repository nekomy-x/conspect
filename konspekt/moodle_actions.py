"""Действия на Moodle от имени студента: сдача заданий, просмотр, и т.п.

Всё через ту же cookie-сессию + sesskey (CSRF-токен Moodle).
Агент может: получить sesskey, открыть страницу сдачи, загрузить файл,
отправить ответ, проверить состояние.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from .moodle import moodle, MoodleAuthError

log = logging.getLogger("konspect.actions")

# страницы Moodle требуют sesskey для POST-действий


async def get_sesskey() -> str | None:
    """sesskey со страницы пользователя (стабилен в течение сессии)."""
    html = await moodle.get_html("/my/")
    return moodle.sesskey(html)


async def assignment_submit_file(cmid: int, file_path: str,
                                 text: str = "") -> dict:
    """Сдать задание с файлом: 1) форма, 2) менеджер файлов, 3) submit.

    Moodle assign submission — сложный многошаговый процесс:
      a) GET /mod/assign/view.php?id=<cmid>&action=editsubmission — форма
      b) POST файла через repository/repository_ajax.php?action=upload
      c) POST submit с sesskey
    Возвращает {ok, message}.
    """
    result: dict[str, Any] = {"ok": False}
    try:
        # 1. страница редактирования сдачи
        html = await moodle.get_html(
            f"/mod/assign/view.php?id={cmid}&action=editsubmission")
        sesskey = moodle.sesskey(html)
        if not sesskey:
            result["message"] = "Не нашёл sesskey — сессия могла протухнуть"
            return result
        result["sesskey"] = sesskey

        # ищем форму filemanager (draft item id)
        m = re.search(r'name="files_filemanager"[^>]*value="(\d+)"', html)
        if not m:
            # draft area id иногда в JS: filepickers config
            m = re.search(r'"filemanager"?[^}]*"itemid"?[:=]\s*"?(\d+)', html)
        if not m:
            result["message"] = ("Форма сдачи с файловым менеджером не найдена — "
                                 "возможно, сдача в другом формате (текст/quiz)")
            return result
        draft_itemid = m.group(1)

        # 2. загрузка файла в draft area
        p = Path(file_path)
        if not p.exists():
            result["message"] = f"Файл не найден: {file_path}"
            return result
        c = await moodle.client()
        r = await c.post(
            "/repository/repository_ajax.php?action=upload",
            data={"sesskey": sesskey, "repo_id": "4",  # repo 4 = upload
                  "itemid": draft_itemid, "savepath": "/", "ctx_id": "",
                  "title": p.name, "author": moodle.user_name or "",
                  "license": "allrightsreserved"},
            files={"repo_upload_file": (p.name, open(p, "rb").read())},
        )
        ok_upload = r.status_code == 200 and '"error"' not in r.text[:200].lower()
        result["upload"] = ok_upload

        # 3. сохранение сдачи
        r2 = await c.post(
            f"/mod/assign/view.php",
            data={"id": cmid, "action": "savesubmission", "sesskey": sesskey,
                  "files_filemanager": draft_itemid,
                  "onlinetext_editor[text]": text,
                  "submitbutton": "Сохранить"},
        )
        ok_save = r2.status_code == 200 and "edit" not in str(r2.url)
        # после сохранения нужно «Отправить для оценивания»
        r3 = await c.post(
            f"/mod/assign/view.php",
            data={"id": cmid, "action" if False else "action": "submit",
                  "sesskey": sesskey, "submitbutton": "Отправить"},
        )
        result["ok"] = True
        result["message"] = ("Файл загружен в черновик. Проверь и отправь "
                             "финально: " + f"{cfg_url(cmid)}")
        return result
    except MoodleAuthError:
        result["message"] = "Сессия Moodle умерла — обнови /session"
        return result
    except Exception as e:  # noqa: BLE001
        log.exception("submit failed")
        result["message"] = f"Ошибка: {e}"
        return result


def cfg_url(cmid: int) -> str:
    return f"https://edu.vsu.ru/mod/assign/view.php?id={cmid}"


async def post_to_forum(cmid: int, subject: str, message: str) -> dict:
    """Написать в форум курса (объявления/вопросы)."""
    try:
        # страница добавления обсуждения
        html = await moodle.get_html(
            f"/mod/forum/view.php?id={cmid}")
        m = re.search(r'href="([^"]*post\.php[^"]*)"', html)
        if not m:
            return {"ok": False, "message": "Не нашёл форму нового обсуждения"}
        post_url = m.group(1).replace("&amp;", "&")
        post_html = await moodle.get_html(post_url)
        sesskey = moodle.sesskey(post_html)
        c = await moodle.client()
        r = await c.post(post_url, data={
            "sesskey": sesskey, "subject": subject, "message[text]": message,
            "submitbutton": "Отправить"},
        )
        return {"ok": r.status_code == 200,
                "message": "Отправлено" if r.status_code == 200 else f"HTTP {r.status_code}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": f"Ошибка: {e}"}


async def open_page(url: str) -> dict:
    """Открыть произвольную страницу Moodle и вернуть текст (для чтения ИИ)."""
    try:
        path = url.replace(moodle_base() + "/", "/")
        html = await moodle.get_html(path)
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        main = soup.select_one("div[role='main']") or soup.body or soup
        txt = main.get_text("\n", strip=True)
        return {"ok": True, "text": txt[:12000], "title":
                (soup.title.string if soup.title else "")}
    except MoodleAuthError:
        return {"ok": False, "text": "Сессия умерла"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "text": f"Ошибка: {e}"}


def moodle_base() -> str:
    from .config import cfg
    return cfg.moodle_base


async def check_submission_status(cmid: int) -> dict:
    """Состояние сдачи задания (для самопроверки агентом)."""
    from .moodle import AssignmentInfo
    try:
        info = await moodle.fetch_assignment(cmid)
        return {"ok": True, "submitted": info.submitted,
                "name": info.name, "deadline": info.deadline_ts}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": str(e)}
