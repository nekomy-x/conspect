"""Эвристический парсер расписаний: таблицы/текст/дни недели/время."""
from __future__ import annotations

import re
from datetime import datetime, timedelta

TIME_RE = re.compile(r"(\d{1,2}):(\d{2})\s*[–—-]\s*(\d{1,2}):(\d{2})")
TIME_SINGLE_RE = re.compile(r"\b(\d{1,2}):(\d{2})\b")
DAY_OF_WEEK_RE = re.compile(
    r"(понедельник|вторник|сред[ау]|четверг|пятниц[ау]|суббота|воскресенье)", re.I)
DATE_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4}|\d{2})")
TOMORROW_RE = re.compile(r"\b(завтра|послезавтра)\b", re.I)
TODAY_RE = re.compile(r"\b(сегодня)\b", re.I)

WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]

ONLINE_HINTS = re.compile(r"(zoom|bbb|bigbluebutton|online|онлайн|ссылк|вебинар|meet|синхрон)", re.I)
LECTURE_HINTS = re.compile(r"(лекц|лекция)", re.I)
PRACTICE_HINTS = re.compile(r"(практ|лаб|семинар)", re.I)

# чётность недели: верхняя = чётная, нижняя = нечётная
EVEN_WEEK_RE = re.compile(r"(верхн\w+|чётн\w+|1[-–]я|первая)\s*(недел\w+)?", re.I)
ODD_WEEK_RE = re.compile(r"(нижн\w+|нечётн\w+|2[-–]я|вторая)\s*(недел\w+)?", re.I)


def _norm_wd(wd: str) -> int | None:
    wd = wd.lower()[:6]
    for i, name in enumerate(WEEKDAYS):
        if name.startswith(wd[:4]):
            return i
    return None


def _next_date_for_weekday(wd_idx: int, ref: datetime) -> str:
    days_ahead = (wd_idx - ref.weekday()) % 7
    d = ref + timedelta(days=days_ahead)
    return d.strftime("%Y-%m-%d")


def _kind(title: str) -> str:
    if ONLINE_HINTS.search(title):
        return "online"
    if LECTURE_HINTS.search(title):
        return "lecture"
    if PRACTICE_HINTS.search(title):
        return "practice"
    return "other"


def parse_schedule_text(text: str) -> list[dict]:
    """Парсинг построчно. Распознаёт дату/день недели + время + предмет + место
    + чётность недели (верхняя=even, нижняя=odd)."""
    rows: list[dict] = []
    base_day: str | None = None
    explicit_date: str | None = None
    current_parity: str = "all"

    today = datetime.now()

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        lower = line.lower()

        # чётность недели (верхняя/нижняя) — глобальный флаг до следующей смены
        if EVEN_WEEK_RE.search(line):
            current_parity = "even"
            continue
        if ODD_WEEK_RE.search(line):
            current_parity = "odd"
            continue

        # маркер дня
        dm = DATE_RE.search(line)
        if dm:
            d, m, y = dm.groups()
            y = int(y)
            y = 2000 + y if y < 100 else y
            try:
                explicit_date = datetime(y, int(m), int(d)).strftime("%Y-%m-%d")
            except ValueError:
                pass
        wm = DAY_OF_WEEK_RE.search(line)
        if wm and "расписание" not in lower:
            idx = _norm_wd(wm.group(1))
            if idx is not None:
                base_day = _next_date_for_weekday(idx, today)
        if TOMORROW_RE.search(lower):
            base_day = (today + timedelta(days=1)).strftime("%Y-%m-%d")
        if TODAY_RE.search(lower):
            base_day = today.strftime("%Y-%m-%d")

        tm = TIME_RE.search(line) or _loose_time(line)
        if not tm:
            continue
        day = explicit_date or base_day or today.strftime("%Y-%m-%d")

        # время
        g = tm.groups()
        if len(g) == 4:
            ts_ = f"{int(g[0]):02d}:{g[1]}"
            te = f"{int(g[2]):02d}:{g[3]}"
        else:
            ts_ = f"{int(g[0]):02d}:{g[1]}"
            te = ""
        if ts_ >= "24:00":
            continue

        # предмет и место: чистим строку от времени/дат
        title = line
        for pat in (TIME_RE, DATE_RE, DAY_OF_WEEK_RE, TOMORROW_RE, TODAY_RE):
            title = pat.sub(" ", title)
        title = re.sub(r"[|·—–]+", " ", title)
        title = re.sub(r"\s+", " ", title).strip(" .,-")
        # аудитория: слово после «ауд», «корп», номер с буквами
        place = ""
        pm = re.search(r"(ауд\.?\s*[\w-]+|корпус\s*[\w-]+|А-\d+|Б-\d+|В-\d+|\d{3}[а-я]? auditor)", title, re.I)
        if pm:
            place = pm.group(1)
            title = title.replace(place, " ").strip()
        if len(title) < 2:
            title = "(без названия)"

        rows.append({
            "day": day, "time_start": ts_, "time_end": te,
            "title": re.sub(r"^\d+[).]\s*", "", title)[:120],
            "place": place, "kind": _kind(title),
            "week_parity": current_parity,
        })
        explicit_date = None
    return rows


def _loose_time(line: str):
    m = TIME_SINGLE_RE.search(line)
    if m:
        h, mi = int(m.group(1)), m.group(2)
        if h < 24:
            return m
    return None
