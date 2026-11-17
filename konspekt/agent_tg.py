"""Полноценный агент-менеджер в Telegram: индикация, файлы, фото, follow-up.

Расширяет AgentSession:
  - прогресс («🧠 Думаю…», «📡 Хожу в Moodle…», «🎬 Конспектирую запись…»)
  - streaming-обновление статуса при вызове инструментов
  - отправка файлов И картинок прямо во время работы
  - follow-up кнопки после ответа
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from .agent import AgentSession, TOOL_SCHEMAS, HANDLERS, CONFIRM_REQUIRED
from .db import db
from .tg_notify import send_document
from .ai.client import ai

log = logging.getLogger("konspect.agent.tg")

TOOL_EMOJI = {
    "moodle_health": "📊", "moodle_courses": "📚", "moodle_activities": "📚",
    "moodle_assignments": "📋", "read_page": "📖", "meeting_status": "📺",
    "scan_moodle": "📡", "get_schedule": "📅", "watch_course": "👁",
    "add_reminder": "🔔", "conspect_status": "🎬", "read_conspect": "📄",
    "analyze_assignment": "🔍", "write_assignment_answer": "✍️",
    "submit_assignment_file": "📤", "check_submission": "✅",
    "process_recording": "🎬",
}

FOLLOW_UPS = [
    ("📋 Что сдавать?", "agentfu:todo"),
    ("📅 Расписание", "agentfu:schedule"),
    ("🎬 Конспекты", "agentfu:conspects"),
]


class TGAgent:
    """Обёртка агента для Telegram: прогресс, подтверждения, follow-up."""

    def __init__(self) -> None:
        self.session = AgentSession()
        self._last_msg: dict[int, int] = {}

    async def handle(self, m: Message, text: str) -> None:
        chat_id = m.chat.id
        status = await m.answer("🤔 Думаю…")
        try:
            await m.bot.send_chat_action(chat_id, "typing")
        except Exception:  # noqa: BLE001
            pass

        async def progress(label: str) -> None:
            try:
                await status.edit_text(label)
            except Exception:  # noqa: BLE001
                pass

        async def send_file(path: str) -> None:
            await send_document(chat_id, path)

        async def send_photo(path: str, caption: str = "") -> None:
            import httpx
            import re as _re
            tok = _re.search(r"^TELEGRAM_BOT_TOKEN=(.+)$",
                             open(".env").read(), _re.M).group(1).strip()
            with open(path, "rb") as f:
                async with httpx.AsyncClient(timeout=120) as c:
                    r = await c.post(
                        f"https://api.telegram.org/bot{tok}/sendPhoto",
                        data={"chat_id": chat_id, "caption": caption[:900]},
                        files={"photo": f})
                    if r.status_code >= 300:
                        log.warning("sendPhoto failed: %s", r.text[:200])

        async def confirm_cb(description: str) -> bool:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Да, делать",
                                      callback_data="agentconfirm:1"),
                 InlineKeyboardButton(text="❌ Отмена",
                                      callback_data="agentconfirm:0")],
            ])
            try:
                await status.edit_text(description, reply_markup=kb)
            except Exception:  # noqa: BLE001
                await m.answer(description, reply_markup=kb)
            import konspekt.bot as bot_mod
            ev = asyncio.Event()
            bot_mod._agent_confirm_state[chat_id] = ev
            try:
                await asyncio.wait_for(ev.wait(), timeout=180)
                return bot_mod._agent_confirm_result.get(chat_id, False)
            except asyncio.TimeoutError:
                await progress("⌛ Не дождался ответа — отменил действие.")
                return False

        # ---- цикл агента с индикацией ----
        answer = ""
        try:
            answer, _ = await asyncio.wait_for(
                self._ask(chat_id, text, progress, send_file, confirm_cb,
                          send_photo),
                timeout=900)
        except asyncio.TimeoutError:
            await progress("⌛ Слишком долгая задача — попробуй разбить на шаги.")
            return
        except Exception as e:  # noqa: BLE001
            log.exception("tg agent")
            txt = str(e)
            if "Connection" in txt or "timeout" in txt.lower():
                await progress(
                    "🔌 Нейронка недоступна (зеркало лежит/сеть). Moodle-сканер и "
                    "уведомления работают. Попробуй позже — или спроси /todo, "
                    "это работает без ИИ.")
            else:
                await progress(f"❌ Ошибка: {e}")
            return

        try:
            await status.delete()
        except Exception:  # noqa: BLE001
            pass
        await self._reply(m, answer)

    async def _ask(self, chat_id: int, text: str, progress, send_file,
                   confirm_cb, send_photo=None) -> tuple[str, None]:
        """Цикл как в AgentSession.ask, но с progress-колбэком."""
        sess = self.session
        sess.histories.setdefault(chat_id, [sess._system_msg()])
        sess.histories[chat_id][0] = sess._system_msg()
        hist = sess.histories[chat_id]
        hist.append({"role": "user", "content": text})

        from .config import cfg
        for _turn in range(8):
            try:
                resp = await ai.client.chat.completions.create(
                    model=cfg.openai_model, messages=hist,
                    tools=TOOL_SCHEMAS, tool_choice="auto")
            except Exception:  # noqa: BLE001
                plain = await ai.chat(hist)
                hist.append({"role": "assistant", "content": plain})
                sess._trim(chat_id)
                return plain, None

            msg = resp.choices[0].message
            tool_calls = getattr(msg, "tool_calls", None)
            if not tool_calls:
                answer = msg.content or ""
                hist.append({"role": "assistant", "content": answer})
                sess._trim(chat_id)
                return answer, None

            hist.append({"role": "assistant", "content": msg.content or "",
                         "tool_calls": [{
                             "id": tc.id, "type": "function",
                             "function": {"name": tc.function.name,
                                          "arguments": tc.function.arguments}}
                             for tc in tool_calls]})
            for tc in tool_calls:
                name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                emoji = TOOL_EMOJI.get(name, "🔧")
                arg_desc = " ".join(str(v) for v in args.values()
                                    if isinstance(v, (int, str)))[:60]
                await progress(f"{emoji} {name} {arg_desc}…")

                if name in CONFIRM_REQUIRED and confirm_cb is not None:
                    ok = await confirm_cb(
                        f"⚠️ Действие на Moodle: загрузить файл в задание "
                        f"№{args.get('cmid')}?\nФайл: {args.get('file_path')}")
                    if not ok:
                        tool_result = {"skipped": "пользователь отменил"}
                    else:
                        tool_result = await sess._run_tool(name, args)
                else:
                    tool_result = await sess._run_tool(name, args)

                fp = tool_result.get("file_path") if isinstance(tool_result, dict) else None
                if fp and send_file:
                    await progress("📎 Отправляю файл…")
                    try:
                        await send_file(fp)
                    except Exception as e:  # noqa: BLE001
                        log.warning("file: %s", e)

                pp = tool_result.get("photo_path") if isinstance(tool_result, dict) else None
                if pp and send_photo:
                    await progress("🖼 Отправляю картинку…")
                    try:
                        await send_photo(pp, tool_result.get("caption", ""))
                    except Exception as e:  # noqa: BLE001
                        log.warning("photo: %s", e)

                hist.append({"role": "tool", "tool_call_id": tc.id,
                             "content": json.dumps(tool_result, ensure_ascii=False)[:12000]})
            await progress("🧠 Осмысливаю результат…")

        final = await ai.chat(hist + [{"role": "user", "content": "Итог кратко."}])
        hist.append({"role": "assistant", "content": final})
        sess._trim(chat_id)
        return final, None

    async def _reply(self, m: Message, answer: str) -> None:
        """Ответ + follow-up кнопки."""
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=t, callback_data=c)
             for t, c in FOLLOW_UPS],
        ])
        text = answer.strip() or "…"
        # Markdown → HTML: ИИ любит **жирный**, а ТГ-клиенту нужен HTML
        text = _md_to_html(text)
        if len(text) <= 3800:
            await _send_html(m, text, kb)
            return
        # длинный — кусками, кнопки на последнем
        parts, cur = [], ""
        for para in text.split("\n"):
            if len(cur) + len(para) + 1 > 3800 and cur:
                parts.append(cur)
                cur = para
            else:
                cur = f"{cur}\n{para}" if cur else para
        if cur:
            parts.append(cur)
        for i, p in enumerate(parts):
            if i == len(parts) - 1:
                await _send_html(m, p, kb)
            else:
                await _send_html(m, p, None)


def _md_to_html(text: str) -> str:
    """Markdown от ИИ → Telegram HTML: **жирный**, *курсив*, `код`,
    ### заголовки, [ссылки](url). Остальное экранируем."""
    import html as _h
    import re as _re

    # сначала вырезаем защищённые зоны (код-блоки и инлайн-код)
    code_blocks: list[str] = []
    def _stash_code(m_):
        code_blocks.append(m_.group(2))
        return f"\x00CODE{len(code_blocks)-1}\x00"
    text = _re.sub(r"```(\w*)\n(.*?)```", lambda m: _stash_code(m), text, flags=_re.S)
    inline_codes: list[str] = []
    def _stash_inline(m_):
        inline_codes.append(m_.group(1))
        return f"\x01INLINE{len(inline_codes)-1}\x01"
    text = _re.sub(r"`([^`\n]+)`", _stash_inline, text)

    # ссылки [text](url) — stash до экранирования
    links: list[str] = []
    def _stash_link(m_):
        links.append(f'<a href="{m_.group(2)}">{_h.escape(m_.group(1), quote=False)}</a>')
        return f"\x02LINK{len(links)-1}\x02"
    text = _re.sub(
        r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
        _stash_link, text)

    # экранируем HTML-спецсимволы в остальном
    text = _h.escape(text, quote=False)

    # маркдаун-жирный/курсив
    text = _re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", text)
    text = _re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", text)
    # заголовки → жирные строки
    text = _re.sub(r"^#{1,4}\s*(.+)$", r"<b>\1</b>", text, flags=_re.M)
    # моно-списки: "- " в начале строки оставляем как есть

    # возвращаем код
    for i, cb_ in enumerate(code_blocks):
        tag = f"\x00CODE{i}\x00"
        text = text.replace(tag, f"<pre>{_h.escape(cb_)}</pre>")
    for i, ic in enumerate(inline_codes):
        tag = f"\x01INLINE{i}\x01"
        text = text.replace(tag, f"<code>{_h.escape(ic)}</code>")
    for i, lk in enumerate(links):
        tag = f"\x02LINK{i}\x02"
        text = text.replace(tag, lk)
    return text


async def _send_html(m, text: str, kb) -> None:
    """Отправка с HTML-разметкой; при ошибке — чистый текст (страховка)."""
    try:
        await m.answer(text, parse_mode="HTML", reply_markup=kb,
                       disable_web_page_preview=True)
    except Exception:  # noqa: BLE001
        # что-то в разметке не так — шлём без неё
        import re as _re
        plain = _re.sub(r"<[^>]+>", "", text)
        try:
            await m.answer(plain[:4000], reply_markup=kb)
        except Exception:  # noqa: BLE001
            pass


tg_agent = TGAgent()


# ------------------------------------------------- follow-up обработчики
async def handle_followup(c) -> None:
    """Кнопки после ответа агента."""
    from aiogram.types import CallbackQuery
    action = c.data.split(":", 1)[1]
    chat_id = c.message.chat.id
    await c.answer()
    if action == "todo":
        await tg_agent.handle(c.message, "что мне сдавать? курс и дедлайн у каждого")
    elif action == "schedule":
        await tg_agent.handle(c.message, "расписание на сегодня и завтра")
    elif action == "conspects":
        await tg_agent.handle(c.message, "какие конспекты готовы? покажи список")
