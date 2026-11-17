"""Планировщик: сканы Moodle, мониторинг лекций, дедлайны, расписание, рефреш сессии."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .config import cfg
from .db import db
from .scanner import scan_all, events, check_meeting_buttons
from .schedule_logic import daily_briefing, hourly_schedule_check, morning_wakeup

log = logging.getLogger("konspect.scheduler")
scheduler = AsyncIOScheduler(timezone=cfg.tz)


def start() -> None:
    from .db import db
    # глобальный режим мониторинга (кнопки в ТГ)
    if db.kv_get("monitoring_enabled") is None:
        db.kv_set("monitoring_enabled", True)
    scheduler.add_job(scan_job, "interval", seconds=cfg.scan_interval,
                      id="moodle_scan", next_run_time=datetime.now())
    # мониторинг лекций: каждые 120 сек (мягче для Moodle)
    scheduler.add_job(lecture_watch_job, "interval", seconds=120, id="lecture_watch")
    # дедлайны/напоминания
    scheduler.add_job(reminders_job, "interval", seconds=30, id="reminders")
    # расписание: утренняя сводка в 7:00, вечерняя «завтра» в 21:00
    scheduler.add_job(briefing_job, "cron", hour=7, minute=0, id="morning_brief")
    scheduler.add_job(evening_job, "cron", hour=21, minute=0, id="evening_brief")
    scheduler.add_job(hourly_schedule_check, "cron", minute=0, id="hourly_schedule")
    # сессию держим живой: раз в 20 минут
    scheduler.add_job(session_keepalive_job, "interval", minutes=20, id="keepalive")
    # слежка за произвольными URL (web-страницы пользователя)
    scheduler.add_job(url_watch_job, "interval", minutes=10, id="url_watch")
    scheduler.start()
    log.info("scheduler started: scan=%ss, lecture_watch=120s", cfg.scan_interval)


async def scan_job() -> None:
    from .db import db
    if not db.kv_get("monitoring_enabled", True):
        log.debug("мониторинг выключен — скип скана")
        return
    try:
        s = await scan_all()
        log.debug("scan: %s", s)
    except Exception as e:  # noqa: BLE001
        log.error("scan_job: %s", e)


async def lecture_watch_job() -> None:
    """Каждые 2 минуты (мягко для Moodle):
    - у лекций с временем начала: анонс за 15 мин, контроль кнопки −10…+40 мин;
    - лекции БЕЗ времени: редкая проверка (раз в 10 мин)."""
    from .db import db
    if not db.kv_get("monitoring_enabled", True):
        return
    now = int(time.time())
    checked = 0
    for m in db.meetings():
        opening = m.get("opening_ts") or 0
        course = next((c for c in db.courses() if c["id"] == m["course_id"]), None)
        cname = course["name"] if course else "курс"

        if opening:
            # анонс «вот-вот начнётся» за 15 мин — ТОЛЬКО если время из Moodle
            if (0 < opening - now <= 15 * 60 and not m.get("announced_upcoming")
                    and m.get("from_moodle")):
                db.set_meeting(m["activity_id"], announced_upcoming=1)
                if events.on_meeting_upcoming:
                    try:
                        await events.on_meeting_upcoming(m, cname)
                    except Exception as e:  # noqa: BLE001
                        log.warning("upcoming cb: %s", e)

            # активный монитор кнопки: за 10 мин до → 40 мин после начала
            if -40 * 60 <= opening - now <= 10 * 60:
                try:
                    await check_meeting_buttons(m["activity_id"], cname)
                    checked += 1
                except Exception as e:  # noqa: BLE001
                    log.debug("lecture watch %s: %s", m["activity_id"], e)
        else:
            # без времени начала — ОЧЕНЬ редкая проверка (раз в 10 мин),
            # максимум 3 встречи за прогон (защита от убивания сессии)
            last = m.get("last_check") or 0
            if (now - last >= 600 and not m.get("announced_start")
                    and checked < 3):
                try:
                    await check_meeting_buttons(m["activity_id"], cname)
                    checked += 1
                except Exception as e:  # noqa: BLE001
                    log.debug("lecture watch %s: %s", m["activity_id"], e)
    if checked:
        log.debug("lecture_watch: проверено %s встреч", checked)


async def reminders_job() -> None:
    now = int(time.time())
    for r in db.due_reminders(now):
        try:
            await _fire_reminder(r)
            db.mark_reminder(r["id"])
        except Exception as e:  # noqa: BLE001
            log.error("reminder %s: %s", r["id"], e)


async def _fire_reminder(r: dict) -> None:
    from .tg_notify import send_text
    if r["kind"] == "deadline" and events.on_deadline:
        cmid = int(r["payload"] or 0)
        asg = db.assignment(cmid)
        if asg:
            course = next((c for c in db.courses() if c["id"] == asg["course_id"]), None)
            await events.on_deadline(asg, course["name"] if course else "", r["text"])
            return
    # plain текст — отправляем как есть в чат напоминания
    await send_text(r["chat_id"] or cfg.owner_chat_id, r["text"])
    # повторяющееся — создаём следующее
    rep = db.kv_get(f"repeat:{r['id']}")
    if rep and rep.get("repeat"):
        import datetime as dt
        base = dt.datetime.fromtimestamp(r["due_ts"])
        mode = rep["repeat"]
        if mode == "daily":
            nxt = base + dt.timedelta(days=1)
        elif mode == "weekly":
            nxt = base + dt.timedelta(weeks=1)
        elif mode == "weekdays":
            nxt = base + dt.timedelta(days=1)
            while nxt.weekday() >= 5:   # сб/вс пропускаем
                nxt = nxt + dt.timedelta(days=1)
        else:
            return
        new_id = db.add_reminder(r["chat_id"] or cfg.owner_chat_id,
                                 int(nxt.timestamp()), r["text"], kind="plain")
        db.kv_set(f"repeat:{new_id}", {"repeat": mode, "text": r["text"],
                                       "base_ts": int(nxt.timestamp())})
        log.info("reminder %s повторится %s (%s)", r["id"], nxt, mode)


async def briefing_job() -> None:
    await daily_briefing()


async def evening_job() -> None:
    await morning_wakeup()


async def session_keepalive_job() -> None:
    from .moodle import moodle
    ok, reason = await moodle.is_alive()
    if not ok:
        # повторная проверка через 30 сек — защита от разового глюка Moodle
        import asyncio as _a
        await _a.sleep(30)
        ok2, reason2 = await moodle.is_alive()
        if not ok2:
            # СНАЧАЛА пробуем авто-логин, уведомляем только если не помог
            from .auth import ensure_session
            log.warning("СЕССИЯ MOODLE УМЕРЛА: %s — пробую авто-логин…", reason2)
            login_ok, login_msg = await ensure_session()
            if login_ok:
                log.info("авто-логин успешен, сессия восстановлена")
                return  # не спамим юзеру
            # авто-логин не помог — уведомляем (но не чаще раза в час)
            import time as _t
            last_notify = db.kv_get("last_auth_notify", 0)
            now = int(_t.time())
            if now - last_notify > 3600:
                db.kv_set("last_auth_notify", now)
                if events.on_auth_lost:
                    try:
                        await events.on_auth_lost()
                    except Exception as e:  # noqa: BLE001
                        log.warning("keepalive auth cb: %s", e)
            else:
                log.info("авто-логин не помог, но уведомление уже было <1ч назад")
        else:
            log.info("сессия жива после повторной проверки (разовый глюк)")


async def url_watch_job() -> None:
    """Слежка за произвольными URL: изменилась страница → уведомление."""
    from .tools import check_url_watches
    from .tg_notify import send_text
    from .config import cfg
    try:
        changes = await check_url_watches()
        for w in changes:
            note = f" ({w['note']})" if w.get("note") else ""
            await send_text(cfg.owner_chat_id,
                            f"👁 <b>Страница изменилась</b>{note}\n{w['url']}")
    except Exception as e:  # noqa: BLE001
        log.debug("url watch: %s", e)


def stop() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
