"""Нейросетевой слой: OpenAI-клиент, многошаговый анализ, конспекты, презентации.

Философия «агентных» запросов: каждая большая задача разбивается на
шаги (шаг = запрос + инструкция + проверка результата). Плохой результат
прогоняется повторно с уточнением. Всё это — в ai.pipeline.
"""
from __future__ import annotations

import base64
import json
import logging
import re
from pathlib import Path
from typing import Any, Callable

from openai import AsyncOpenAI

from ..config import cfg

log = logging.getLogger("konspect.ai")


class AI:
    def __init__(self) -> None:
        self.client = AsyncOpenAI(api_key=cfg.openai_api_key, base_url=cfg.openai_base_url)

    @staticmethod
    def _message_text(msg: Any) -> str:
        """Текст ответа. У reasoning-моделей (deepseek-v4.1 и подобных)
        content может прийти пустым, а весь вывод лежать в reasoning_content —
        тогда берём его, иначе получим пустой конспект без всякой ошибки."""
        text = (getattr(msg, "content", None) or "").strip()
        if text:
            return text
        for attr in ("reasoning_content", "reasoning"):
            extra = (getattr(msg, attr, None) or "").strip()
            if extra:
                return extra
        return ""

    async def chat(
        self,
        messages: list[dict],
        model: str | None = None,
        json_mode: bool = False,
        max_tokens: int | None = None,
    ) -> str:
        model = model or cfg.openai_model
        kwargs: dict[str, Any] = {}
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        if max_tokens:
            kwargs["max_tokens"] = max_tokens
        resp = await self.client.chat.completions.create(
            model=model, messages=messages, **kwargs
        )
        choice = resp.choices[0]
        text = self._message_text(choice.message)
        if not text:
            raise RuntimeError(
                f"модель {model} вернула пустой ответ "
                f"(finish_reason={choice.finish_reason!r}) — вероятно, "
                f"не хватило лимита токенов для reasoning-модели"
            )
        return text

    async def chat_stream_events(self, messages: list[dict],
                                 model: str | None = None):
        """Поток с разделением: размышления модели ≠ текст ответа.

        У reasoning-моделей (deepseek-v4.1 и подобных) внутренние
        рассуждения идут отдельным полем reasoning_content. Отдавать их
        как ответ нельзя — пользователь увидит английские черновики
        вместо ответа. Поэтому размечаем события типами.
        """
        model = model or cfg.openai_model
        stream = await self.client.chat.completions.create(
            model=model, messages=messages, stream=True
        )
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            think = getattr(delta, "reasoning_content", None)
            if not think:
                extra = getattr(delta, "model_extra", None) or {}
                think = extra.get("reasoning_content") or extra.get("reasoning")
            if think:
                yield {"type": "reasoning", "text": think}
            text = getattr(delta, "content", None)
            if text:
                yield {"type": "delta", "text": text}

    async def chat_stream(
        self,
        messages: list[dict],
        model: str | None = None,
    ):
        """Потоковая генерация: отдаёт куски ТЕКСТА ОТВЕТА по мере появления.

        Нужна веб-хабу (SSE): ответ печатается сразу, а не ждёт минуту молча.
        Отмена на стороне клиента рвёт генератор — запрос к модели закрывается.
        """
        async for ev in self.chat_stream_events(messages, model):
            if ev["type"] == "delta":
                yield ev["text"]

    async def chat_json(self, messages: list[dict], model: str | None = None) -> dict:
        txt = await self.chat(messages, model=model, json_mode=True)
        try:
            return json.loads(txt)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", txt, re.S)
            if m:
                return json.loads(m.group(0))
            raise

    async def vision(self, prompt: str, images: list[Path | str], model: str | None = None) -> str:
        """OCR/анализ изображений (скриншоты слайдов)."""
        content: list[dict] = [{"type": "text", "text": prompt}]
        for img in images:
            p = Path(img)
            b64 = base64.b64encode(p.read_bytes()).decode()
            mime = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"},
            })
        messages = [{"role": "user", "content": content}]
        resp = await self.client.chat.completions.create(
            model=model or cfg.openai_vision_model, messages=messages
        )
        return self._message_text(resp.choices[0].message)

    async def transcribe_audio(self, path: Path, model: str = "whisper-1") -> str:
        """Опционально: распознавание через API (локальный whisper в media.py)."""
        with open(path, "rb") as f:
            resp = await self.client.audio.transcriptions.create(model=model, file=f)
        return resp.text


