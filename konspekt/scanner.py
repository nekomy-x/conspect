"""Сканеры Moodle: курсы, активности, записи BBB, задания, уведомления."""
from __future__ import annotations

import asyncio
import logging
import re
import time

from .config import cfg
from .db import db
from .moodle import moodle, MoodleAuthError, parse_ru_datetime

log = logging.getLogger("konspect.scanner")

# какие модули отслеживаем и как зовём в ТГ
MOD_LABELS = {
    "bigbluebuttonbn": "📺 Лекция (видеоконференция)",
    "assign": "📋 Задание/лаба",
    "resource": "📄 Материал",
    "folder": "🗂 Папка",
    "forum": "💬 Форум",
    "quiz": "🧪 Тест",
    "attendance": "✅ Посещаемость",
}

NOTIFY_MODS = {"bigbluebuttonbn", "assign", "resource", "quiz", "folder", "forum", "attendance"}


class ScannerEvents:
    """Колбэки для бота (инъекция из main)."""

    def __init__(self) -> None:
        self.on_new_course = None            # async (course: dict)
        self.on_new_activity = None          # async (activity: dict)
        self.on_new_recording = None         # async (rec: dict, course_name)
        self.auto_conspect = True            # новые записи конспектятся автоматически
        self.on_auth_lost = None             # async ()
        self.on_meeting_upcoming = None      # async (meeting, course_name)
        self.on_meeting_started = None       # async (meeting, course_name)
        self.on_deadline = None              # async (assignment, course_name, stage)


events = ScannerEvents()


async def scan_all() -> dict:
    """Полный проход: дашборд → курсы → активности → BBB/assign детали.
    Следит только за курсами из watchlist (по умолчанию все)."""
    summary = {"courses": 0, "new_courses": 0, "activities": 0,
               "new_activities": 0, "new_recordings": 0}
    now = int(time.time())
    try:
        courses = await moodle.fetch_courses()
    except MoodleAuthError:
        await _auth_lost()
        return summary
    except Exception as e:  # noqa: BLE001
        log.error("fetch courses failed: %s", e)
        return summary

    summary["courses"] = len(courses)
    for c in courses:
        is_new = db.upsert_course(c["id"], c["name"], c["url"], now)
        if is_new:
            summary["new_courses"] += 1
            log.info("НОВЫЙ КУРС: %s (%s)", c["name"], c["id"])
            if events.on_new_course:
                try:
                    await events.on_new_course(c)
                except Exception as e:  # noqa: BLE001
                    log.warning("on_new_course cb: %s", e)

    for c in courses:
        if not db.is_watched(c["id"]):
            log.debug("курс %s вне watchlist — пропускаю", c["name"])
            continue
        try:
            acts = await moodle.fetch_activities(c["id"])
        except Exception as e:  # noqa: BLE001
            log.error("activities %s failed: %s", c["id"], e)
            continue
        summary["activities"] += len(acts)
        now2 = int(time.time())
        # ВАЖНО: считаем stale ДО upsert (upsert обновляет last_seen каждый скан)
        stale_map = {}
        for a in acts:
            known = db.activity(a.cmid)
            last_seen = (known or {}).get("last_seen") or 0
            stale_map[a.cmid] = (now2 - last_seen >= 1800)  # 30 мин
        for a in acts:
            is_new = db.upsert_activity(a.cmid, a.course_id, a.modname, a.name,
                                        a.url, a.section, now)
            if is_new and a.modname in NOTIFY_MODS:
                summary["new_activities"] += 1
                payload = {
                    "cmid": a.cmid, "course_id": a.course_id, "modname": a.modname,
                    "name": a.name, "url": a.url, "section": a.section,
                    "label": MOD_LABELS.get(a.modname, a.modname),
                    "course_name": c["name"],
                }
                log.info("НОВАЯ АКТИВНОСТЬ: [%s] %s / %s", a.modname, c["name"], a.name)
                if events.on_new_activity:
                    try:
                        await events.on_new_activity(payload)
                    except Exception as e:  # noqa: BLE001
                        log.warning("on_new_activity cb: %s", e)
            # детали: НОВЫЕ активности всегда; старые BBB — только раз в 30 мин
            # (защита сессии от перегруза: 297 BBB × meeting_info убивают Moodle)
            try:
                stale_ok = stale_map.get(a.cmid, True)
                if a.modname == "assign":
                    if is_new or stale_ok:
                        await _scan_assign(a, c["name"])
                elif a.modname == "bigbluebuttonbn":
                    if is_new or stale_ok:
                        await _scan_bbb(a, c["name"])
            except MoodleAuthError:
                await _auth_lost()
                return summary
            except Exception as e:  # noqa: BLE001
                log.error("detail %s %s failed: %s", a.modname, a.cmid, e)
            # пауза между активностями — не долбим Moodle
            await asyncio.sleep(0.3)

    # подчищаем мёртвые встречи (старше 8 часов)
    try:
        await _cleanup_meetings(now)
    except Exception:  # noqa: BLE001
        pass
    return summary


