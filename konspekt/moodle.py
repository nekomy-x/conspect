"""Клиент Moodle: сессия по кукам, сканеры страниц, парсинг активностей."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, quote

import httpx
from bs4 import BeautifulSoup

from .config import cfg

log = logging.getLogger("konspect.moodle")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

RUS_MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}


class MoodleAuthError(Exception):
    pass


@dataclass
class Activity:
    cmid: int
    course_id: int
    modname: str
    name: str
    url: str
    section: str | None = None
    extra: dict = field(default_factory=dict)


@dataclass
class AssignmentInfo:
    cmid: int
    course_id: int
    name: str
    url: str
    intro: str = ""
    deadline_ts: int | None = None
    attachments: list[dict] = field(default_factory=list)   # [{url,name}]
    submitted: bool | False = False
    graded: bool | None = None
    text: str = ""                                          # полный текст для ИИ


class MoodleClient:
    """Синхронный по сути, но с асинхронным API (httpx.AsyncClient)."""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self.session_cookie = cfg.moodle_session_cookie
        self.user_name = ""
        self.user_id: int | None = None
        self._last_sesskey: str | None = None

    async def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=cfg.moodle_base,
                headers={"User-Agent": UA, "Accept-Language": "ru,en;q=0.8"},
                cookies={"MoodleSession": self.session_cookie},
                follow_redirects=True,
                max_redirects=5,
                timeout=httpx.Timeout(30.0, connect=15.0),
            )
        else:
            self._client.cookies.set("MoodleSession", self.session_cookie)
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def set_session_cookie(self, cookie: str) -> None:
        self.session_cookie = cookie
        c = await self.client()
        c.cookies.set("MoodleSession", cookie)

    # ------------------------------------------------------------------ core
    async def get_html(self, path: str, **kw) -> str:
        c = await self.client()
        try:
            r = await c.get(path, **kw)
        except httpx.TooManyRedirects:
            # бесконечный цикл редиректов = мёртвая сессия → пробуем авто-логин
            from .auth import ensure_session
            ok, _ = await ensure_session()
            if ok:
                c = await self.client()
                try:
                    r = await c.get(path, **kw)
                except httpx.TooManyRedirects:
                    raise MoodleAuthError("Сессия Moodle истекла (цикл редиректов)")
                r.raise_for_status()
                html = r.text
                if not self._is_logged_out(html):
                    self._extract_user(html)
                    sk = self.sesskey(html)
                    if sk:
                        self._last_sesskey = sk
                    return html
            raise MoodleAuthError("Сессия Moodle истекла (авто-логин не помог)")
        r.raise_for_status()
        html = r.text
        if self._is_logged_out(html):
            # пробуем авто-логин один раз
            from .auth import ensure_session
            ok, _ = await ensure_session()
            if ok:
                c = await self.client()
                try:
                    r = await c.get(path, **kw)
                except httpx.TooManyRedirects:
                    raise MoodleAuthError("Сессия Moodle истекла (цикл редиректов)")
                r.raise_for_status()
                html = r.text
                if not self._is_logged_out(html):
                    self._extract_user(html)
                    sk = self.sesskey(html)
                    if sk:
                        self._last_sesskey = sk
                    return html
            raise MoodleAuthError("Сессия Moodle истекла (авто-логин не помог)")
        self._extract_user(html)
        # кэшируем sesskey для AJAX-вызовов
        sk = self.sesskey(html)
        if sk:
            self._last_sesskey = sk
        return html

    async def get_soup(self, path: str, **kw) -> BeautifulSoup:
        return BeautifulSoup(await self.get_html(path, **kw), "html.parser")

    def _is_logged_out(self, html: str) -> bool:
        return 'name="password"' in html and "loginbtn" in html

    def _extract_user(self, html: str) -> None:
        m = re.search(r'profile\.php\?id=(\d+)"[^>]*>([^<]+)</a>', html)
        if m:
            self.user_id = int(m.group(1))
            self.user_name = m.group(2).strip()

    async def is_alive(self) -> tuple[bool, str | None]:
        """Проверка сессии. Возвращает (жива, причина_если_нет).

        Правильная проверка — GET /my/: живая кука → 200 с кабинетом,
        битая → 303-редирект на /login/index.php.
        Таймауты/сети — не смерть (Moodle лежит ≠ кука протухла)."""
        try:
            c = await self.client()
            c.cookies.set("MoodleSession", self.session_cookie)
            r = await c.get("/my/", follow_redirects=False)
            if r.status_code == 200:
                return True, None
            if r.status_code in (301, 302, 303):
                loc = r.headers.get("location", "")
                if "login" in loc:
                    return False, "редирект на /login (кука протухла)"
                # редирект не на логин — возможно норма
                return True, None
            return True, None
        except Exception as e:  # noqa: BLE001
            log.warning("alive check failed: %s", e)
            return True, None  # сеть/таймаут — не смерть

    async def alive_simple(self) -> bool:
        ok, _ = await self.is_alive()
        return ok

    def sesskey(self, html: str) -> str | None:
        m = re.search(r'"sesskey"\s*:\s*"(\w+)"', html)
        return m.group(1) if m else None

    # ------------------------------------------------------------- dashboard
    async def fetch_courses(self) -> list[dict]:
        """Курсы с дашборда /my/."""
        soup = await self.get_soup("/my/")
        out, seen = [], set()
        for a in soup.select("a[href*='course/view.php?id=']"):
            href = a.get("href", "")
            m = re.search(r"id=(\d+)", href)
            if not m:
                continue
            cid = int(m.group(1))
            name = a.get_text(" ", strip=True)
            name = re.sub(r"\s+", " ", name)
            # у имени бывает хвост типа "2УИ-221 ..."

            if cid in seen or not name:
                continue
            seen.add(cid)
            out.append({"id": cid, "name": name, "url": href})
        return out

    # ---------------------------------------------------------------- course
    async def fetch_activities(self, course_id: int) -> list[Activity]:
        """Активности курса со страницы курса (имена, секции, ссылки).

        Использует data-activityname / data-sectionname (тема course-format,
        надёжнее, чем текст ссылок с мусором «Выбрать секцию»).
        """
        soup = await self.get_soup(f"/course/view.php?id={course_id}")
        acts: list[Activity] = []
        seen: set[int] = set()

        for li in soup.select("li.activity[data-id]"):
            try:
                cmid = int(li.get("data-id", "0"))
            except ValueError:
                continue
            if not cmid or cmid in seen:
                continue
            a = li.select_one("a.aalink") or li.select_one("span.instancename a") or li.select_one("a[href]")
            if not a:
                continue
            href = a.get("href", "")
            m = re.search(r"/mod/([a-z0-9_]+)/view\.php\?id=(\d+)", href)
            if not m:
                continue
            modname = m.group(1)
            card = li.select_one("[data-activityname]") or li
            name = card.get("data-activityname", "") or ""
            if not name:
                inst = li.select_one("span.instancename")
                name = inst.get_text(" ", strip=True) if inst else a.get_text(" ", strip=True)
            name = re.sub(r"\s+", " ", name)
            # секция: ближайший родитель с data-sectionname (li.section / li.course-section)
            sec = li.find_parent(attrs={"data-sectionname": True})
            secname = sec.get("data-sectionname") if sec else None
            acts.append(Activity(
                cmid=cmid, course_id=course_id, modname=modname, name=name,
                url=href, section=secname,
            ))
            seen.add(cmid)
        return acts

    # --------------------------------------------------------------- AJAX API
    async def bbb_meeting_info(self, cmid: int, bbb_id: int) -> dict | None:
        """Живое состояние конференции через Moodle AJAX (как это делает JS плеер):
        statusrunning, canjoin, joinurl, openingtime. Требует живой sesskey."""
        html = await self.get_html(f"/mod/bigbluebuttonbn/view.php?id={cmid}")
        sesskey = self.sesskey(html)
        if not sesskey:
            return None
        payload = [{"index": 0, "methodname": "mod_bigbluebuttonbn_meeting_info",
                    "args": {"bigbluebuttonbnid": bbb_id, "groupid": 0,
                             "updatecache": True}}]
        c = await self.client()
        r = await c.post(
            f"/lib/ajax/service.php?sesskey={sesskey}&info=mod_bigbluebuttonbn_meeting_info",
            json=payload, headers={"Content-Type": "application/json"})
        try:
            data = r.json()
            if data and not data[0].get("error"):
                out = data[0]["data"]
                # &amp; в joinurl
                out["joinurl"] = out.get("joinurl", "").replace("&amp;", "&")
                return out
        except Exception:  # noqa: BLE001
            pass
        return None

    # ------------------------------------------------------------------- BBB
    async def fetch_bbb(self, cmid: int) -> dict:
        """Страница BBB: время начала, join-кнопка, записи.

        Возвращает dict:
          opening_ts, join_url, recordings: [{playback_url,title}],
          presentation: [url], room: {bbb_id}, html
        """
        html = await self.get_html(f"/mod/bigbluebuttonbn/view.php?id={cmid}")
        soup = BeautifulSoup(html, "html.parser")
        out: dict[str, Any] = {"cmid": cmid, "html": html}

        # bbb room
        room = soup.select_one("[data-bbb-id]")
        out["bbb_id"] = int(room["data-bbb-id"]) if room and room.get("data-bbb-id") else None

        # заголовок: h3 в div[role=main] или data-activityname
        main = soup.select_one("div[role='main']")
        h3 = main.select_one("h2, h3") if main else soup.select_one("h2")
        info_el = soup.select_one("[data-activityname]")
        out["title"] = (h3.get_text(" ", strip=True) if h3 else
                        (info_el.get("data-activityname", "") if info_el else f"BBB {cmid}"))

        # даты начала/окончания из блока activity-dates
        for row in soup.select("[data-region='activity-dates'] div"):
            txt = row.get_text(" ", strip=True)
            if "Открыто с" in txt or "Участвовать с" in txt or "начал" in txt.lower():
                out["opening_text"] = txt
            elif "до" in txt.lower() or "Срок" in txt:
                out["closing_text"] = txt

        # встреча уже идёт? кнопки в HTML нет (JS) — только через AJAX
        if out.get("bbb_id"):
            info = await self.bbb_meeting_info(cmid, out["bbb_id"])
            if info:
                out["meeting_info"] = info
                out["join_url"] = info.get("joinurl")
                out["running"] = bool(info.get("statusrunning"))
                out["canjoin"] = bool(info.get("canjoin"))
                if info.get("openingtime"):
                    out["opening_ts"] = int(info["openingtime"])

        # кнопка присоединиться — из meeting_info (AJAX), в HTML её нет
        join = out.get("join_url")
        out["join_url"] = join

        # записи: через Moodle AJAX (в HTML их НЕТ — рендерится JS-плеером)
        out["recordings"] = await self._fetch_recordings_ajax(cmid, out.get("bbb_id"))
        # legacy-фоллбэк: ссылки playback в HTML (старые темы)
        if not out["recordings"]:
            for a in soup.select("a[href*='playback']"):
                href = a.get("href", "")
                title = a.get_text(" ", strip=True) or href
                out["recordings"].append(
                    {"playback_url": urljoin(cfg.moodle_base, href), "title": title})

        # презентация лекции
        pres = [urljoin(cfg.moodle_base, a.get("href", "")) for a in
                soup.select("a[href*='presentationdefault'], a[href*='mod_bigbluebuttonbn']")]
        out["presentation"] = pres
        return out

    async def _fetch_recordings_ajax(self, cmid: int, bbb_id: int | None) -> list[dict]:
        """Записи конференции через Moodle AJAX (как JS-плеер).

        URL playback Moodle (bbb_view.php) — НЕ прямой bbb.edu.vsu.ru.
        Для скачивания структуры записи нужен реальный ID записи:
        bbb_view.php редиректит на playback/<hash>-<ts>. Делаем HEAD.
        """
        if not bbb_id:
            return []
        sesskey = self._last_sesskey
        if not sesskey:
            # получим из любой страницы
            html = await self.get_html("/my/")
            sesskey = self.sesskey(html)
        if not sesskey:
            return []
        c = await self.client()
        r = await c.post(
            "/lib/ajax/service.php?sesskey=" + sesskey,
            json=[{"index": 0, "methodname": "mod_bigbluebuttonbn_get_recordings",
                   "args": {"bigbluebuttonbnid": bbb_id, "groupid": 0,
                            "tools": "protect,unprotect,publish,unpublish,delete"}}],
            headers={"Content-Type": "application/json"})
        recs: list[dict] = []
        try:
            resp = r.json()
            if resp and not resp[0].get("error"):
                raw = resp[0]["data"]["tabledata"]["data"]
                rows = json.loads(raw) if isinstance(raw, str) else raw
                for row in rows:
                    pb = row.get("playback", "")
                    rid = re.search(r'data-recordingid="(\d+)"', pb)
                    if not rid:
                        continue
                    title = row.get("recording", "") or "запись"
                    # реальный playback-URL из поля preview:
                    # в preview НЕСКОЛЬКО rid: первый = meeting id (РАБОЧИЙ,
                    # файлы доступны на bbb.edu.vsu.ru), второй = presentation
                    # id (недоступен напрямую). Берём ПЕРВЫЙ.
                    preview = row.get("preview", "")
                    real_url = None
                    m_all = re.findall(
                        r'/presentation/([0-9a-f]{40}-\d{13})', preview)
                    if m_all:
                        real_url = (f"{cfg.bbb_base}/playback/presentation/2.3/"
                                    f"{m_all[0]}")
                    else:
                        # фоллбэк: резолвим редирект bbb_view
                        moodle_url = (f"{cfg.moodle_base}/mod/bigbluebuttonbn/bbb_view.php"
                                      f"?action=play&bn={bbb_id}&rid={rid.group(1)}"
                                      f"&rtype=presentation")
                        real_url = moodle_url
                        try:
                            rr = await c.head(moodle_url, follow_redirects=False)
                            loc = rr.headers.get("location", "")
                            if loc and "playback" in loc:
                                real_url = loc if loc.startswith("http") else urljoin(cfg.moodle_base, loc)
                        except Exception:  # noqa: BLE001
                            pass
                    ts_ms = row.get("date")
                    try:
                        import datetime as _dt
                        if ts_ms:
                            title = (_dt.datetime.fromtimestamp(int(ts_ms) / 1000)
                                     .strftime("%d.%m %H:%M ") + title)
                    except Exception:  # noqa: BLE001
                        pass
                    recs.append({"playback_url": real_url, "title": title,
                                 "rid": rid.group(1),
                                 "duration_min": row.get("duration")})
        except Exception as e:  # noqa: BLE001
            log.warning("recordings ajax: %s", e)
        return recs

    # ------------------------------------------------------------- assignment
    async def fetch_assignment(self, cmid: int) -> AssignmentInfo:
        html = await self.get_html(f"/mod/assign/view.php?id={cmid}")
        soup = BeautifulSoup(html, "html.parser")
        info = AssignmentInfo(cmid=cmid, course_id=0, name="", url=f"{cfg.moodle_base}/mod/assign/view.php?id={cmid}")

        # intro (описание задания)
        desc = (soup.select_one(".activity-description")
                or soup.select_one("div[role='main'] .box.intro, #intro")
                or soup.select_one("div[role='main'] .no-overflow"))
        if desc:
            info.intro = re.sub(r"\s+", " ", desc.get_text(" ", strip=True))

        # заголовок: data-activityname или первый h2/h3 ДО блока состояния
        info_el = soup.select_one("[data-activityname]")
        main = soup.select_one("div[role='main']")
        name = ""
        if main:
            for h in main.select("h2, h3"):
                txt = h.get_text(" ", strip=True)
                if txt and "Состояние ответа" not in txt and "Состояние оценивания" not in txt:
                    name = txt
                    break
        if not name and info_el and info_el.get("data-activityname"):
            name = info_el["data-activityname"]
        info.name = name or f"assign {cmid}"

        # вложения описания
        for a in soup.select("a[href*='pluginfile.php'][href*='mod_assign']"):
            href = urljoin(cfg.moodle_base, a.get("href", ""))
            name = a.get_text(" ", strip=True) or href.rsplit("/", 1)[-1]
            info.attachments.append({"url": href, "name": re.sub(r"\?.*$", "", name)})

        # курс
        m = re.search(r"course/view\.php\?id=(\d+)", html)
        if m:
            info.course_id = int(m.group(1))

        # дедлайн: ищем таблицу состояния или текст «до ...»
        info.deadline_ts = self._find_deadline(soup, info.intro)

        # состояние сдачи
        tbl = soup.get_text(" ", strip=True)
        info.submitted = ("Отправлено для оценивания" in tbl) or ("Отправлено" in tbl and "попытк" in tbl.lower())
        m = re.search(r"Оценк[аи][^а-я]{0,40}", tbl)

        info.text = self._full_text(soup)
        return info

    def _find_deadline(self, soup: BeautifulSoup, intro: str) -> int | None:
        # приоритет: блок «Срок сдачи:» на странице
        for row in soup.select("[data-region='activity-dates'] div"):
            txt = row.get_text(" ", strip=True)
            if "Срок сдачи" in txt or "закрыт" in txt.lower():
                ts = parse_ru_datetime(txt)
                if ts:
                    return ts
        txt = soup.get_text(" ", strip=True)
        for pat in (
            r"до\s+(\d{1,2})\.(\d{1,2})\.(\d{2,4})",
            r"до\s+(\d{1,2})\s+([а-яё]+)\s+(\d{4})",
        ):
            m = re.search(pat, txt + " " + intro, re.I)
            if m:
                return self._to_ts(m)
        return None

    def _to_ts(self, m: re.Match) -> int:
        import datetime as dt
        d, mon_s, y = m.groups()
        d = int(d)
        if mon_s.isdigit():
            mon = int(mon_s)
        else:
            mon = RUS_MONTHS.get(mon_s.lower())
        y = int(y)
        year = 2000 + y if y < 100 else y
        if mon is None:
            return 0
        try:
            return int(dt.datetime(year, mon, d).timestamp())
        except ValueError:
            return 0

    def _full_text(self, soup: BeautifulSoup) -> str:
        main = soup.select_one("div[role='main'], #region-main, section#region-main")
        node = main or soup.body or soup
        txt = node.get_text("\n", strip=True)
        return re.sub(r"\n{3,}", "\n\n", txt)

    # ---------------------------------------------------------------- files
    async def download(self, url: str, dest: str) -> str:
        c = await self.client()
        if url.startswith("/"):
            url = cfg.moodle_base + url
        r = await c.get(url)
        r.raise_for_status()
        with open(dest, "wb") as f:
            f.write(r.content)
        return dest


# --- утилита: парсинг времени начала из текста вида "24 сентября 13:28" ---
def parse_ru_datetime(text: str) -> int | None:
    m = re.search(r"(\d{1,2})\s+([а-яё]+)[,]?\s*(\d{4})?[,.]?\s*(\d{1,2}):(\d{2})", text, re.I)
    if not m:
        return None
    d, mon_s, y, hh, mm = m.groups()
    mon = RUS_MONTHS.get(mon_s.lower())
    if not mon:
        return None
    import datetime as dt
    year = int(y) if y else time.localtime().tm_year
    try:
        return int(dt.datetime(year, mon, int(d), int(hh), int(mm)).timestamp())
    except ValueError:
        return None


moodle = MoodleClient()
