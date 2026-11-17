"""Простая отправка сообщений в ТГ без bot-поллинга (для планировщика)."""
from __future__ import annotations

import logging

import httpx

from .config import cfg

log = logging.getLogger("konspect.tg")


async def send_text(chat_id: int, text: str, reply_markup=None) -> None:
    token = cfg.bot_token
    if not token or not chat_id:
        log.warning("send_text skipped (no token/chat): %s", text[:80])
        return
    payload: dict = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                     "disable_web_page_preview": True}
    if reply_markup is not None:
        # aiogram-объект → json-словарь для raw HTTP
        try:
            payload["reply_markup"] = reply_markup.model_dump(exclude_none=True)
        except AttributeError:
            payload["reply_markup"] = reply_markup
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload)
        if r.status_code >= 300:
            # повтор без HTML-разметки при ошибке парсинга
            payload.pop("parse_mode", None)
            payload.pop("reply_markup", None)
            r2 = await c.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload)
            log.warning("tg send fallback: %s %s", r.status_code, r2.status_code)


async def send_document(chat_id: int, path: str, caption: str = "") -> None:
    token = cfg.bot_token
    if not token or not chat_id:
        log.warning("send_document skipped: %s", path)
        return
    async with httpx.AsyncClient(timeout=300) as c:
        with open(path, "rb") as f:
            r = await c.post(
                f"https://api.telegram.org/bot{token}/sendDocument",
                data={"chat_id": chat_id, "caption": caption[:1000]},
                files={"document": f},
            )
        if r.status_code >= 300:
            log.error("tg sendDocument %s: %s", path, r.text[:300])
