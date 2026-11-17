"""Логика расписания: парсинг любого входа (текст/фото/файл) + брифинги."""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta

from .config import cfg
from .db import db
from .schedule_parse import parse_schedule_text
from .ai.client import ai  # noqa: F401  (используется в ai_parse_schedule)

log = logging.getLogger("konspect.schedule")

WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


async def ingest_schedule(raw_text: str, source: str = "manual") -> dict:
    """Универсальный ввод расписания: сперва эвристики, потом нейронка-парсер."""
    rows = parse_schedule_text(raw_text)
    if not rows:
        # fallback: LLM-парсер в JSON
        rows = await ai_parse_schedule(raw_text)
    n = db.replace_schedule(rows, source=source)
    return {"count": n, "rows": rows}


async def ai_parse_schedule(raw_text: str) -> list[dict]:
    try:
        data = await ai.chat_json([
            {"role": "system", "content":
                "Ты парсишь университетское расписание. Отвечай только JSON."},
            {"role": "user", "content": (
                f"Текст расписания:\n{raw_text[:12000]}\n\n"
                "Сегодня: " + datetime.now().strftime("%Y-%m-%d (%A)") + "\n"
                'Верни JSON {"rows": [{"day": "YYYY-MM-DD", "time_start": "HH:MM", '
                '"time_end": "HH:MM", "title": "предмет", "place": "аудитория/ссылка", '
                '"kind": "lecture|practice|online|other", "teacher": "имя"}]}. '
                "Если день недели без даты — вычисли ближайшую дату. "
                "Если несколько недель — все даты разверни."
            )},
        ])
        return [r for r in data.get("rows", []) if r.get("day") and r.get("time_start")]
    except Exception as e:  # noqa: BLE001
        log.error("ai_parse_schedule: %s", e)
        return []


def fmt_day(day: str) -> str:
    try:
        d = datetime.strptime(day, "%Y-%m-%d")
        wd = WEEKDAYS[d.weekday()]
        return d.strftime(f"%d.%m.%Y ({wd})")
    except ValueError:
        return day


def fmt_row(r: dict) -> str:
    icon = {"lecture": "📖", "practice": "🛠", "online": "📺"}.get(r.get("kind"), "•")
    t = f"{r.get('time_start', '')}–{r.get('time_end', '')}".strip("–")
    place = f" · {r['place']}" if r.get("place") else ""
    teacher = f" · {r['teacher']}" if r.get("teacher") else ""
    return f"{icon} {t} {r.get('title', '')}{place}{teacher}"


def today_schedule_text() -> str | None:
    today = datetime.now().strftime("%Y-%m-%d")
    rows, parity = db.schedule_day_parity(today)
    if not rows:
        return None
    week_label = "чётная (верхняя)" if parity == "even" else "нечётная (нижняя)"
    lines = [f"📅 Расписание на сегодня, {fmt_day(today)} "
             f"[неделя {week_label}]:"]
    lines += [fmt_row(r) for r in rows]
    return "\n".join(lines)


def tomorrow_schedule_text() -> str | None:
    tm = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    rows, parity = db.schedule_day_parity(tm)
    if not rows:
        return None
    week_label = "чётная (верхняя)" if parity == "even" else "нечётная (нижняя)"
    first = rows[0]
    lines = [f"📅 Завтра, {fmt_day(tm)} [неделя {week_label}]. "
             f"Пар: {len(rows)}. Начало в {first.get('time_start')}.",
             ""]
    lines += [fmt_row(r) for r in rows]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Уведомления
# --------------------------------------------------------------------------
async def daily_briefing() -> None:
    """Утром в 7:00: расписание на сегодня (дублируется за 1:30 до первой пары
    в hourly_schedule_check)."""
    from .tg_notify import send_text
    txt = today_schedule_text()
    if txt and cfg.owner_chat_id:
        await send_text(cfg.owner_chat_id, txt)


async def morning_wakeup() -> None:
    """Вечером в 21:00: «завтра пары, первая в ...» с расчётом сна и дороги."""
    from .tg_notify import send_text
    from .config import cfg
    txt = tomorrow_schedule_text()
    if txt and cfg.owner_chat_id:
        tm = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        rows, _ = db.schedule_day_parity(tm)
        if rows:
            try:
                first = datetime.strptime(f"{tm} {rows[0]['time_start']}", "%Y-%m-%d %H:%M")
                # будильник = первая пара − дорога
                # лечь = будильник − сон − время на засыпание
                wake_at = first - timedelta(minutes=cfg.commute_minutes)
                sleep_by = (wake_at - timedelta(hours=cfg.sleep_hours)
                            - timedelta(minutes=cfg.fall_asleep_minutes))
                sleep_h = int(cfg.sleep_hours) if cfg.sleep_hours == int(cfg.sleep_hours) else cfg.sleep_hours
                wake_h, wake_m = divmod(cfg.commute_minutes, 60)
                commute_txt = f"{wake_h} ч {wake_m} мин" if wake_h else f"{wake_m} мин"
                txt += (f"\n\n🛌 Сон {sleep_h} ч + засыпание {cfg.fall_asleep_minutes} мин"
                        f" + дорога {commute_txt}:"
                        f"\n— будильник в {wake_at.strftime('%H:%M')}"
                        f"\n— лечь до {sleep_by.strftime('%H:%M')}"
                        f"\n— отбой (глаза закрыть) в {sleep_by.strftime('%H:%M')}")
            except Exception:  # noqa: BLE001
                pass
        await send_text(cfg.owner_chat_id, txt)


async def hourly_schedule_check() -> None:
    """Каждый час: 1) расписание на сегодня за 1:30 до первой пары;
    2) уведомление о паре за 15 мин; 3) «началась» по времени."""
    from .tg_notify import send_text
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    rows, _ = db.schedule_day_parity(today)
    for r in rows:
        try:
            start = datetime.strptime(f"{today} {r['time_start']}", "%Y-%m-%d %H:%M")
        except Exception:  # noqa: BLE001
            continue
        dt_min = (start - now).total_seconds() / 60
        # 90 минут до пары — сводка на день (один раз)
        if 0 < dt_min <= 90 and not _notified(r, "brief90"):
            db.set_schedule_notified(r["id"], "brief90")
            txt = today_schedule_text()
            if txt:
                await send_text(cfg.owner_chat_id, txt)
        # 15 минут до пары
        if 0 < dt_min <= 15 and not _notified(r, "soon15"):
            db.set_schedule_notified(r["id"], "soon15")
            title = (r.get("title") or "").strip()
            if title and title != ".":   # мусорные пустые названия не слать
                await send_text(cfg.owner_chat_id,
                                f"⏰ Через 15 минут ({r['time_start']}): {title} "
                                f"{('· ' + r['place']) if r.get('place') else ''}")
        # началась
        if -5 <= dt_min <= 0 and not _notified(r, "started"):
            db.set_schedule_notified(r["id"], "started")
            await send_text(cfg.owner_chat_id,
                            f"▶️ Пара началась: {r.get('title')} "
                            f"{('· ' + r['place']) if r.get('place') else ''}")


def _notified(r: dict, flag: str) -> bool:
    import json
    try:
        return bool(json.loads(r.get("notified") or "{}").get(flag))
    except Exception:  # noqa: BLE001
        return False
