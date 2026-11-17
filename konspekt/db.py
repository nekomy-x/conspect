"""SQLite-хранилище: состояние мудла, напоминания, расписание, память курса."""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .config import cfg

SCHEMA = """
CREATE TABLE IF NOT EXISTS courses (
    id INTEGER PRIMARY KEY,
    name TEXT,
    url TEXT,
    first_seen INTEGER,
    last_seen INTEGER
);
CREATE TABLE IF NOT EXISTS activities (
    id INTEGER PRIMARY KEY,          -- cmid
    course_id INTEGER,
    modname TEXT,                    -- bigbluebuttonbn / assign / resource / forum ...
    name TEXT,
    url TEXT,
    section TEXT,
    first_seen INTEGER,
    last_seen INTEGER,
    extra TEXT                       -- json: сроки, состояние, и т.д.
);
CREATE TABLE IF NOT EXISTS recordings (
    playback_url TEXT PRIMARY KEY,
    activity_id INTEGER,
    course_id INTEGER,
    title TEXT,
    first_seen INTEGER,
    processed INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS bbb_meetings (           -- мониторинг «кнопка присоединиться»
    activity_id INTEGER PRIMARY KEY,
    course_id INTEGER,
    name TEXT,
    url TEXT,
    opening_ts INTEGER,              -- время начала из расписания/страницы
    join_url TEXT,
    started INTEGER DEFAULT 0,
    announced_upcoming INTEGER DEFAULT 0,
    announced_start INTEGER DEFAULT 0,
    last_check INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS assignments (
    activity_id INTEGER PRIMARY KEY,
    course_id INTEGER,
    name TEXT,
    url TEXT,
    deadline_ts INTEGER,
    intro TEXT,
    attachment_paths TEXT,            -- json list
    state TEXT,                      -- draft/ready/submitted
    answer_path TEXT,
    answer TEXT,
    what_to_submit TEXT,             -- json list
    verdict TEXT,                    -- вердикт последней проверки
    deadline_notified TEXT           -- json {"24h":1,"3h":1}
);
CREATE TABLE IF NOT EXISTS memories (               -- «память» контекста курса
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS schedule (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT,                        -- ISO date
    time_start TEXT,                 -- HH:MM
    time_end TEXT,
    title TEXT,
    place TEXT,
    kind TEXT,                       -- lecture/practice/online/other
    raw TEXT,
    source TEXT,
    notified TEXT                    -- json flags
);
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER,
    due_ts INTEGER NOT NULL,
    text TEXT NOT NULL,
    kind TEXT DEFAULT 'plain',
    payload TEXT,
    sent INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS watchlist (
    course_id INTEGER PRIMARY KEY,
    enabled INTEGER DEFAULT 1,
    note TEXT
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


class DB:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else cfg.db_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._conn() as c:
            c.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=5000")
        return c

    # ---------- generic ----------
    def kv_get(self, key: str, default: Any = None) -> Any:
        with self._lock, self._conn() as c:
            r = c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(r["value"]) if r else default

    def kv_set(self, key: str, value: Any) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO kv(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value, ensure_ascii=False)),
            )

    # ---------- courses / activities ----------
    def upsert_course(self, cid: int, name: str, url: str, now: int) -> bool:
        """returns True если курс новый"""
        with self._lock, self._conn() as c:
            r = c.execute("SELECT id FROM courses WHERE id=?", (cid,)).fetchone()
            c.execute(
                "INSERT INTO courses(id,name,url,first_seen,last_seen) VALUES(?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name, last_seen=excluded.last_seen",
                (cid, name, url, now, now),
            )
        return r is None

    def upsert_activity(
        self, cmid: int, course_id: int, modname: str, name: str, url: str,
        section: str | None, now: int, extra: dict | None = None,
    ) -> bool:
        """returns True если активность новая"""
        with self._lock, self._conn() as c:
            r = c.execute("SELECT id FROM activities WHERE id=?", (cmid,)).fetchone()
            c.execute(
                "INSERT INTO activities(id,course_id,modname,name,url,section,first_seen,last_seen,extra) "
                "VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET course_id=excluded.course_id, modname=excluded.modname, "
                "name=excluded.name, url=excluded.url, section=excluded.section, "
                "last_seen=excluded.last_seen, extra=excluded.extra",
                (cmid, course_id, modname, name, url, section, now, now,
                 json.dumps(extra or {}, ensure_ascii=False)),
            )
        return r is None

    def courses(self) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM courses ORDER BY name")]

    def activities(self, modname: str | None = None, course_id: int | None = None) -> list[dict]:
        q, a = "SELECT * FROM activities WHERE 1=1", []
        if modname:
            q += " AND modname=?"; a.append(modname)
        if course_id:
            q += " AND course_id=?"; a.append(course_id)
        q += " ORDER BY course_id, id"
        with self._conn() as c:
            return [dict(r) for r in c.execute(q, a)]

    def activity(self, cmid: int) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM activities WHERE id=?", (cmid,)).fetchone()
        return dict(r) if r else None

    # ---------- recordings ----------
    def add_recording(self, playback_url: str, activity_id: int, course_id: int, title: str, now: int) -> bool:
        with self._lock, self._conn() as c:
            r = c.execute("SELECT playback_url, processed FROM recordings WHERE playback_url=?",
                          (playback_url,)).fetchone()
            if r is not None:
                # существующую не сбрасываем в processed=0 (иначе вечный цикл)
                c.execute("UPDATE recordings SET title=?, activity_id=?, course_id=? "
                          "WHERE playback_url=?",
                          (title, activity_id, course_id, playback_url))
            else:
                c.execute(
                    "INSERT INTO recordings(playback_url,activity_id,course_id,title,first_seen,processed) "
                    "VALUES(?,?,?,?,?,0)",
                    (playback_url, activity_id, course_id, title, now),
                )
        return r is None

    def unprocessed_recordings(self) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM recordings WHERE processed=0")]

    def mark_recording(self, playback_url: str, processed: int = 1) -> None:
        with self._conn() as c:
            c.execute("UPDATE recordings SET processed=? WHERE playback_url=?", (processed, playback_url))

    # ---------- bbb meetings ----------
    def upsert_meeting(self, activity_id: int, **kw) -> bool:
        with self._lock, self._conn() as c:
            r = c.execute("SELECT * FROM bbb_meetings WHERE activity_id=?", (activity_id,)).fetchone()
            if r is None:
                cols = ["activity_id"] + list(kw)
                vals = [activity_id] + list(kw.values())
                c.execute(
                    f"INSERT INTO bbb_meetings({','.join(cols)}) VALUES({','.join('?' * len(vals))})",
                    vals,
                )
                return True
            sets = ", ".join(f"{k}=?" for k in kw)
            c.execute(
                f"UPDATE bbb_meetings SET {sets}, last_check=strftime('%s','now') "
                f"WHERE activity_id=?",
                list(kw.values()) + [activity_id],
            )
        return False

    def meetings(self) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM bbb_meetings")]

    def meeting(self, activity_id: int) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM bbb_meetings WHERE activity_id=?", (activity_id,)).fetchone()
        return dict(r) if r else None

    def set_meeting(self, activity_id: int, **kw) -> None:
        sets = ", ".join(f"{k}=?" for k in kw)
        with self._conn() as c:
            c.execute(f"UPDATE bbb_meetings SET {sets} WHERE activity_id=?",
                      list(kw.values()) + [activity_id])

    # ---------- assignments ----------
    def upsert_assignment(self, activity_id: int, **kw) -> bool:
        with self._lock, self._conn() as c:
            r = c.execute("SELECT activity_id FROM assignments WHERE activity_id=?", (activity_id,)).fetchone()
            if r is None:
                cols = ["activity_id"] + list(kw)
                vals = [activity_id] + list(kw.values())
                c.execute(f"INSERT INTO assignments({','.join(cols)}) VALUES({','.join('?' * len(vals))})", vals)
                return True
            sets = ", ".join(f"{k}=?" for k in kw)
            c.execute(f"UPDATE assignments SET {sets} WHERE activity_id=?", list(kw.values()) + [activity_id])
        return False

    def assignments(self) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM assignments ORDER BY deadline_ts")]

    def assignment(self, activity_id: int) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM assignments WHERE activity_id=?", (activity_id,)).fetchone()
        return dict(r) if r else None

    def update_assignment(self, activity_id: int, **kw) -> None:
        sets = ", ".join(f"{k}=?" for k in kw)
        with self._conn() as c:
            c.execute(f"UPDATE assignments SET {sets} WHERE activity_id=?", list(kw.values()) + [activity_id])

    # ---------- schedule ----------
    def replace_schedule(self, rows: list[dict], source: str = "manual",
                         weeks: int = 16) -> int:
        """Сохраняет пары как ЕЖЕНЕДЕЛЬНОЕ расписание: размножает по `weeks`
        неделям вперёд от базовой недели (у каждой пары).
        week_parity: 'all' | 'even' | 'odd' — чётная/нечётная неделя."""
        from datetime import datetime, timedelta
        with self._lock, self._conn() as c:
            c.execute("DELETE FROM schedule WHERE source=?", (source,))
            n = 0
            for r in rows:
                try:
                    base = datetime.strptime(r["day"], "%Y-%m-%d")
                except ValueError:
                    continue
                parity = r.get("week_parity", "all")
                for w in range(weeks):
                    d = base + timedelta(weeks=w)
                    # фильтруем по чётности недели
                    if parity == "even" and d.isocalendar()[1] % 2 != 0:
                        continue
                    if parity == "odd" and d.isocalendar()[1] % 2 != 1:
                        continue
                    ds = d.strftime("%Y-%m-%d")
                    c.execute(
                        "INSERT INTO schedule(day,time_start,time_end,title,place,kind,raw,source,notified,week_parity) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (ds, r.get("time_start"), r.get("time_end"), r["title"],
                         r.get("place"), r.get("kind", "other"),
                         json.dumps(r, ensure_ascii=False), source, "{}", parity),
                    )
                    n += 1
        return n

    def clear_schedule(self, day: str | None = None, source: str = "manual") -> None:
        with self._conn() as c:
            if day:
                c.execute("DELETE FROM schedule WHERE source=? AND day=?", (source, day))
            else:
                c.execute("DELETE FROM schedule WHERE source=?", (source,))

    def schedule_day(self, day: str) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM schedule WHERE day=? ORDER BY time_start", (day,)
            ).fetchall()
        return [dict(r) for r in rows]

    def schedule_day_parity(self, day: str) -> tuple[list[dict], str]:
        """Пары на день с учётом чётности недели.
        Возвращает (пары, 'even'|'odd')."""
        from datetime import datetime
        d = datetime.strptime(day, "%Y-%m-%d")
        iso_week = d.isocalendar()[1]
        parity = "even" if iso_week % 2 == 0 else "odd"
        rows = self.schedule_day(day)
        # фильтруем: all + совпадающая чётность
        filtered = [r for r in rows
                    if r.get("week_parity", "all") in ("all", parity)]
        return filtered, parity

    def schedule_upcoming(self, from_day: str) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM schedule WHERE day>=? ORDER BY day, time_start", (from_day,)
            ).fetchall()
        return [dict(r) for r in rows]

    def set_schedule_notified(self, row_id: int, flag: str) -> None:
        row = None
        with self._conn() as c:
            row = c.execute("SELECT notified FROM schedule WHERE id=?", (row_id,)).fetchone()
            if row:
                data = json.loads(row["notified"] or "{}")
                data[flag] = 1
                c.execute("UPDATE schedule SET notified=? WHERE id=?",
                          (json.dumps(data), row_id))

    # ---------- watchlist ----------
    def watch(self, course_id: int, enabled: bool = True, note: str = "") -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO watchlist(course_id,enabled,note) VALUES(?,?,?) "
                "ON CONFLICT(course_id) DO UPDATE SET enabled=excluded.enabled, note=excluded.note",
                (course_id, int(enabled), note),
            )

    def unwatch(self, course_id: int) -> None:
        self.watch(course_id, enabled=False)

    def is_watched(self, course_id: int) -> bool:
        with self._conn() as c:
            r = c.execute("SELECT enabled FROM watchlist WHERE course_id=?", (course_id,)).fetchone()
        return True if r is None else bool(r["enabled"])   # по умолчанию все под слежкой

    def watchlist(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT w.course_id, w.enabled, w.note, co.name "
                "FROM watchlist w LEFT JOIN courses co ON co.id=w.course_id").fetchall()
        return [dict(r) for r in rows]

    # ---------- memories ----------
    def memory(self, course_id: int) -> dict:
        key = f"course:{course_id}"
        v = self.kv_get(key, {})
        return v if isinstance(v, dict) else {}

    def memory_append(self, course_id: int, **kv) -> None:
        key = f"course:{course_id}"
        data = self.kv_get(key, {}) or {}
        data.update(kv)
        self.kv_set(key, data)

    def memory_push_list(self, course_id: int, list_name: str, item: Any, limit: int = 40) -> None:
        data = self.kv_get(f"course:{course_id}", {}) or {}
        lst = data.setdefault(list_name, [])
        lst.append(item)
        data[list_name] = lst[-limit:]
        self.kv_set(f"course:{course_id}", data)

    # ---------- reminders ----------
    def add_reminder(self, chat_id: int, due_ts: int, text: str, kind: str = "plain", payload: str | None = None) -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO reminders(chat_id,due_ts,text,kind,payload,sent) VALUES(?,?,?,?,?,0)",
                (chat_id, due_ts, text, kind, payload),
            )
            return cur.lastrowid or 0

    def due_reminders(self, now_ts: int, limit: int = 20) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM reminders WHERE sent=0 AND due_ts<=? ORDER BY due_ts LIMIT ?",
                (now_ts, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_reminder(self, rid: int, sent: int = 1) -> None:
        with self._conn() as c:
            c.execute("UPDATE reminders SET sent=? WHERE id=?", (sent, rid))

    def reminders(self, chat_id: int | None = None) -> list[dict]:
        with self._conn() as c:
            if chat_id:
                rows = c.execute("SELECT * FROM reminders WHERE sent=0 ORDER BY due_ts").fetchall()
            else:
                rows = c.execute("SELECT * FROM reminders WHERE sent=0 ORDER BY due_ts").fetchall()
        return [dict(r) for r in rows]

    def delete_reminder(self, rid: int) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM reminders WHERE id=?", (rid,))


db = DB()