async def _scan_bbb(a, course_name: str) -> None:
    info = await moodle.fetch_bbb(a.cmid)
    now = int(time.time())

    # записи
    for rec in info.get("recordings", []):
        is_new = db.add_recording(rec["playback_url"], a.cmid, a.course_id,
                                  rec["title"], now)
        if is_new:
            log.info("НОВАЯ ЗАПИСЬ: %s / %s", course_name, rec["title"])
            if events.on_new_recording:
                await events.on_new_recording(
                    {"playback_url": rec["playback_url"], "title": rec["title"],
                     "activity_id": a.cmid, "course_id": a.course_id,
                     "name": a.name, "url": a.url},
                    course_name,
                )

    # время начала: Moodle openingtime — часто шаблон семестра (первая лекция),
    # не реальное время пары. Если оно старше 3 дней или его нет — берём время
    # ближайшей пары из расписания по совпадению названия курса.
    mi = info.get("meeting_info") or {}
    opening_ts = info.get("opening_ts")
    now = int(time.time())
    # из Moodle openingtime — надёжное время, доверяем
    from_moodle = bool(opening_ts and now - 3 * 86400 < opening_ts < now + 60 * 86400)
    if not from_moodle:
        opening_ts = None
    if not opening_ts and info.get("opening_text"):
        ts = parse_ru_datetime(info["opening_text"])
        if ts and now - 86400 < ts < now + 60 * 86400:
            opening_ts = ts
            from_moodle = True
    if not opening_ts:
        # из расписания — ВРЕМЯ ПАРЫ, но не факт что это ТВОЯ онлайн-лекция
        # (может быть другая подгруппа). Анонс только для онлайн-пар.
        opening_ts = _next_lesson_ts(course_name, now, online_only=True)
        from_moodle = False
    join_url = info.get("join_url")
    canjoin = bool(mi.get("canjoin"))
    running = bool(mi.get("statusrunning"))

    # мониторим встречу если: есть время начала ИЛИ (можно войти/идёт)
    if opening_ts or canjoin or running:
        db.upsert_meeting(
            a.cmid, course_id=a.course_id, name=a.name, url=a.url,
            opening_ts=opening_ts or 0, join_url=join_url,
            from_moodle=1 if from_moodle else 0,
        )
        await check_meeting_buttons(a.cmid, course_name)


def _next_lesson_ts(course_name: str, now: int, online_only: bool = True) -> int:
    """Ближайшая пара из расписания, чей предмет похож на курс Moodle.

    online_only=True: только ДИСТАНТ-пары — очная пара не означает, что
    эта Moodle-лекция твоя (подгруппы!), анонсить её нельзя."""
    name_words = [w for w in re.split(r"[^а-яёa-z]+", course_name.lower()) if len(w) > 4]
    if not name_words:
        return 0
    best = 0
    import datetime as dt
    for r in db.schedule_upcoming(dt.datetime.now().strftime("%Y-%m-%d")):
        if online_only and r.get("kind") != "online":
            continue
        title = (r.get("title") or "").lower()
        # совпадение слов курса с предметом (или наоборот)
        match = any(w in title for w in name_words) or any(
            w in course_name.lower() for w in
            [t for t in re.split(r"[^а-яёa-z]+", title) if len(t) > 4])
        if not match:
            continue
        try:
            start = dt.datetime.strptime(f"{r['day']} {r['time_start']}", "%Y-%m-%d %H:%M")
            ts = int(start.timestamp())
        except Exception:  # noqa: BLE001
            continue
        if ts > now - 45 * 60 and (best == 0 or ts < best):
            best = ts
    return best


