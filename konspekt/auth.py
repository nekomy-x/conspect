"""Авто-логин в Moodle (edu.vsu.ru) по логину/паролю.

Работает через стандартную форму Moodle 4.x (logintoken CSRF).
При успехе сохраняет MoodleSession cookie в .env — дальше вся система
работает на куке как раньше.

Пароль читается ТОЛЬКО из konspekt.secret, в логах маскируется.
"""
from __future__ import annotations

import logging
import re

import httpx

from .config import cfg
from .secret import get_credentials, has_credentials, mask_secret, sanitize_log

log = logging.getLogger("konspect.auth")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


async def auto_login() -> tuple[bool, str]:
    """Логинится в Moodle. Возвращает (успех, сообщение).

    Успех: True + "ok" (cookie сохранена в .env).
    Ошибка: False + причина (пароль маскируется).
    """
    if not has_credentials():
        return False, "нет логина/пароля в .env (MOODLE_USERNAME, MOODLE_PASSWORD)"

    username, password = get_credentials()
    base = cfg.moodle_base

    try:
        async with httpx.AsyncClient(
            timeout=30, headers={"User-Agent": UA}, follow_redirects=False
        ) as client:
            # 1. GET форма логина → logintoken
            r = await client.get(f"{base}/login/index.php")
            if r.status_code != 200:
                return False, f"форма логина недоступна (HTTP {r.status_code})"

            m = re.search(r'name="logintoken"\s+value="([^"]+)"', r.text)
            if not m:
                # может быть hidden input в другом порядке
                m = re.search(r'value="([^"]+)"[^>]*name="logintoken"', r.text)
            if not m:
                return False, "logintoken не найден — возможно SSO-вход"

            logintoken = m.group(1)

            # 2. POST логин
            r = await client.post(
                f"{base}/login/index.php",
                data={
                    "username": username,
                    "password": password,
                    "logintoken": logintoken,
                    "anchor": "",
                },
            )

            # 3. Проверяем результат
            # Moodle может редиректить на testsession (проверка сессии) — это ОК
            if r.status_code in (301, 302, 303):
                loc = r.headers.get("location", "")
                if "testsession" in loc:
                    # тестовая сессия — следуем за редиректом
                    r = await client.get(loc)
                    loc = r.headers.get("location", "")
                if "login" in loc and "testsession" not in loc:
                    # редирект на login БЕЗ testsession = ошибка
                    return False, "неверный логин или пароль"
                # редирект не на login (или testsession прошёл) — успех
            elif r.status_code == 200:
                if "name=\"password\"" in r.text and "loginbtn" in r.text:
                    return False, "неверный логин или пароль"
            else:
                return False, f"HTTP {r.status_code}"

            # 4. Достаём MoodleSession cookie
            session_cookie = client.cookies.get("MoodleSession")
            if not session_cookie:
                # cookie может быть в Set-Cookie заголовке
                for h in r.headers.get_list("set-cookie"):
                    m2 = re.search(r"MoodleSession=([^;]+)", h)
                    if m2:
                        session_cookie = m2.group(1)
                        break

            if not session_cookie:
                return False, "сессия не получена (cookie не найдена)"

            # 5. Сохраняем в .env
            from .secret import write_env_value
            write_env_value("MOODLE_SESSION_COOKIE", session_cookie)
            cfg.moodle_session_cookie = session_cookie

            log.info("авто-логин успешен: %s (cookie %s…)",
                     username, mask_secret(session_cookie))
            return True, "ok"

    except httpx.TimeoutException:
        return False, "таймаут при логине"
    except Exception as e:  # noqa: BLE001
        return False, sanitize_log(str(e))


async def ensure_session() -> tuple[bool, str]:
    """Проверяет текущую сессию; если мертва — авто-логин.

    Вызывается из keepalive и при MoodleAuthError.
    """
    from .moodle import moodle

    ok, _ = await moodle.is_alive()
    if ok:
        return True, "сессия жива"

    if not has_credentials():
        return False, "сессия умерла, авто-логин невозможен (нет пароля)"

    log.info("сессия умерла — пробую авто-логин…")
    success, msg = await auto_login()
    if success:
        # обновляем клиент
        await moodle.set_session_cookie(cfg.moodle_session_cookie)
        return True, "авто-логин успешен"
    return False, msg