ai = AI()


# ---------------------------------------------------------------------------
# Многошаговый конвейер с проверкой результата
# ---------------------------------------------------------------------------
STEP_RETRIES = 2


async def run_step(
    name: str,
    build_prompt: Callable[[str | None], list[dict]],
    validate: Callable[[str], str | None],   # returns error text or None
    json_mode: bool = False,
) -> str:
    """Один шаг пайплайна: запрос → валидация → повтор с указанием ошибки."""
    feedback: str | None = None
    for attempt in range(1, STEP_RETRIES + 2):
        messages = build_prompt(feedback)
        txt = await ai.chat(messages, json_mode=json_mode)
        err = validate(txt)
        if err is None:
            return txt
        log.info("step %s: attempt %d failed: %s", name, attempt, err[:200])
        feedback = (
            f"Предыдущий результат был отклонён проверкой: {err}. "
            f"Исправь и верни новый ответ. Предыдущий ответ:\n{txt[:2000]}"
        )
    raise RuntimeError(f"Шаг «{name}» не прошёл проверку после {STEP_RETRIES + 1} попыток")


def _json_validator(fields: list[str]) -> Callable[[str], str | None]:
    def validate(txt: str) -> str | None:
        try:
            data = json.loads(txt)
        except json.JSONDecodeError:
            return "ответ не является валидным JSON"
        missing = [f for f in fields if f not in data]
        if missing:
            return f"в JSON не хватает полей: {missing}"
        return None
    return validate


async def analyze_document(
    purpose: str,
    doc_text: str,
    context: dict | None = None,
    questions: list[str] | None = None,
) -> dict:
    """Многошаговый анализ документа (PDF лабы и т.п.):
    1) структура и требования; 2) чек-лист что сделать; 3) подводные камни.
    """
    ctx = json.dumps(context or {}, ensure_ascii=False)
    steps = [
        ("structure", [
            {"role": "system", "content": "Ты — методист, разбираешь учебные документы. Отвечай только JSON."},
            {"role": "user", "content": (
                f"Цель анализа: {purpose}\n\nДокумент:\n{doc_text[:24000]}\n\n"
                f"Контекст курса: {ctx}\n\n"
                'Верни JSON: {"summary": "краткое содержание", '
                '"requirements": ["требование 1", ...], '
                '"deliverables": ["что сдать", ...], '
                '"steps": [{"step": 1, "action": "...", "details": "..."}]}'
            )},
        ]),
        ("pitfalls", [
            {"role": "system", "content": "Ты — строгий преподаватель. Отвечай только JSON."},
            {"role": "user", "content": (
                f"Документ:\n{doc_text[:16000]}\n\n"
                'Найди подводные камни и критерии оценки. Верни JSON: '
                '{"pitfalls": ["..."], "grading": ["..."], "common_mistakes": ["..."]}'
            )},
        ]),
    ]
    if questions:
        steps.append(("answers", [
            {"role": "user", "content": (
                f"Документ:\n{doc_text[:16000]}\n\nВопросы:\n"
                + "\n".join(f"- {q}" for q in questions)
                + '\nВерни JSON: {"answers": [{"question": "...", "answer": "..."}]}'
            )},
        ]))
    out: dict[str, Any] = {}
    for name, msgs in steps:
        try:
            data = json.loads(await ai.chat(msgs, json_mode=True))
            out[name] = data
        except Exception as e:  # noqa: BLE001
            log.warning("analyze step %s failed: %s", name, e)
            out[name] = {}
    return out
