"""Live-мониторинг BBB-лекции: пинг присутствия пока лекция идёт.

Пока лекция идёт (running=true), каждые 5 минут делает GET join URL —
эмуляция активности (пинг присутствия). Когда лекция заканчивается —
монитор останавливается.

Конспект с лайва невозможен (presentation_text.json создаётся только
после публикации записи). Голосование требует полный WS-клиент BBB.
"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx

from .config import cfg
from .db import db

log = logging.getLogger("konspect.live")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

PING_INTERVAL = 300  # 5 минут — пинг присутствия

# активные задачи мониторинга
_active_monitors: dict[int, asyncio.Task] = {}


async def get_join_url(cmid: int, bbb_id: int) -> str | None:
    """Получаем join URL с sessionToken."""
    cookie = cfg.moodle_session_cookie
    if not cookie:
        return None
    moodle_url = (f"{cfg.moodle_base}/mod/bigbluebuttonbn/bbb_view.php"
                  f"?action=join&id={cmid}&bn={bbb_id}")
    try:
        async with httpx.AsyncClient(
            timeout=30, headers={"User-Agent": UA}, follow_redirects=True
        ) as client:
            r = await client.get(moodle_url, cookies={"MoodleSession": cookie})
            if r.status_code == 200:
                return str(r.url)
    except Exception as e:  # noqa: BLE001
        log.warning("get_join_url: %s", e)
    return None


async def ping_presence(join_url: str) -> bool:
    """GET join URL — эмуляция активности (пинг)."""
    try:
        async with httpx.AsyncClient(
            timeout=15, headers={"User-Agent": UA}, follow_redirects=True
        ) as client:
            r = await client.get(join_url)
            return r.status_code == 200
    except Exception as e:  # noqa: BLE001
        log.warning("ping: %s", e)
        return False


async def live_monitor_loop(cmid: int, bbb_id: int, lecture_name: str) -> None:
    """Цикл мониторинга: пинг пока лекция идёт, стоп когда закончилась."""
    from .moodle import moodle

    log.info("live monitor started: %s (cmid=%s)", lecture_name[:50], cmid)
    last_ping = 0

    try:
        while True:
            now = int(time.time())

            # проверяем идёт ли ещё лекция
            try:
                info = await moodle.fetch_bbb(cmid)
                mi = info.get("meeting_info") or {}
                running = bool(mi.get("statusrunning"))
                canjoin = bool(mi.get("canjoin"))
            except Exception:  # noqa: BLE001
                running = False
                canjoin = False

            if not running and not canjoin:
                log.info("live monitor stopped: лекция закончилась (%s)",
                         lecture_name[:40])
                db.set_meeting(cmid, live_monitor=0)
                break

            # пинг присутствия
            if now - last_ping >= PING_INTERVAL:
                join_url = await get_join_url(cmid, bbb_id)
                if join_url:
                    ok = await ping_presence(join_url)
                    log.info("ping %s: %s", lecture_name[:30],
                             "ok" if ok else "fail")
                last_ping = now

            await asyncio.sleep(60)  # проверяем раз в минуту
    except asyncio.CancelledError:
        log.info("live monitor cancelled: %s", lecture_name[:40])
    finally:
        _active_monitors.pop(cmid, None)
        db.set_meeting(cmid, live_monitor=0)


def start_live_monitor(cmid: int, bbb_id: int, lecture_name: str) -> None:
    """Запускает фоновый мониторинг живой лекции (если ещё не запущен)."""
    if cmid in _active_monitors and not _active_monitors[cmid].done():
        return  # уже запущен
    task = asyncio.create_task(live_monitor_loop(cmid, bbb_id, lecture_name))
    _active_monitors[cmid] = task
    db.set_meeting(cmid, live_monitor=1)
    log.info("live monitor task created: %s", lecture_name[:50])


def stop_all_monitors() -> None:
    """Останавливает все активные мониторы (при выключении бота)."""
    for cmid, task in list(_active_monitors.items()):
        task.cancel()
    _active_monitors.clear()
