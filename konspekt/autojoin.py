"""Авто-присоединение к BBB-конференциям (эмуляция посещения).

Когда лекция начинается (canjoin=true), бот автоматически "заходит"
в конференцию через sessionToken — это отмечает посещение в Moodle.

Поток:
1. GET Moodle bbb_view.php?action=join&id=X&bn=Y (с кукой)
2. → редирект на bbbN.edu.vsu.ru/html5client/join?sessionToken=Z
3. GET BBB URL → 200 OK = присоединился

Одна попытка на лекцию, не спамим.
"""
from __future__ import annotations

import logging
import re

import httpx

from .config import cfg
from .db import db

log = logging.getLogger("konspect.autojoin")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


async def auto_join_lecture(cmid: int, bbb_id: int, lecture_name: str) -> tuple[bool, str]:
    """Автоматически присоединяется к BBB-конференции.

    Возвращает (успех, сообщение).
    Одна попытка на лекцию — проверяем флаг joined в БД.
    """
    # проверяем уже присоединялись?
    meeting = db.meeting(cmid)
    if meeting and meeting.get("joined"):
        return True, "уже присоединялись"

    cookie = cfg.moodle_session_cookie
    if not cookie:
        return False, "нет сессии Moodle"

    moodle_url = (f"{cfg.moodle_base}/mod/bigbluebuttonbn/bbb_view.php"
                  f"?action=join&id={cmid}&bn={bbb_id}")

    try:
        async with httpx.AsyncClient(
            timeout=30, headers={"User-Agent": UA}, follow_redirects=True
        ) as client:
            # Шаг 1: Moodle join → редирект на BBB с sessionToken
            r = await client.get(moodle_url, cookies={"MoodleSession": cookie})

            if r.status_code != 200:
                return False, f"Moodle join: HTTP {r.status_code}"

            # ищем sessionToken в финальном URL
            final_url = str(r.url)
            m = re.search(r"sessionToken=([A-Za-z0-9]+)", final_url)
            if not m:
                # может быть в теле страницы
                m = re.search(r"sessionToken=([A-Za-z0-9]+)", r.text)
            if not m:
                return False, "sessionToken не найден"

            token = m.group(1)
            bbb_url = final_url if "bbb" in final_url else None

            if not bbb_url:
                # извлекаем BBB URL из текста
                m2 = re.search(r"(https?://bbb[^\"'\s]+)", r.text)
                if m2:
                    bbb_url = m2.group(1)
                else:
                    return False, "BBB URL не найден"

            # Шаг 2: GET BBB URL → присоединяемся
            r2 = await client.get(bbb_url)

            if r2.status_code == 200:
                # отмечаем как присоединённых
                db.set_meeting(cmid, joined=1)
                log.info("авто-присоединение: %s (cmid=%s)", lecture_name[:50], cmid)
                return True, "присоединились"
            else:
                return False, f"BBB join: HTTP {r2.status_code}"

    except httpx.TimeoutException:
        return False, "таймаут при присоединении"
    except Exception as e:  # noqa: BLE001
        return False, str(e)[:100]
