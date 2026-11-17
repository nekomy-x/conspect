"""Telegram-бот (aiogram 3): команды, кнопки «присоединиться», конспекты, расписание."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

from aiogram import Bot, Dispatcher, F, html
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
    ReplyKeyboardMarkup, KeyboardButton,
)

from .config import cfg
from .db import db
from .moodle import moodle, MoodleAuthError
from .scanner import events, scan_all
from .tg_notify import send_text, send_document
from . import scheduler

log = logging.getLogger("konspect.bot")

HELP_TEXT = """🤖 <b>Конспект-ассистент ВГУ</b>

<b>Просто пиши мне что угодно</b> — я агент-менеджер: сам хожу в Moodle,
смотрю задания, курсы, страницы, делаю конспекты, ставлю напоминания.
Могу и ДЕЛАТЬ действия на Moodle (сдать задание) — спрошу подтверждение.

Или жми <b>☰ Меню</b> — всё управление кнопками.

Мониторинг на автомате: новые курсы/задания/записи, конспекты сами
присылаются, кнопка «Присоединиться» на лекции, дедлайны 24ч/3ч/30мин.
"""

# ------------------------------------------------------------- главное меню
MENU_KB = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="📋 Что делать", callback_data="menu:todo"),
     InlineKeyboardButton(text="📅 Расписание", callback_data="menu:schedule")],
    [InlineKeyboardButton(text="📚 Курсы и слежка", callback_data="menu:courses"),
     InlineKeyboardButton(text="⏳ Дедлайны", callback_data="menu:deadlines")],
    [InlineKeyboardButton(text="🎬 Конспекты", callback_data="menu:conspects"),
     InlineKeyboardButton(text="🧠 План на день", callback_data="menu:plan")],
    [InlineKeyboardButton(text="📊 Статус", callback_data="menu:status"),
     InlineKeyboardButton(text="⚙️ Настройки", callback_data="menu:settings")],
])

dp = Dispatcher()
_edit_mode: set[int] = set()


def _guard() -> None:
    if not cfg.bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN не задан в .env")


def allowed(message: Message) -> bool:
    ids = cfg.allowed_ids()
    chat = message.chat.id
    if not ids:
        # пока владелец не настроен — первый /start становится владельцем
        log.warning("OWNER_CHAT_ID пуст — авто-регистрация первого юзера")
        return True
    return chat in ids


# ---------------------------------------------------------------------- cmd
@dp.message(CommandStart())
async def cmd_start(m: Message) -> None:
    ids = cfg.allowed_ids()
    if not ids:
        cfg.owner_chat_id = m.chat.id
        cfg.kv_owner(m.chat.id)
        db.kv_set("owner_chat_id", m.chat.id)
        cfg.allowed_ids.cache_clear() if hasattr(cfg.allowed_ids, "cache_clear") else None
    await m.answer(
        f"Привет! Я конспект-ассистент для edu.vsu.ru.\n"
        f"Твой chat_id: <code>{m.chat.id}</code> (впиши в .env OWNER_CHAT_ID)",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="☰ Меню")]],
            resize_keyboard=True))
    await m.answer(HELP_TEXT, parse_mode="HTML", reply_markup=MENU_KB)


@dp.message(Command("help"))
async def cmd_help(m: Message) -> None:
    await m.answer(HELP_TEXT, parse_mode="HTML", reply_markup=MENU_KB)


# ------------------------------------------------------------- меню-хаб
@dp.callback_query(F.data == "menu:home")
async def cb_home(c: CallbackQuery) -> None:
    await c.answer()
    await c.message.edit_text(HELP_TEXT, parse_mode="HTML", reply_markup=MENU_KB)


@dp.callback_query(F.data.startswith("menu:"))
async def cb_menu(c: CallbackQuery) -> None:
    """Центральный обработчик кнопок меню."""
    if not cfg.allowed_ids() or c.message.chat.id not in cfg.allowed_ids():
        await c.answer("Нет доступа", show_alert=True)
        return
    page = c.data.split(":", 1)[1]
    back_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:home")]])
    try:
        if page == "todo":
            await c.answer("Собираю…")
            await _render_todo(c.message, back_kb)
        elif page == "schedule":
            await c.answer()
            await _render_schedule(c.message, back_kb)
        elif page == "courses":
            await c.answer()
            await _render_courses_menu(c.message)
        elif page == "deadlines":
            await c.answer("Собираю…")
            await _render_deadlines(c.message, back_kb)
        elif page == "conspects":
            await c.answer()
            await _render_conspects_menu(c.message, back_kb)
        elif page == "plan":
            await c.answer("Думаю…")
            await _render_plan(c.message, back_kb)
        elif page == "status":
            await c.answer()
            await _render_status(c.message, back_kb)
        elif page == "settings":
            await c.answer()
            await _render_settings(c.message)
    except Exception as e:  # noqa: BLE001
        log.exception("menu %s", page)
        await c.message.answer(f"Ошибка меню: {e}")


async def _render_todo(m: Message, kb) -> None:
    """Та же логика /todo, но в сообщение с кнопкой Назад."""
    now = int(time.time())
    sections: list[str] = []
    overdue = [a for a in db.assignments()
               if a.get("deadline_ts") and now - 30 * 86400 < a["deadline_ts"] < now
               and a.get("state") != "submitted"]
    if overdue:
        lines = []
        for a in sorted(overdue, key=lambda x: x["deadline_ts"])[:10]:
            days = (now - a["deadline_ts"]) // 86400
            lines.append(f"🔴 <a href=\"{a['url']}\">{a['name'][:48]}</a> — {days} дн")
        sections.append("❗️ <b>Просрочено:</b>\n" + "\n".join(lines))
    week = [a for a in db.assignments()
            if a.get("deadline_ts") and now <= a["deadline_ts"] <= now + 7 * 86400
            and a.get("state") != "submitted"]
    if week:
        lines = []
        for a in sorted(week, key=lambda x: x["deadline_ts"])[:10]:
            left = a["deadline_ts"] - now
            when = (f"{left // 86400} дн" if left >= 86400 else
                    f"{left // 3600} ч")
            lines.append(f"🟡 <a href=\"{a['url']}\">{a['name'][:48]}</a> — {when}")
        sections.append("📋 <b>Сдать на неделе:</b>\n" + "\n".join(lines))
    rows = db.schedule_day(datetime.now().strftime("%Y-%m-%d"))
    if rows:
        from .schedule_logic import fmt_row
        upcoming = [r for r in rows if _row_not_yet(r)]
        if upcoming:
            sections.append("📅 <b>Пары сегодня:</b>\n" +
                            "\n".join(fmt_row(r) for r in upcoming))
    text = "\n\n".join(sections) or "🎉 Ничего не горит!"
    try:
        await m.edit_text(text[:4000], parse_mode="HTML",
                          disable_web_page_preview=True, reply_markup=kb)
    except Exception:  # noqa: BLE001
        await m.answer(text[:4000], parse_mode="HTML",
                       disable_web_page_preview=True, reply_markup=kb)


async def _render_schedule(m: Message, kb) -> None:
    from .schedule_logic import today_schedule_text, tomorrow_schedule_text
    t = today_schedule_text() or "На сегодня пар нет"
    tm = tomorrow_schedule_text() or "На завтра пар нет"
    add_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Скинуть расписание (текст/фото/файл)",
                              callback_data="menu:sched_hint")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:home")]])
    try:
        await m.edit_text(f"{t}\n\n{tm}"[:4000], reply_markup=add_kb)
    except Exception:  # noqa: BLE001
        await m.answer(f"{t}\n\n{tm}"[:4000], reply_markup=add_kb)


async def _render_courses_menu(m: Message) -> None:
    courses = db.courses()
    buttons = []
    for c in courses[:50]:
        on = db.is_watched(c["id"])
        buttons.append([InlineKeyboardButton(
            text=f"{'👁' if on else '🚫'} {c['name'][:35]}",
            callback_data=f"watch:{c['id']}:{0 if on else 1}")])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:home")])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    try:
        await m.edit_text("📚 Курсы. Тап — вкл/выкл слежку (👁 следим, 🚫 игнор):",
                          reply_markup=kb)
    except Exception:  # noqa: BLE001
        await m.answer("📚 Курсы. Тап — вкл/выкл слежку:", reply_markup=kb)


async def _render_deadlines(m: Message, kb) -> None:
    now = int(time.time())
    rows = sorted([a for a in db.assignments() if a.get("deadline_ts")],
                  key=lambda x: x["deadline_ts"])
    lines = []
    for a in rows:
        left = a["deadline_ts"] - now
        mark = "✅" if a.get("state") == "submitted" else (
            "🔴" if left < 0 else "🟡")
        lines.append(f"{mark} <a href=\"{a['url']}\">{a['name'][:52]}</a>")
    text = "\n\n".join(lines[:25]) or "Дедлайнов нет"
    try:
        await m.edit_text(text[:4000], parse_mode="HTML",
                          disable_web_page_preview=True, reply_markup=kb)
    except Exception:  # noqa: BLE001
        await m.answer(text[:4000], parse_mode="HTML",
                       disable_web_page_preview=True, reply_markup=kb)


async def _render_conspects_menu(m: Message, kb) -> None:
    root = Path(cfg.data_dir) / "conspects"
    found = []
    if root.exists():
        for d in sorted(root.iterdir()):
            md = d / "conspect.md"
            if md.exists():
                try:
                    head = md.read_text(encoding="utf-8")[:400]
                    t = re.search(r"^#\s*(.+)$", head, re.M)
                    title = t.group(1).strip() if t else d.name
                except Exception:  # noqa: BLE001
                    title = d.name
                found.append((title[:45], md.stat().st_size, str(md)))
    if not found:
        try:
            await m.edit_text("Конспектов пока нет. Новые записи конспектятся "
                              "автоматически; вручную — пришли ссылку на лекцию.",
                              reply_markup=kb)
        except Exception:  # noqa: BLE001
            await m.answer("Конспектов пока нет", reply_markup=kb)
        return
    db.kv_set("conspects_list", [str(p) for _, _, p in found])
    buttons = [[InlineKeyboardButton(text=f"📄 {name[:40]} ({sz // 1024}КБ)",
                                      callback_data=f"getfile:{i}")]
               for i, (name, sz, _) in enumerate(found, 1)]
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:home")])
    kb2 = InlineKeyboardMarkup(inline_keyboard=buttons)
    try:
        await m.edit_text("🎬 Готовые конспекты — тапни, пришлю файл:",
                          reply_markup=kb2)
    except Exception:  # noqa: BLE001
        await m.answer("🎬 Готовые конспекты:", reply_markup=kb2)


@dp.callback_query(F.data.startswith("getfile:"))
async def cb_getfile(c: CallbackQuery) -> None:
    i = int(c.data.split(":", 1)[1]) - 1
    paths = db.kv_get("conspects_list", [])
    if 0 <= i < len(paths):
        await c.answer("Отправляю…")
        await send_document(c.message.chat.id, paths[i])
    else:
        await c.answer("Не найдено", show_alert=True)


async def _render_plan(m: Message, kb) -> None:
    from .ai.client import ai
    now = int(time.time())
    ctx = {
        "сейчас": datetime.now().strftime("%Y-%m-%d %H:%M (%A)"),
        "расписание_сегодня": db.schedule_day(datetime.now().strftime("%Y-%m-%d")),
        "дедлайны_несданные": [
            {"name": a["name"], "deadline": a["deadline_ts"], "state": a.get("state")}
            for a in db.assignments() if a.get("deadline_ts") and a.get("state") != "submitted"][:15],
        "лекции_сегодня": [
            {"name": mt["name"], "start": mt.get("opening_ts")}
            for mt in db.meetings()
            if mt.get("opening_ts") and now <= mt["opening_ts"] <= now + 86400],
    }
    try:
        plan = await ai.chat([
            {"role": "system", "content":
                "Ты — менеджер учёбы. Составь план на день: что делать, когда, "
                "с приоритетами. Кратко, нумерованный список со временем."},
            {"role": "user", "content": json.dumps(ctx, ensure_ascii=False)},
        ])
        try:
            await m.edit_text(plan[:4000], reply_markup=kb)
        except Exception:  # noqa: BLE001
            await m.answer(plan[:4000], reply_markup=kb)
    except Exception as e:  # noqa: BLE001
        await m.answer(f"ИИ недоступен: {e}")


async def _render_status(m: Message, kb) -> None:
    alive = await moodle.alive_simple()
    courses = db.courses()
    watched = sum(1 for c in courses if db.is_watched(c["id"]))
    asg = db.assignments()
    unsent = sum(1 for a in asg if a.get("state") != "submitted")
    text = (f"📊 <b>Статус</b>\n\n"
            f"Moodle: {'✅ живая' if alive else '❌ умерла — Settings → Обновить куки'}\n"
            f"Курсов: {len(courses)} (под слежкой: {watched})\n"
            f"Заданий: {len(asg)} (не сдано: {unsent})\n"
            f"Записей в очереди на конспект: {len(db.unprocessed_recordings())}\n"
            f"Пар сегодня: {len(db.schedule_day(datetime.now().strftime('%Y-%m-%d')))}")
    try:
        await m.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:  # noqa: BLE001
        await m.answer(text, parse_mode="HTML", reply_markup=kb)


async def _render_settings(m: Message) -> None:
    monitoring = db.kv_get("monitoring_enabled", True)
    mon_btn = ("⏸ Остановить мониторинг" if monitoring
               else "▶️ Запустить мониторинг")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=mon_btn, callback_data="set:monitor")],
        [InlineKeyboardButton(text="🔑 Пароль Moodle", callback_data="set:cookie")],
        [InlineKeyboardButton(text="🔔 Напоминания", callback_data="set:reminders")],
        [InlineKeyboardButton(text="🔄 Проверить Moodle сейчас", callback_data="set:scan")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:home")],
    ])
    alive = await moodle.alive_simple()
    text = (f"⚙️ <b>Настройки</b>\n\n"
            f"Сессия Moodle: {'✅ живая' if alive else '❌ умерла'}\n"
            f"Мониторинг: {'🟢 работает' if monitoring else '🔴 остановлен'}\n"
            f"Пользователь: {moodle.user_name or '—'}\n"
            f"Модель ИИ: {cfg.openai_model}")
    try:
        await m.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:  # noqa: BLE001
        await m.answer(text, parse_mode="HTML", reply_markup=kb)


@dp.callback_query(F.data == "set:monitor")
async def cb_set_monitor(c: CallbackQuery) -> None:
    """СТОП/СТАРТ мониторинга Moodle (защита сессии от перегруза)."""
    cur = db.kv_get("monitoring_enabled", True)
    new = not cur
    db.kv_set("monitoring_enabled", new)
    if new:
        await c.answer("Мониторинг запущен ✅")
        await c.message.edit_text("🟢 <b>Мониторинг запущен.</b>\n\n"
                                  "Скан Moodle каждые 5 мин, лекции — 2 мин.")
    else:
        await c.answer("Мониторинг остановлен")
        await c.message.edit_text("🔴 <b>Мониторинг остановлен.</b>\n\n"
                                  "Moodle не трогается — сессия не перегружается.\n"
                                  "Напоминания и расписание работают.\n"
                                  "Запустить: ⚙️ Настройки → мониторинг")


@dp.callback_query(F.data == "set:scan")
async def cb_set_scan(c: CallbackQuery) -> None:
    await c.answer("Сканирую…")
    from .scanner import scan_all
    s = await scan_all()
    await c.message.answer(f"Готово: курсов {s['courses']}, новых активностей "
                           f"{s['new_activities']}, новых записей {s['new_recordings']}")


@dp.callback_query(F.data == "set:reminders")
async def cb_set_reminders(c: CallbackQuery) -> None:
    await c.answer()
    rows = db.reminders()
    if not rows:
        text = "Напоминаний нет. Просто напиши: «напомни в 18:00 …»"
    else:
        lines = [f"{i}. {datetime.fromtimestamp(r['due_ts']):%d.%m %H:%M} — {r['text'][:50]}"
                 for i, r in enumerate(rows[:15], 1)]
        text = "🔔 Напоминания:\n" + "\n".join(lines)
        text += "\n\nНапиши новое текстом: «напомни в 19:00 текст»"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:settings")]])
    await c.message.answer(text, reply_markup=kb)


@dp.callback_query(F.data == "set:cookie")
async def cb_set_cookie(c: CallbackQuery) -> None:
    await c.answer()
    await c.message.answer(
        "Скопируй из браузера (DevTools → Application → Cookies → edu.vsu.ru) "
        "значение MoodleSession и пришли сообщением в виде:\n"
        "<code>/session p2t79r4ttsd0vtuf2sqros674j</code>", parse_mode="HTML")


@dp.message(Command("status"))
async def cmd_status(m: Message) -> None:
    if not allowed(m):
        return
    alive = await moodle.alive_simple()
    lines = [f"Moodle-сессия: {'✅ живая' if alive else '❌ умерла (/session <куки>)'}"]
    courses = db.courses()
    watched = sum(1 for c in courses if db.is_watched(c["id"]))
    lines.append(f"Курсов: {len(courses)} (под слежкой: {watched})")
    recs = db.unprocessed_recordings()
    lines.append(f"Необработанных записей: {len(recs)}")
    asg = db.assignments()
    drafts = [a for a in asg if a.get("state") != "submitted"]
    lines.append(f"Заданий: {len(asg)} (не сдано: {len(drafts)})")
    today = datetime.now().strftime("%Y-%m-%d")
    rows = db.schedule_day(today)
    lines.append(f"Пар сегодня: {len(rows)}")
    await m.answer("\n".join(lines))


@dp.message(Command("todo"))
async def cmd_todo(m: Message) -> None:
    """Менеджерская сводка: что сдавать, что скоро, что просрочено, что делать."""
    if not allowed(m):
        return
    now = int(time.time())
    sections: list[str] = []

    # 1. просроченные несданные (только за последние 30 дней — старьё не душим)
    overdue = [a for a in db.assignments()
               if a.get("deadline_ts") and now - 30 * 86400 < a["deadline_ts"] < now
               and a.get("state") != "submitted"]
    if overdue:
        lines = []
        for a in sorted(overdue, key=lambda x: x["deadline_ts"]):
            days = (now - a["deadline_ts"]) // 86400
            lines.append(f"🔴 <a href=\"{a['url']}\">{a['name'][:48]}</a> — {days} дн")
        sections.append("❗️ <b>Просрочено (сдать ASAP):</b>\n" + "\n".join(lines))

    # 2. ближайшие дедлайны (7 дней)
    week = [a for a in db.assignments()
            if a.get("deadline_ts") and now <= a["deadline_ts"] <= now + 7 * 86400
            and a.get("state") != "submitted"]
    if week:
        lines = []
        for a in sorted(week, key=lambda x: x["deadline_ts"]):
            left = a["deadline_ts"] - now
            when = (f"{left // 86400} дн" if left >= 86400 else
                    f"{left // 3600} ч {left % 3600 // 60} мин")
            lines.append(f"🟡 <a href=\"{a['url']}\">{a['name'][:48]}</a> — {when}")
        sections.append("📋 <b>Сдать на этой неделе:</b>\n" + "\n".join(lines))

    # 3. лекции сегодня (BBB по времени начала)
    today_end = now + 86400
    for mt in db.meetings():
        op = mt.get("opening_ts") or 0
        if now <= op <= today_end:
            when = datetime.fromtimestamp(op).strftime("%H:%M")
            course = next((c for c in db.courses() if c["id"] == mt["course_id"]), None)
            sections.append(
                f"📺 <b>Лекция сегодня в {when}</b>: {mt['name']}\n"
                f"{course['name'] if course else ''}")
            break

    # 4. пары сегодня из расписания
    rows = db.schedule_day(datetime.now().strftime("%Y-%m-%d"))
    if rows:
        from .schedule_logic import fmt_row
        upcoming = [r for r in rows if _row_not_yet(r)]
        if upcoming:
            lines = [fmt_row(r) for r in upcoming]
            sections.append("📅 <b>Пары сегодня:</b>\n" + "\n".join(lines))

    if not sections:
        await m.answer("🎉 Ничего не горит: просрочек нет, на неделе дедлайнов нет, "
                       "пар сегодня больше нет. Можно отдыхать.")
        return
    await m.answer("\n\n".join(sections), parse_mode="HTML",
                   disable_web_page_preview=True)


def _row_not_yet(r: dict) -> bool:
    try:
        start = datetime.strptime(
            f"{datetime.now().strftime('%Y-%m-%d')} {r['time_start']}", "%Y-%m-%d %H:%M")
        return start >= datetime.now() - timedelta(minutes=30)
    except Exception:  # noqa: BLE001
        return True


@dp.message(Command("plan"))
async def cmd_plan(m: Message) -> None:
    """План на день от ИИ-менеджера: что и когда делать по всем данным."""
    if not allowed(m):
        return
    from .ai.client import ai
    now = int(time.time())
    ctx = {
        "сейчас": datetime.now().strftime("%Y-%m-%d %H:%M (%A)"),
        "расписание_сегодня": db.schedule_day(datetime.now().strftime("%Y-%m-%d")),
        "дедлайны_несданные": [
            {"name": a["name"], "deadline": a["deadline_ts"], "state": a.get("state")}
            for a in db.assignments() if a.get("deadline_ts") and a.get("state") != "submitted"
        ][:15],
        "лекции_сегодня": [
            {"name": mt["name"], "start": mt.get("opening_ts")}
            for mt in db.meetings()
            if mt.get("opening_ts") and now <= mt["opening_ts"] <= now + 86400
        ],
        "необработанные_записи": len(db.unprocessed_recordings()),
    }
    await m.answer("🧠 Составляю план на день…")
    try:
        plan = await ai.chat([
            {"role": "system", "content":
                "Ты — менеджер учёбы студента. По данным составь план на день: "
                "что делать, в каком порядке, с учётом дедлайнов и пар. "
                "Кратко, по делу, с приоритетами. Формат: нумерованный список "
                "с временем. Учитывай, что конспекты по записям делаются автоматически."},
            {"role": "user", "content": json.dumps(ctx, ensure_ascii=False)},
        ])
        await m.answer(plan[:4000])
    except Exception as e:  # noqa: BLE001
        await m.answer(f"ИИ недоступен: {e}")


@dp.message(Command("courses"))
async def cmd_courses(m: Message) -> None:
    if not allowed(m):
        return
    try:
        courses = await moodle.fetch_courses()
    except Exception as e:  # noqa: BLE001
        await m.answer(f"Не удалось получить курсы: {e}")
        return
    lines = []
    for c in courses:
        mark = "👁" if db.is_watched(c["id"]) else "🚫"
        lines.append(f"{mark} [{c['id']}] {html.bold(c['name'])}\n{c['url']}")
    for chunk in _chunks(lines, 20):
        await m.answer("\n".join(chunk), parse_mode="HTML",
                       disable_web_page_preview=True)
    await m.answer("Управление слежкой: /watch\n(👁 — следим, 🚫 — игнорируем)")


@dp.message(Command("watch"))
async def cmd_watch(m: Message) -> None:
    """Меню вкл/выкл слежки по курсам (кнопки)."""
    if not allowed(m):
        return
    try:
        courses = await moodle.fetch_courses()
    except Exception as e:  # noqa: BLE001
        await m.answer(f"Не удалось получить курсы: {e}")
        return
    buttons = []
    for c in courses:
        on = db.is_watched(c["id"])
        btn = InlineKeyboardButton(
            text=f"{'👁' if on else '🚫'} {c['name'][:28]}",
            callback_data=f"watch:{c['id']}:{0 if on else 1}",
        )
        buttons.append([btn])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons[:60])
    await m.answer("Нажми на курс — слежка вкл/выкл. 👁 = следим, 🚫 = игнор:",
                   reply_markup=kb)


@dp.message(Command("unwatch"))
async def cmd_unwatch(m: Message, command: CommandObject) -> None:
    if not allowed(m):
        return
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await m.answer("Формат: /unwatch 32764 (id курса, смотри /courses)")
        return
    cid = int(arg)
    db.unwatch(cid)
    course = next((c for c in db.courses() if c["id"] == cid), None)
    name = course["name"] if course else str(cid)
    await m.answer(f"🚫 Слежка за «{name}» выключена. Включить: /watch")


@dp.callback_query(F.data.startswith("watch:"))
async def cb_watch(c: CallbackQuery) -> None:
    parts = c.data.split(":")
    cid, on = int(parts[1]), bool(int(parts[2]))
    db.watch(cid, enabled=on)
    course = next((x for x in db.courses() if x["id"] == cid), None)
    name = course["name"][:30] if course else str(cid)
    await c.answer(("👁 Следим за «%s»" if on else "🚫 «%s» больше не следим") % name)
    # обновляем текст и кнопку
    try:
        courses = await moodle.fetch_courses()
        buttons = []
        for cc in courses:
            is_on = db.is_watched(cc["id"])
            buttons.append([InlineKeyboardButton(
                text=f"{'👁' if is_on else '🚫'} {cc['name'][:28]}",
                callback_data=f"watch:{cc['id']}:{0 if is_on else 1}")])
        kb = InlineKeyboardMarkup(inline_keyboard=buttons[:60])
        await c.message.edit_reply_markup(reply_markup=kb)
    except Exception:  # noqa: BLE001
        pass


@dp.message(Command("conspects"))
async def cmd_conspects(m: Message) -> None:
    """Готовые конспекты с кнопками — выбрать что прислать."""
    if not allowed(m):
        return
    root = Path(cfg.data_dir) / "conspects"
    found = []
    if root.exists():
        for d in sorted(root.iterdir()):
            md = d / "conspect.md"
            if md.exists():
                try:
                    head = md.read_text(encoding="utf-8")[:400]
                    t = re.search(r"^#\s*(.+)$", head, re.M)
                    title = t.group(1).strip() if t else d.name
                except Exception:  # noqa: BLE001
                    title = d.name
                found.append((title[:55], d.name))
    if not found:
        await m.answer("Готовых конспектов пока нет. /conspect <ссылка на запись>")
        return
    buttons = []
    for i, (title, slug) in enumerate(found, 1):
        buttons.append([InlineKeyboardButton(
            text=f"📄 {i}. {title}", callback_data=f"conspect_menu:{slug}")])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await m.answer("📚 <b>Готовые конспекты</b> — выбери, что прислать:",
                   parse_mode="HTML", reply_markup=kb)


@dp.callback_query(F.data.startswith("conspect_menu:"))
async def cb_conspect_menu(c: CallbackQuery) -> None:
    """Меню конспекта: что прислать."""
    slug = c.data.split(":", 1)[1]
    root = Path(cfg.data_dir) / "conspects" / slug
    if not root.exists():
        await c.answer("Конспект не найден", show_alert=True)
        return
    web_link = f"{cfg.web_url}/conspect/{slug}"
    buttons = [
        [InlineKeyboardButton(text="🌐 Открыть онлайн", url=web_link)],
        [InlineKeyboardButton(text="📝 Конспект (MD)", callback_data=f"send_file:{slug}:conspect.md")],
        [InlineKeyboardButton(text="📊 Презентация (HTML)", callback_data=f"send_file:{slug}:presentation.html")],
        [InlineKeyboardButton(text="🎙 Стенограмма", callback_data=f"send_file:{slug}:transcript.txt")],
    ]
    # слайды
    slides_dir = root / "slides"
    if slides_dir.exists() and list(slides_dir.glob("*.png")):
        buttons.append([InlineKeyboardButton(text="🖼 Слайды (5 шт)", callback_data=f"send_slides:{slug}")])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await c.message.edit_reply_markup(reply_markup=kb)
    await c.answer()


@dp.callback_query(F.data.startswith("send_file:"))
async def cb_send_file(c: CallbackQuery) -> None:
    """Отправить конкретный файл конспекта."""
    parts = c.data.split(":")
    slug, filename = parts[1], parts[2]
    path = Path(cfg.data_dir) / "conspects" / slug / filename
    if not path.exists():
        await c.answer("Файл не найден", show_alert=True)
        return
    captions = {
        "conspect.md": "📝 Конспект (Markdown)",
        "presentation.html": "📊 Презентация (HTML, ←→)",
        "transcript.txt": "🎙 Стенограмма (текст лекции)",
    }
    await c.answer("Отправляю…")
    await send_document(c.message.chat.id, str(path),
                        caption=captions.get(filename, filename))


@dp.callback_query(F.data.startswith("send_slides:"))
async def cb_send_slides(c: CallbackQuery) -> None:
    """Отправить слайды конспекта (до 5)."""
    slug = c.data.split(":", 1)[1]
    slides_dir = Path(cfg.data_dir) / "conspects" / slug / "slides"
    if not slides_dir.exists():
        await c.answer("Слайдов нет", show_alert=True)
        return
    pngs = sorted(slides_dir.glob("*.png"))[:5]
    await c.answer(f"Отправляю {len(pngs)} слайдов…")
    for i, p in enumerate(pngs, 1):
        await send_document(c.message.chat.id, str(p), caption=f"🖼 Слайд {i}")


@dp.callback_query(F.data == "menu:sched_hint")
async def cb_sched_hint(c: CallbackQuery) -> None:
    await c.answer()
    await c.message.answer(
        "Просто пришли расписание — текстом, фото или файлом (.txt/.csv/.pdf). "
        "Я распарситаю и запомню.")


@dp.message(Command("deadlines"))
async def cmd_deadlines(m: Message) -> None:
    if not allowed(m):
        return
    now = int(time.time())
    # только актуальные (за 30 дней) — старьё 2020-х не душим
    rows = [a for a in db.assignments() if a.get("deadline_ts")
            and now - 30 * 86400 < a["deadline_ts"]]
    lines = []
    for a in sorted(rows, key=lambda x: x["deadline_ts"]):
        left = a["deadline_ts"] - now
        mark = "✅" if a.get("state") == "submitted" else (
            "🔴" if left < 0 else "🟡")
        lines.append(f"{mark} <a href=\"{a['url']}\">{a['name'][:52]}</a>"
                     + ("" if a.get("state") == "submitted" else
                        f" — {'просрочено' if left < 0 else f'через {left // 86400} дн'}"))
    if not lines:
        await m.answer("Дедлайнов нет")
        return
    await m.answer("\n".join(lines[:25]), parse_mode="HTML",
                   disable_web_page_preview=True)


@dp.message(Command("assignments"))
async def cmd_assignments(m: Message) -> None:
    if not allowed(m):
        return
    now = int(time.time())
    rows = [a for a in db.assignments()
            if not (a.get("deadline_ts") and a["deadline_ts"] < now - 365 * 86400)]
    lines = []
    for a in rows:
        mark = {"submitted": "✅", "ready": "📄"}.get(a.get("state"), "⬜")
        lines.append(f"{mark} {a['name'][:55]}")
    for chunk in _chunks(lines, 30):
        await m.answer("\n".join(chunk) + "\n\n(полные ссылки: /deadlines)")


@dp.message(Command("recordings"))
async def cmd_recordings(m: Message) -> None:
    if not allowed(m):
        return
    with db._conn() as c:  # noqa: SLF001
        rows = c.execute("SELECT * FROM recordings ORDER BY first_seen DESC LIMIT 10").fetchall()
    if not rows:
        await m.answer("Записей пока не найдено")
        return
    # компактно: кликабельные заголовки, без URL-простыней
    lines = [f"{'🎬' if r['processed'] else '⏳'} <a href=\"{r['playback_url']}\">"
             f"{(r['title'] or 'запись')[:60]}</a>"
             for r in rows]
    await m.answer("\n".join(lines), parse_mode="HTML",
                   disable_web_page_preview=True)


@dp.message(Command("schedule"))
async def cmd_schedule(m: Message) -> None:
    if not allowed(m):
        return
    from .schedule_logic import today_schedule_text, tomorrow_schedule_text
    t = today_schedule_text() or "На сегодня пар нет"
    tm = tomorrow_schedule_text() or "На завтра пар нет"
    await m.answer(f"{t}\n\n{tm}")


@dp.message(Command("sched_set"))
async def cmd_sched_set(m: Message) -> None:
    await m.answer("Пришли расписание: текстом, фото или файлом (.txt/.csv/.md). "
                   "В ответ запарсю и заменю текущее.")


@dp.message(Command("session"))
async def cmd_session(m: Message, command: CommandObject) -> None:
    if not allowed(m):
        return
    cookie = (command.args or "").strip()
    if not cookie:
        await m.answer("Скинь так: /session p2t79r4ttsd0vtuf2sqros674j")
        return
    await moodle.set_session_cookie(cookie)
    ok = await moodle.alive_simple()
    if ok:
        db.kv_set("moodle_cookie", cookie)
        await m.answer("✅ Сессия обновлена и живая")
    else:
        await m.answer("❌ Кука не подошла (страница авторизации)")


@dp.message(Command("password"))
async def cmd_password(m: Message, command: CommandObject) -> None:
    """Приём пароля Moodle от владельца. Сообщение удаляется из чата."""
    if not allowed(m):
        return
    pwd = (command.args or "").strip()
    if not pwd:
        await m.answer("Скинь так: /password <пароль>\n"
                       "Сообщение удалится автоматически, пароль не светится.")
        return
    from .secret import write_env_value, has_credentials
    write_env_value("MOODLE_PASSWORD", pwd)
    # удаляем сообщение с паролем из чата
    try:
        await m.delete()
    except Exception:  # noqa: BLE001
        pass
    # пробуем авто-логин сразу
    from .auth import auto_login
    ok, msg = await auto_login()
    if ok:
        await m.answer("✅ Пароль сохранён, авто-логин успешен. "
                       "Сессия будет обновляться сама.")
    else:
        await m.answer(f"⚠️ Пароль сохранён, но логин не прошёл: {msg}\n"
                       "Проверь логин/пароль или попробуй позже.")


@dp.message(Command("remind"))
async def cmd_remind(m: Message, command: CommandObject) -> None:
    if not allowed(m):
        return
    args = (command.args or "").split(maxsplit=2)
    if len(args) < 2:
        await m.answer("Формат: /remind 18:30 25.09.2026 текст напоминания")
        return
    tstr, text = args[0], args[-1]
    ts = _parse_remind_time(tstr, args[1] if len(args) == 3 else None)
    if not ts:
        await m.answer("Не понял время. Примеры: 18:30 / 18:30 25.09 / 18:30 25.09.2026")
        return
    rid = db.add_reminder(m.chat.id, ts, text)
    await m.answer(f"⏰ Напомню {datetime.fromtimestamp(ts):%d.%m %H:%M} (id {rid})")


def _parse_remind_time(tstr: str, dstr: str | None) -> int | None:
    try:
        hh, mm = map(int, tstr.split(":"))
        d = datetime.now()
        if dstr:
            for fmt in ("%d.%m.%Y", "%d.%m", "%d.%m.%y"):
                try:
                    d = datetime.strptime(dstr, fmt)
                    if fmt == "%d.%m":
                        d = d.replace(year=datetime.now().year)
                    break
                except ValueError:
                    continue
        dt = d.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if dt <= datetime.now() and not dstr:
            dt = dt.fromordinal(dt.toordinal() + 1)
        return int(dt.timestamp())
    except Exception:  # noqa: BLE001
        return None


@dp.message(Command("reminders"))
async def cmd_reminders(m: Message) -> None:
    if not allowed(m):
        return
    rows = db.reminders()
    if not rows:
        await m.answer("Напоминаний нет")
        return
    lines = [f"{i}. {datetime.fromtimestamp(r['due_ts']):%d.%m %H:%M} — {r['text'][:60]}"
             for i, r in enumerate(rows, 1)]
    await m.answer("\n".join(lines) + "\n\n/remove N — убрать")


@dp.message(Command("remove"))
async def cmd_remove(m: Message, command: CommandObject) -> None:
    if not allowed(m):
        return
    n = (command.args or "").strip()
    if n.isdigit():
        db.delete_reminder(int(n))
        await m.answer("Убрал (если был)")


@dp.message(Command("edit"))
async def cmd_edit(m: Message) -> None:
    if not allowed(m):
        return
    if m.chat.id in _edit_mode:
        _edit_mode.discard(m.chat.id)
        await m.answer("Режим правки выключен")
    else:
        _edit_mode.add(m.chat.id)
        await m.answer("✏️ Режим правки: пиши правки уведомлений/расписания текстом, "
                       "я применю. /edit — выключить")


# ------------------------------------------------------------ ai commands
@dp.message(Command("analyze"))
async def cmd_analyze(m: Message, command: CommandObject) -> None:
    if not allowed(m):
        return
    url = (command.args or "").strip()
    cmid = _cmid_from_url(url)
    if not cmid:
        await m.answer("Скинь ссылку вида https://edu.vsu.ru/mod/assign/view.php?id=...")
        return
    await _run_assignment_flow(m, cmid, analyze_only=True)


@dp.message(Command("answer"))
async def cmd_answer(m: Message, command: CommandObject) -> None:
    if not allowed(m):
        return
    url = (command.args or "").strip()
    cmid = _cmid_from_url(url)
    if not cmid:
        await m.answer("Скинь ссылку вида https://edu.vsu.ru/mod/assign/view.php?id=...")
        return
    await _run_assignment_flow(m, cmid, analyze_only=False)


async def _run_assignment_flow(m: Message, cmid: int, analyze_only: bool) -> None:
    from .ai.assignments import analyze_assignment, generate_answer, check_answer
    from .moodle import AssignmentInfo
    await m.answer("🔍 Разбираю задание (несколько запросов к ИИ)…")
    try:
        info = await moodle.fetch_assignment(cmid)
        analysis = await analyze_assignment(info)
        struct = analysis["analysis"].get("structure", {})
        text = (f"📋 <b>{info.name}</b>\n\n"
                f"<b>Что сделать:</b>\n" +
                "\n".join(f"• {r}" for r in struct.get("requirements", [])[:10]) +
                f"\n\n<b>Что сдать:</b>\n" +
                "\n".join(f"• {r}" for r in struct.get("deliverables", [])[:10]))
        await m.answer(text[:4000], parse_mode="HTML")
        pp = analysis["analysis"].get("pitfalls", {})
        if pp.get("pitfalls"):
            await m.answer("⚠️ Подводные камни:\n" +
                           "\n".join(f"• {p}" for p in pp["pitfalls"][:8]))
        if analyze_only:
            return
        await m.answer("✍️ Пишу готовый ответ…")
        answer = await generate_answer(info, analysis)
        verdict = await check_answer(info, answer, analysis)
        path = Path(cfg.data_dir) / "assignments" / str(cmid) / "answer.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {info.name}\n\n{answer}", encoding="utf-8")
        await send_document(m.chat.id, str(path), caption=f"Ответ: {info.name}")
        await m.answer("Проверка: " + (verdict if verdict != "ok" else "✅ всё по требованиям"))
        db.update_assignment(cmid, state="ready", answer_path=str(path), answer=answer[:4000])
    except MoodleAuthError:
        await m.answer("❌ Сессия Moodle умерла — обнови /session")
    except Exception as e:  # noqa: BLE001
        log.exception("assignment flow")
        await m.answer(f"Ошибка: {e}")


@dp.message(Command("conspect"))
async def cmd_conspect(m: Message, command: CommandObject) -> None:
    if not allowed(m):
        return
    arg = (command.args or "").strip()
    if not arg:
        await m.answer("Скинь ссылку на страницу лекции "
                       "(mod/bigbluebuttonbn/view.php?id=...) или на запись")
        return
    await _conspect_flow(m, arg)


@dp.message(Command("reconspect"))
async def cmd_reconspect(m: Message, command: CommandObject) -> None:
    """Принудительно переделать конспект."""
    if not allowed(m):
        return
    arg = (command.args or "").strip()
    if not arg:
        await m.answer("Скинь ссылку на лекцию или запись — переделаю конспект заново")
        return
    await m.answer("♻️ Переделываю конспект с нуля ( Whisper + ИИ)…")
    await _conspect_flow(m, arg, force=True)


async def _conspect_flow(m: Message, arg: str, force: bool = False) -> None:
    from .ai.pipeline import process_recording
    playback = arg
    course_name = ""
    act = None
    if "bigbluebuttonbn" in arg:
        cmid = _cmid_from_url(arg)
        if not cmid:
            await m.answer("Не понял ссылку")
            return
        info = await moodle.fetch_bbb(cmid)
        recs = info.get("recordings", [])
        if not recs:
            await m.answer("У этой лекции пока нет записей. Буду мониторить автоматически.")
            return
        playback = recs[0]["playback_url"]
        act = db.activity(cmid)
        if act:
            course = next((c for c in db.courses() if c["id"] == act["course_id"]), None)
            course_name = course["name"] if course else ""
    if not force:
        await m.answer("🎬 Проверяю/делаю конспект… (если уже сделан — просто пришлю)")
    try:
        result = await process_recording(playback, course_name or "Лекция", "",
                                         force=force)
        if result.get("error"):
            await m.answer(f"❌ {result['error']}")
            return
        if result.get("already_done"):
            await m.answer("✅ Конспект уже готов — не переделываю "
                           "(/reconspect, если нужно заново)")
        else:
            await m.answer("📄 Конспект собран!")

        slug = result.get("slug", "")
        web_link = f"{cfg.web_url}/conspect/{slug}"
        title = result.get("title", "Лекция")

        # 1. ссылка на сайт
        await m.answer(f"📖 <b>Конспект: {title}</b>\n\n"
                       f"🌐 Онлайн: {web_link}",
                       parse_mode="HTML", disable_web_page_preview=True)

        # 2. все файлы — конспект, презентация, стенограмма
        files = [
            (result.get("conspect"), "📝 Конспект (Markdown)"),
            (result.get("presentation"), "📊 Презентация (HTML, ←→)"),
            (result.get("transcript"), "🎙 Стенограмма (текст лекции)"),
        ]
        for path, caption in files:
            if path:
                try:
                    await send_document(m.chat.id, path, caption=caption)
                except Exception as e:  # noqa: BLE001
                    log.warning("file send: %s", e)

        # 3. слайды — до 5 картинок
        slides = result.get("slides", [])
        if isinstance(slides, list) and slides:
            sent = 0
            for s in slides[:5]:
                png = s.get("png") if isinstance(s, dict) else None
                if png:
                    try:
                        await send_document(m.chat.id, png,
                                            caption=f"🖼 Слайд {s.get('n', sent+1)}")
                        sent += 1
                    except Exception as e:  # noqa: BLE001
                        log.warning("slide send: %s", e)
            if sent:
                await m.answer(f"🖼 Отправил {sent} слайдов (из {len(slides)})")
    except Exception as e:  # noqa: BLE001
        log.exception("conspect flow")
        await m.answer(f"Ошибка: {e}")


def _cmid_from_url(url: str) -> int | None:
    import re
    mm = re.search(r"[?&]id=(\d+)", url)
    return int(mm.group(1)) if mm else None


# ------------------------------------------------------------ schedule input
@dp.message(F.photo)
async def on_photo(m: Message) -> None:
    if not allowed(m):
        return
    from .ai.client import ai
    await m.answer("👀 Читаю расписание с фото…")
    photo = m.photo[-1]
    token = cfg.bot_token
    import httpx
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.get(f"https://api.telegram.org/bot{token}/getFile",
                        params={"file_id": photo.file_id})
        fpath = r.json()["result"]["file_path"]
        r = await c.get(f"https://api.telegram.org/file/bot{token}/{fpath}")
        p = Path(cfg.data_dir) / "media" / f"{photo.file_id}.jpg"
        p.write_bytes(r.content)
    try:
        text = await ai.vision(
            "Распознай всё расписание с изображения. Верни просто текст расписания "
            "построчно, сохраняя дни, время, предметы, аудитории.", [p])
        await _ingest_schedule(m, text)
    except Exception as e:  # noqa: BLE001
        await m.answer(f"Не смог распознать: {e}")


@dp.message(F.document)
async def on_document(m: Message) -> None:
    if not allowed(m):
        return
    doc = m.document
    name = doc.file_name or "file"
    ext = Path(name).suffix.lower()
    if ext not in (".txt", ".csv", ".md", ".pdf", ".xlsx", ".xls", ".docx"):
        await m.answer("Принимаю .txt/.csv/.md/.pdf/.xlsx/.xls/.docx")
        return
    import httpx
    token = cfg.bot_token
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.get(f"https://api.telegram.org/bot{token}/getFile",
                        params={"file_id": doc.file_id})
        fpath = r.json()["result"]["file_path"]
        r = await c.get(f"https://api.telegram.org/file/bot{token}/{fpath}")
        p = Path(cfg.data_dir) / "media" / name
        p.write_bytes(r.content)
    # извлекаем текст по типу файла
    try:
        if ext == ".pdf":
            from .ai.assignments import pdf_text
            text = pdf_text(p)
        elif ext in (".xlsx", ".xls"):
            text = _xlsx_to_text(p)
        elif ext == ".docx":
            text = _docx_to_text(p)
        else:
            text = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001
        await m.answer(f"Не смог прочитать файл: {e}")
        return
    if not text.strip():
        await m.answer("Файл пустой или не читается 🤔")
        return
    await m.answer("Распарсил файл, заношу расписание…")
    await _ingest_schedule(m, text)


def _xlsx_to_text(p: Path) -> str:
    """Excel → текст расписания.

    Для СПО-расписаний ВГУ (шаблон с «Группа 01/N»): раскрывает объединённые
    ячейки, берёт колонку 4-й группы (ИСиП), дедуплицирует пары подгрупп.
    Фоллбэк — обычный табличный дамп.
    """
    try:
        from openpyxl import load_workbook
    except ImportError:  # noqa: BLE001
        return p.read_text(encoding="utf-8", errors="replace")
    wb = load_workbook(str(p))  # не read_only — нужны merged ranges
    parts: list[str] = []
    for ws in wb.worksheets:
        # матрица значений с раскрытыми merge: значение anchor → во все ячейки
        vals: dict = {}
        for row in ws.iter_rows():
            for cell in row:
                if cell.value is not None and "MergedCell" not in str(type(cell)):
                    vals[(cell.row, cell.column)] = cell.value
        for mc in ws.merged_cells.ranges:
            anchor = vals.get((mc.min_row, mc.min_col))
            if anchor is None:
                continue
            for r in range(mc.min_row, mc.max_row + 1):
                for c in range(mc.min_col, mc.max_col + 1):
                    vals[(r, c)] = anchor

        # ищем строку «Группа 01/N» — колонки групп
        group_col = None
        max_row = max((r for (r, c) in vals), default=0)
        for (r, c), v in vals.items():
            if isinstance(v, str) and "Группа" in v and "01/4" in v.replace(" ", ""):
                group_col = c
                break
        if group_col is None:
            # просто дамп
            for r in range(1, max_row + 1):
                cells = [str(vals.get((r, c), "")).strip() for c in range(1, 25)]
                cells = [x for x in cells if x]
                if cells:
                    parts.append(" | ".join(cells))
            continue

        days_map = {"Пон": "Понедельник", "Вто": "Вторник", "Сре": "Среда",
                    "Чет": "Четверг", "Пят": "Пятница", "Суб": "Суббота"}
        day = ""
        seen: set[tuple] = set()
        for r in range(1, max_row + 1):
            d0 = str(vals.get((r, 1), "") or "").strip()
            if d0[:3] in days_map:
                day = days_map[d0[:3]]
            t_raw = str(vals.get((r, 2), "") or "").strip()
            if not t_raw or t_raw == "-":
                continue
            t = re.sub(r"\s+", "", t_raw.replace("\n", ""))
            if not re.match(r"\d{1,2}:\d{2}-\d{1,2}:\d{2}", t):
                continue
            c = vals.get((r, group_col))
            if not c or not day:
                continue
            subj = str(c).strip()
            lines = [l for l in subj.split("\n") if l.strip()]
            subject_name = lines[0].strip() if lines else ""
            place = teacher = ""
            for ln in lines[1:]:
                ln = ln.strip()
                if "ДИСТАНТ" in ln.upper() or "ONLINE" in ln.upper():
                    place = "онлайн"
                elif ("ауд" in ln.lower() or "корпус" in ln.lower()
                      or "вгу" in ln.lower()):
                    place = ln
                elif len(ln) > 2 and not teacher:
                    teacher = ln
            key = (day, t, subject_name)
            if key in seen or not subject_name:
                continue
            seen.add(key)
            parts.append(f"{day} | {t} | {subject_name} | {place} | {teacher}")
    return "\n".join(parts)


def _docx_to_text(p: Path) -> str:
    """Word → текст."""
    import zipfile
    import re
    with zipfile.ZipFile(p) as z:
        xml = z.read("word/document.xml").decode("utf-8", errors="replace")
    xml = re.sub(r"<w:p[ >]", "\n<w:p ", xml)
    text = re.sub(r"<[^>]+>", "", xml)
    return re.sub(r"\n{3,}", "\n\n", text)


@dp.message(F.text)
async def on_text(m: Message) -> None:
    if not allowed(m):
        return
    txt = (m.text or "").strip()
    # кнопка «☰ Меню» с клавиатуры-подсказки
    if txt in ("☰ Меню", "меню", "Меню", "/меню"):
        await m.answer(HELP_TEXT, parse_mode="HTML", reply_markup=MENU_KB)
        return
    # расписание: длинный текст с временем или «пар»
    if m.chat.id in _edit_mode or _looks_like_schedule(txt) or txt.lower().startswith(("расписание", "расписание:")):
        await _ingest_schedule(m, txt)
        return
    # всё остальное — свободный диалог с агентом-менеджером
    await _agent_reply(m, txt)


async def _agent_reply(m: Message, txt: str) -> None:
    """Свободный запрос к полноценному TG-агенту: прогресс, файлы, follow-up."""
    from .agent_tg import tg_agent
    await tg_agent.handle(m, txt)


async def _send_long(m: Message, text: str, chunk: int = 3800) -> None:
    """Отправить длинный текст кусками (лимит ТГ 4096)."""
    if len(text) <= chunk:
        await m.answer(text)
        return
    # режем по абзацам/строкам чтобы не рвать слова
    parts: list[str] = []
    cur = ""
    for para in text.split("\n"):
        if len(cur) + len(para) + 1 > chunk and cur:
            parts.append(cur)
            cur = para
        else:
            cur = f"{cur}\n{para}" if cur else para
    if cur:
        parts.append(cur)
    for p in parts:
        await m.answer(p)


_agent_confirm_state: dict[int, asyncio.Event] = {}
_agent_confirm_result: dict[int, bool] = {}


@dp.callback_query(F.data.startswith("agentconfirm:"))
async def cb_agent_confirm(c: CallbackQuery) -> None:
    ok = c.data.endswith("1")
    _agent_confirm_result[c.message.chat.id] = ok
    ev = _agent_confirm_state.get(c.message.chat.id)
    if ev:
        ev.set()
    await c.answer("Принято!" if ok else "Отменено")


@dp.callback_query(F.data.startswith("agentfu:"))
async def cb_agent_followup(c: CallbackQuery) -> None:
    """Follow-up кнопки после ответа агента."""
    from .agent_tg import handle_followup
    await handle_followup(c)


def _looks_like_schedule(txt: str) -> bool:
    import re
    has_time = re.search(r"\d{1,2}:\d{2}", txt)
    many_lines = txt.count("\n") >= 2
    schedule_words = re.search(r"(понедельник|вторник|среда|четверг|пятница|суббота|пара|ауд|недел)", txt, re.I)
    return bool(has_time and (many_lines or schedule_words))


async def _ingest_schedule(m: Message, text: str) -> None:
    from .schedule_logic import ingest_schedule, fmt_day, fmt_row
    res = await ingest_schedule(text)
    rows = res["rows"]
    if not rows:
        await m.answer("Не нашёл ни одной пары в этом тексте 🤔 Скинь подробнее.")
        return
    lines = [f"✅ Записал {res['count']} пар:"]
    byday: dict[str, list] = {}
    for r in rows:
        byday.setdefault(r["day"], []).append(r)
    for day in sorted(byday)[:7]:
        lines.append(f"\n{fmt_day(day)}")
        lines += [fmt_row(r) for r in byday[day]]
    await m.answer("\n".join(lines)[:4000])


async def _assistant_reply(m: Message, txt: str) -> None:
    from .ai.client import ai
    ctx = {
        "курсы": [c["name"] for c in db.courses()][:10],
        "дедлайны": [
            {"name": a["name"], "deadline": a["deadline_ts"]}
            for a in db.assignments() if a.get("deadline_ts")][:8],
        "расписание_сегодня": db.schedule_day(datetime.now().strftime("%Y-%m-%d")),
    }
    try:
        answer = await ai.chat([
            {"role": "system", "content":
                "Ты — ассистент студента. Отвечай кратко по-русски. Контекст: "
                + json.dumps(ctx, ensure_ascii=False)},
            {"role": "user", "content": txt},
        ])
        await m.answer(answer[:4000])
    except Exception as e:  # noqa: BLE001
        await m.answer(f"ИИ недоступен: {e}")


# ---------------------------------------------------------------- callbacks
@dp.callback_query(F.data.startswith("join:"))
async def cb_join(c: CallbackQuery) -> None:
    # короткий id в callback_data (лимит ТГ 64 байта), URL берём из БД
    key = c.data.split(":", 1)[1]
    url = db.kv_get(f"joinurl:{key}") or ""
    if not url:
        await c.answer("Ссылка не найдена, спроси заново", show_alert=True)
        return
    await c.answer("Открываю конференцию…")
    await c.message.answer(f"🔗 <a href=\"{url}\">Присоединиться к конференции</a>",
                           parse_mode="HTML")
    await c.message.answer(f"{url}")


@dp.callback_query(F.data.startswith("conspect:"))
async def cb_conspect(c: CallbackQuery) -> None:
    key = c.data.split(":", 1)[1]
    playback = db.kv_get(f"playurl:{key}") or key  # фолбэк: сам key = url
    await c.answer("Запускаю конспект…")
    await _conspect_flow(c.message, playback)


@dp.callback_query(F.data.startswith("analyze:"))
async def cb_analyze(c: CallbackQuery) -> None:
    cmid = int(c.data.split(":", 1)[1])
    await c.answer("Разбираю…")
    await _run_assignment_flow(c.message, cmid, analyze_only=True)


@dp.callback_query(F.data.startswith("dead:"))
async def cb_dead(c: CallbackQuery) -> None:
    cmid = int(c.data.split(":", 1)[1])
    await c.answer("Пишу ответ…")
    await _run_assignment_flow(c.message, cmid, analyze_only=False)


# ------------------------------------------------------------------- events
def wire_events(bot: Bot) -> None:
    async def notify(chat_id: int, text: str, kb: InlineKeyboardMarkup | None = None) -> None:
        await send_text(chat_id, text)

    async def on_new_course(course: dict) -> None:
        await send_text(cfg.owner_chat_id,
                        f"🆕 <b>Новый курс</b>: {course['name']}\n{course['url']}")

    async def on_new_activity(a: dict) -> None:
        # для заданий — авто-разбор
        if a["modname"] == "assign":
            asyncio.create_task(_auto_analyze_assignment(a))
            return
        # для остального — просто уведомление
        await send_text(cfg.owner_chat_id,
                        f"{a['label']}: <b>{a['name']}</b>\n"
                        f"Курс: {a['course_name']}\n{a['url']}")

    async def _auto_analyze_assignment(a: dict) -> None:
        """Авто-разбор нового задания: описание, вложения, дедлайн, что сдать."""
        from .moodle import moodle
        from .ai.assignments import analyze_assignment
        try:
            info = await moodle.fetch_assignment(a["cmid"])
            analysis = await analyze_assignment(info)
            struct = analysis.get("analysis", {}).get("structure", {})
            pit = analysis.get("analysis", {}).get("pitfalls", {})

            lines = [f"📋 <b>Новое задание: {info.name}</b>",
                     f"Курс: {a['course_name']}"]
            if info.deadline_ts:
                from datetime import datetime
                dl = datetime.fromtimestamp(info.deadline_ts)
                lines.append(f"⏰ Дедлайн: {dl.strftime('%d.%m.%Y %H:%M')}")
            if struct.get("requirements"):
                lines.append("\n<b>Требования:</b>")
                for r in struct["requirements"][:8]:
                    lines.append(f"• {r}")
            if struct.get("deliverables"):
                lines.append("\n<b>Что сдать:</b>")
                for d in struct["deliverables"][:6]:
                    lines.append(f"• {d}")
            if pit.get("pitfalls"):
                lines.append("\n⚠️ <b>Подводные камни:</b>")
                for p in pit["pitfalls"][:4]:
                    lines.append(f"• {p}")
            if info.attachments:
                lines.append(f"\n📎 Вложений: {len(info.attachments)}")

            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✍️ Написать ответ",
                                      callback_data=f"dead:{a['cmid']}")],
            ])
            await send_text(cfg.owner_chat_id, "\n".join(lines),
                            reply_markup=kb)
        except Exception as e:  # noqa: BLE001
            log.warning("auto analyze failed: %s", e)
            await send_text(cfg.owner_chat_id,
                            f"📋 <b>Новое задание:</b> {a['name']}\n"
                            f"Курс: {a['course_name']}\n{a['url']}\n"
                            f"(авто-разбор не удался: {e})")

    _conspect_tasks: set[asyncio.Task] = set()

    async def _auto_conspect(rec: dict, course_name: str) -> None:
        """Фоновая обработка новой записи: конспект делается сам."""
        from .ai.pipeline import process_recording
        from .db import db as _db
        from .media import fetch_recording_info
        try:
            # ФИЛЬТР: только записи МОИХ курсов (по context-id в метаданных BBB)
            try:
                info = await fetch_recording_info(rec["playback_url"])
                ctx = info.get("context-id") or ""
                my_courses = {str(c["id"]) for c in db.courses()}
                if ctx and ctx not in my_courses:
                    log.info("запись чужого курса (ctx=%s) — пропускаю", ctx)
                    _db.mark_recording(rec["playback_url"], 1)
                    return
            except Exception as e:  # noqa: BLE001
                log.warning("context filter check failed: %s", e)

            await send_text(cfg.owner_chat_id,
                            f"🎬 <b>Появилась запись</b>: {rec['title'] or rec['name']}\n"
                            f"Курс: {course_name}\n"
                            "🧠 Конспектирую автоматически — пришлю, как будет готово…")
            result = await process_recording(rec["playback_url"], course_name, "")
            if result.get("error"):
                await send_text(cfg.owner_chat_id,
                                f"❌ Конспект не собрался: {result['error']}")
                _db.mark_recording(rec["playback_url"], 0)
                return

            title = rec['title'] or rec['name']
            slug = result.get("slug", "")
            web_link = f"{cfg.web_url}/conspect/{slug}"

            # 1. ссылка на сайт
            await send_text(cfg.owner_chat_id,
                            f"📄 <b>Конспект готов: {title}</b>\n\n"
                            f"🌐 Онлайн: {web_link}")

            # 2. все файлы
            files = [
                (result.get("conspect"), "📝 Конспект (Markdown)"),
                (result.get("presentation"), "📊 Презентация (HTML, ←→)"),
                (result.get("transcript"), "🎙 Стенограмма (текст лекции)"),
            ]
            for path, caption in files:
                if path:
                    try:
                        await send_document(cfg.owner_chat_id, path, caption=caption)
                    except Exception as e:  # noqa: BLE001
                        log.warning("auto conspect file: %s", e)

            # 3. слайды — до 5 картинок
            slides = result.get("slides", [])
            if isinstance(slides, list) and slides:
                sent = 0
                for s in slides[:5]:
                    png = s.get("png") if isinstance(s, dict) else None
                    if png:
                        try:
                            await send_document(cfg.owner_chat_id, png,
                                                caption=f"🖼 Слайд {s.get('n', sent+1)}")
                            sent += 1
                        except Exception as e:  # noqa: BLE001
                            log.warning("auto conspect slide: %s", e)
                if sent:
                    await send_text(cfg.owner_chat_id,
                                    f"🖼 Отправил {sent} слайдов (из {len(slides)})")

            _db.mark_recording(rec["playback_url"], 1)
        except Exception as e:  # noqa: BLE001
            log.warning("auto conspect skip: %s", e)
            # старые/недоступные/чужие — тихо пометить, НЕ спамить в чат
            _db.mark_recording(rec["playback_url"], 1)

    async def on_new_recording(rec: dict, course_name: str) -> None:
        # авто-конспект ТОЛЬКО записей моложе 24 ч (старьё не жжёт CPU)
        import re as _re
        import time as _t
        fresh = True
        m = _re.search(r"-(\d{13})$", rec["playback_url"].split("?")[0])
        if m:
            fresh = (int(_t.time() * 1000) - int(m.group(1))) < 1 * 86400 * 1000
        # Moodle bbb_view URL без timestamp = старая/импортированная → не свежая
        if "bbb_view.php" in rec["playback_url"] and not m:
            fresh = False
        if events.auto_conspect and fresh:
            # строгая очередь: ОДИН конспект за раз (Whisper на CPU тяжёлый)
            while len([t for t in _conspect_tasks if not t.done()]) >= 1:
                await asyncio.sleep(60)
            task = asyncio.create_task(_auto_conspect(rec, course_name))
            _conspect_tasks.add(task)
            task.add_done_callback(_conspect_tasks.discard)
        else:
            # старая/чужая запись — тихо пометить, БЕЗ уведомлений
            from .db import db as _db2
            _db2.mark_recording(rec["playback_url"], 1)
            log.info("запись старая/чужая — пропущена тихо: %s",
                     rec["title"][:50])

    async def on_meeting_upcoming(m: dict, course_name: str) -> None:
        when = datetime.fromtimestamp(m["opening_ts"]).strftime("%H:%M") if m.get("opening_ts") else "?"
        await send_text(cfg.owner_chat_id,
                        f"⏰ <b>Лекция скоро</b> (в {when}): {m['name']}\n"
                        f"Курс: {course_name}\n{m['url']}\n"
                        "Кнопка «Присоединиться» придёт, когда конференция откроется.")

    async def on_meeting_started(m: dict, course_name: str) -> None:
        # callback_data лимит 64 байта — URL длиннее, храним в kv
        key = str(m["activity_id"])
        db.kv_set(f"joinurl:{key}", m.get("join_url") or "")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎥 Присоединиться",
                                  callback_data=f"join:{key}")],
        ])
        await send_text(cfg.owner_chat_id,
                        f"🎥 <b>Конференция открылась</b>: {m['name']}\n"
                        f"Курс: {course_name}", reply_markup=kb)

    async def on_deadline(asg: dict, course_name: str, text: str) -> None:
        await send_text(cfg.owner_chat_id, text +
                        f"\n\n📌 <a href=\"{asg['url']}\">{asg['name'][:60]}</a>")

    async def on_auth_lost() -> None:
        await send_text(cfg.owner_chat_id,
                        "❌ <b>Сессия Moodle умерла, авто-логин не помог.</b>\n\n"
                        "Возможно, сменился пароль или включена капча.\n"
                        "Варианты:\n"
                        "1. Пришли новый пароль: /password &lt;пароль&gt;\n"
                        "2. Или куку из браузера: /session &lt;MoodleSession&gt;")

    events.on_new_course = on_new_course
    events.on_new_activity = on_new_activity
    events.on_new_recording = on_new_recording
    events.on_meeting_upcoming = on_meeting_upcoming
    events.on_meeting_started = on_meeting_started
    events.on_deadline = on_deadline
    events.on_auth_lost = on_auth_lost


def _chunks(lst: list, n: int):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


# ---------------------------------------------------------------------- run
async def run() -> None:
    _guard()
    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode="HTML"))
    wire_events(bot)
    # проверка токена до старта — внятная ошибка вместо стектрейса
    try:
        me = await bot.get_me()
        log.info("бот авторизован: @%s", me.username)
    except Exception as e:  # noqa: BLE001
        await bot.session.close()
        raise SystemExit(
            f"Telegram отклонил токен ({e}). Проверь TELEGRAM_BOT_TOKEN в .env")
    scheduler.start()
    # восстановление куки из БД, если в env пусто
    stored = db.kv_get("moodle_cookie")
    if stored and not cfg.moodle_session_cookie:
        await moodle.set_session_cookie(stored)
    # восстановить владельца из БД, если env пуст
    if not cfg.owner_chat_id:
        owner = db.kv_get("owner_chat_id")
        if owner:
            cfg.owner_chat_id = int(owner)
    log.info("bot polling…")
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.stop()
        await moodle.close()