async def check_meeting_buttons(activity_id: int, course_name: str) -> dict:
    """Проверка состояния конференции: можно войти / идёт — через AJAX.
    Уведомляем ТОЛЬКО если лекция совпадает с расписанием пользователя."""
    m = db.meeting(activity_id)
    if not m:
        return {}
    info = await moodle.fetch_bbb(activity_id)
    mi = info.get("meeting_info") or {}
    join = info.get("join_url") or mi.get("joinurl")
    canjoin = bool(mi.get("canjoin"))
    running = bool(mi.get("statusrunning"))

    if not (join and (canjoin or running)):
        return info

    # конференция открыта — уведомляем один раз, но ТОЛЬКО если это твоя лекция
    if not m.get("announced_start"):
        # проверяем совпадение с расписанием
        if not _is_lesson_in_schedule(course_name, m.get("name", "")):
            log.info("конференция открылась, но НЕ в расписании — пропускаю: %s",
                     m.get("name", "")[:50])
            db.set_meeting(activity_id, announced_start=1)  # не спамим повторно
            return info
        db.set_meeting(activity_id, announced_start=1, join_url=join,
                       started=1 if running else 0)
        if events.on_meeting_started:
            await events.on_meeting_started(
                {**m, "join_url": join, "running": running}, course_name)

        # АВТО-ПРИСОЕДИНЕНИЕ: отмечаем посещение
        from .autojoin import auto_join_lecture
        ok, msg = await auto_join_lecture(activity_id, info.get("bbb_id", 0),
                                          m.get("name", ""))
        if ok:
            log.info("авто-присоединение: %s", msg)
            # запускаем live-мониторинг (пинг пока лекция идёт)
            from .live import start_live_monitor
            start_live_monitor(activity_id, info.get("bbb_id", 0),
                               m.get("name", ""))
        else:
            log.warning("авто-присоединение не удалось: %s", msg)
    elif not m.get("join_url"):
        db.set_meeting(activity_id, join_url=join)
    return info


def _is_lesson_in_schedule(course_name: str, activity_name: str) -> bool:
    """Проверяет, есть ли сегодня ИЛИ завтра в расписании пара, совпадающая
    с курсом/активностью. Возвращает True если совпадение найдено."""
    from datetime import datetime, timedelta
    today = datetime.now().strftime("%Y-%m-%d")
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

    # слова из названия курса и активности
    course_words = set(w for w in re.split(r"[^а-яёa-z0-9]+", course_name.lower()) if len(w) > 3)
    act_words = set(w for w in re.split(r"[^а-яёa-z0-9]+", activity_name.lower()) if len(w) > 3)

    for day in (today, tomorrow):
        rows, _ = db.schedule_day_parity(day)
        if not rows:
            continue
        for r in rows:
            title = (r.get("title") or "").lower()
            title_words = set(w for w in re.split(r"[^а-яёa-z0-9]+", title) if len(w) > 3)
            # совпадение: хотя бы 1 слово из курса ИЛИ 1 слово из активности
            if course_words & title_words or act_words & title_words:
                return True
    return False


async def _scan_assign(a, course_name: str) -> None:
    info = await moodle.fetch_assignment(a.cmid)
    db.upsert_assignment(
        a.cmid, course_id=a.course_id, name=info.name, url=info.url,
        deadline_ts=info.deadline_ts or 0, intro=info.intro[:4000],
        attachment_paths=str([x["url"] for x in info.attachments]),
        state="submitted" if info.submitted else "draft",
    )
    _schedule_deadline_notifications(a.cmid, course_name, info)


def _schedule_deadline_notifications(cmid: int, course_name: str, info) -> None:
    """Напоминания: за 24ч, за 3ч, за 30мин + при просрочке."""
    if not info.deadline_ts or info.submitted:
        return
    asg = db.assignment(cmid)
    notified = {}
    if asg and asg.get("deadline_notified"):
        import json as _json
        try:
            notified = _json.loads(asg["deadline_notified"])
        except Exception:  # noqa: BLE001
            notified = {}
    chat = cfg.owner_chat_id
    if not chat:
        return
    for stage, delta in (("24h", 86400), ("3h", 10800), ("30m", 1800)):
        if notified.get(stage):
            continue
        due = info.deadline_ts - delta
        if int(time.time()) < info.deadline_ts:
            rid = db.add_reminder(
                chat, due,
                f"⏳ Дедлайн через «{'24ч' if stage == '24h' else '3ч' if stage == '3h' else '30м'}»: "
                f"{info.name} ({course_name})\n{info.url}",
                kind="deadline", payload=str(cmid))
            if rid:
                notified[stage] = rid
    db.update_assignment(cmid, deadline_notified=str(notified).replace("'", '"'))


async def _cleanup_meetings(now: int) -> None:
    for m in db.meetings():
        if m.get("opening_ts") and m["opening_ts"] + 8 * 3600 < now:
            # лекция давно прошла — убираем из мониторинга
            import sqlite3
            from .db import db as _db
            with _db._conn() as c:  # noqa: SLF001
                c.execute("DELETE FROM bbb_meetings WHERE activity_id=? AND started=1 "
                          "AND opening_ts<?", (m["activity_id"], now - 8 * 3600))


async def _auth_lost() -> None:
    log.error("СЕССИЯ MOODLE УМЕРЛА")
    if events.on_auth_lost:
        try:
            await events.on_auth_lost()
        except Exception as e:  # noqa: BLE001
            log.warning("on_auth_lost cb: %s", e)
