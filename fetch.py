"""Скачивание ассетов BigBlueButton: metadata, shapes, slides, video, audio."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from html import unescape

import requests
from tqdm import tqdm

BBB_BASE = "https://bbb.edu.vsu.ru/presentation"

# ── Парсинг URL ────────────────────────────────────────────────────────────

RE_RECORD_ID = re.compile(
    r"(?:playback/presentation/(?:\d+\.\d+/))?([0-9a-f]{40}-\d+)"
)
RE_MOODLE_ID = re.compile(r"bigbluebuttonbn/view\.php\?id=(\d+)")


def extract_record_id(url: str) -> str:
    """Извлекает recordId из BBB URL или Moodle URL."""
    # 1) Прямая ссылка на playback / presentation
    m = RE_RECORD_ID.search(url)
    if m:
        return m.group(1)

    # 2) Moodle URL → редиректим, достаём recordId
    if "edu.vsu.ru" in url and "bigbluebuttonbn" in url:
        from config import load_config
        cfg = load_config()
        cookies = _load_cookies(cfg.moodle_cookies) if cfg.moodle_cookies else None
        resp = requests.get(url, cookies=cookies, allow_redirects=True, timeout=30)
        m2 = RE_RECORD_ID.search(resp.url) or RE_RECORD_ID.search(resp.text)
        if m2:
            return m2.group(1)
        raise ValueError(f"Не удалось найти recordId на странице Moodle: {url}")

    raise ValueError(f"Не удалось извлечь recordId из URL: {url}")


def _load_cookies(path: str) -> dict:
    """Загружает cookies.txt (Netscape формат) в dict."""
    cookies = {}
    p = Path(path)
    if not p.exists():
        return cookies
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            cookies[parts[5]] = parts[6]
    return cookies


# ── Сессия HTTP ─────────────────────────────────────────────────────────────

def _session(cookies: Optional[dict] = None) -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = "BBB-Lectures/1.0"
    if cookies:
        s.cookies.update(cookies)
    return s


def _get(sess: requests.Session, url: str, out: Path, desc: str = "") -> bool:
    """Скачивает файл с прогресс-баром и кешем. Возвращает True если скачан."""
    if out.exists() and out.stat().st_size > 0:
        return False  # уже скачано

    resp = sess.get(url, stream=True, timeout=120)
    resp.raise_for_status()
    total = int(resp.headers.get("content-length", 0))
    out.parent.mkdir(parents=True, exist_ok=True)

    label = desc or out.name
    with open(out, "wb") as f, tqdm(
        total=total, unit="B", unit_scale=True, unit_divisor=1024,
        desc=label, leave=False,
    ) as pbar:
        for chunk in resp.iter_content(chunk_size=65536):
            f.write(chunk)
            pbar.update(len(chunk))
    return True


# ── Парсинг shapes.svg ──────────────────────────────────────────────────────

@dataclass
class SlideInfo:
    index: int          # 1-based
    start_sec: float
    svg_href: str       # href из shapes.svg (относительный путь к SVG слайда)
    folder_id: str = ""  # ID презентации (папка с SVG/текстом)


def parse_shapes_svg(svg_text: str) -> list[SlideInfo]:
    """Извлекает список слайдов с таймкодами из shapes.svg."""
    slides: list[SlideInfo] = []
    # shapes.svg использует xlink:href в <image>, формат:
    # <image xlink:href="presentation/<folder>/svgs/slide<N>.svg" ... class="slide" in="<seconds>">
    # Причём также класс "slide" у <g> обёртки
    RE_IMG = re.compile(
        r'<image[^>]*href="([^"]+)"[^>]*class="slide"[^>]*in="([\d.]+)"',
    )
    RE_IMG_ALT = re.compile(
        r'class="slide"[^>]*in="([\d.]+)"[^>]*href="([^"]+)"',
    )

    for m in RE_IMG.finditer(svg_text):
        href, t = m.group(1), float(m.group(2))
        slides.append((t, href))

    if not slides:
        for m in RE_IMG_ALT.finditer(svg_text):
            t, href = float(m.group(1)), m.group(2)
            slides.append((t, href))

    # Альтернативный парсинг: ET (если regex не сработал — например, формат <g> вместо <image>)
    if not slides:
        try:
            root = ET.fromstring(svg_text.encode() if isinstance(svg_text, str) else svg_text)
            ns = {"svg": "http://www.w3.org/2000/svg", "xlink": "http://www.w3.org/1999/xlink"}
            for img in root.iter(f"{{{ns['svg']}}}image"):
                href = img.get(f"{{{ns['xlink']}}}href") or img.get("href") or img.get(f"{{{ns['xlink']}}}href")
                in_val = img.get("in") or img.get("data-in")
                if href and in_val:
                    slides.append((float(in_val), href))
        except ET.ParseError:
            pass

    # Сортируем по таймкоду, извлекаем folder_id
    result = []
    seen = set()
    folder_ids = set()
    for idx, (t, href) in enumerate(sorted(slides, key=lambda x: x[0]), start=1):
        # href: "presentation/<folderId>/svgs/slide1.svg"
        parts = href.split("/")
        if len(parts) >= 2 and parts[0] == "presentation":
            folder_id = parts[1]
            folder_ids.add(folder_id)
        else:
            folder_id = ""
        key = (t, href)
        if key in seen:
            continue
        seen.add(key)
        result.append(SlideInfo(index=idx, start_sec=t, svg_href=href, folder_id=folder_id))

    # Присваиваем folder_id всем, если он у большинства
    if folder_ids:
        dominant = max(folder_ids, key=lambda f: sum(1 for s in result if s.folder_id == f))
        for s in result:
            if not s.folder_id:
                s.folder_id = dominant

    return result


# ── Парсинг metadata.xml ────────────────────────────────────────────────────

@dataclass
class RecordingMeta:
    record_id: str = ""
    meeting_name: str = ""
    course_name: str = ""
    participants: int = 0
    duration_ms: int = 0
    start_time: int = 0
    end_time: int = 0

    @property
    def duration_min(self) -> float:
        return self.duration_ms / 60_000 if self.duration_ms else 0

    @property
    def slug(self) -> str:
        """Короткое имя файла для конспекта."""
        name = self.meeting_name or self.record_id[:8]
        # Убираем неразрешённые символы
        return re.sub(r'[^\w\s\-а-яА-ЯёЁa-zA-Z]+', '', name)[:80].strip()


def parse_metadata(xml_text: str) -> RecordingMeta:
    """Парсит metadata.xml → RecordingMeta."""
    meta = RecordingMeta()
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return meta

    meta.record_id = (root.findtext("id") or "").strip()
    meta.participants = int(root.findtext("participants") or "0")
    meta.start_time = int(root.findtext("start_time") or "0")
    meta.end_time = int(root.findtext("end_time") or "0")
    meta.duration_ms = meta.end_time - meta.start_time if meta.end_time > meta.start_time else 0

    # Название встречи
    meeting = root.find("meeting")
    if meeting is not None:
        raw = meeting.get("name", "")
        meta.meeting_name = unescape(raw).strip()

    # Контекст (курс)
    meta_elem = root.find("meta")
    if meta_elem is not None:
        ctx = meta_elem.findtext("bbb-context-name") or ""
        meta.course_name = unescape(ctx).strip()

    return meta


# ── Основная функция скачивания ─────────────────────────────────────────────

@dataclass
class FetchedRecording:
    """Результат скачивания: пути к файлам и метаданные."""
    record_id: str
    base_dir: Path
    meta: RecordingMeta
    slides: list[SlideInfo]
    metadata_xml: Path
    shapes_svg: Path
    textfiles: list[Path] = field(default_factory=list)
    slide_svgs: list[Path] = field(default_factory=list)
    video_path: Optional[Path] = None
    deskshare_path: Optional[Path] = None


def fetch_recording(
    record_id: str,
    tmp_dir: Path,
    download_video: bool = True,
    cookies: Optional[dict] = None,
) -> FetchedRecording:
    """Скачивает все ассеты BBB-записи. Возвращает пути и метаданные."""
    base_url = f"{BBB_BASE}/{record_id}"
    base_dir = tmp_dir / record_id

    sess = _session(cookies)

    # 1) metadata.xml
    print(f"📥 Скачивание ассетов: {record_id[:16]}...")
    meta_path = base_dir / "metadata.xml"
    _get(sess, f"{base_url}/metadata.xml", meta_path, "metadata.xml")
    meta = parse_metadata(meta_path.read_text(encoding="utf-8"))
    meta.record_id = record_id

    # 2) shapes.svg
    shapes_path = base_dir / "shapes.svg"
    _get(sess, f"{base_url}/shapes.svg", shapes_path, "shapes.svg")
    shapes_text = shapes_path.read_text(encoding="utf-8")
    slides = parse_shapes_svg(shapes_text)

    print(f"   📑 {len(slides)} слайдов, лекция: {meta.meeting_name}")
    print(f"   ⏱  Длительность: {meta.duration_min:.1f} мин, участников: {meta.participants}")

    # 3) Тексты слайдов и SVG-файлы
    folders = {s.folder_id for s in slides if s.folder_id}
    textfiles = []
    slide_svgs = []

    for folder_id in sorted(folders):
        folder_textfiles = base_dir / "presentation" / folder_id / "textfiles"
        folder_svgs = base_dir / "presentation" / folder_id / "svgs"

        # Определяем количество слайдов из shapes.svg
        max_slide_num = max(
            (int(re.search(r"slide(\d+)", s.svg_href).group(1))
             for s in slides if s.folder_id == folder_id and re.search(r"slide(\d+)", s.svg_href)),
            default=0
        )

        for i in range(1, max_slide_num + 1):
            # Текст слайда
            txt_url = f"{base_url}/presentation/{folder_id}/textfiles/slide-{i}.txt"
            txt_path = folder_textfiles / f"slide-{i}.txt"
            _get(sess, txt_url, txt_path, f"text/slide-{i}")
            textfiles.append(txt_path)

            # SVG слайда
            svg_url = f"{base_url}/presentation/{folder_id}/svgs/slide{i}.svg"
            svg_path = folder_svgs / f"slide{i}.svg"
            _get(sess, svg_url, svg_path, f"svg/slide{i}")
            slide_svgs.append(svg_path)

    # 4) Видео (webcams.webm)
    video_path = None
    deskshare_path = None
    if download_video:
        video_path = base_dir / "video" / "webcams.webm"
        _get(sess, f"{base_url}/video/webcams.webm", video_path, "webcams.webm")

        deskshare_path = base_dir / "deskshare" / "deskshare.webm"
        _get(sess, f"{base_url}/deskshare/deskshare.webm", deskshare_path, "deskshare.webm")

    return FetchedRecording(
        record_id=record_id,
        base_dir=base_dir,
        meta=meta,
        slides=slides,
        metadata_xml=meta_path,
        shapes_svg=shapes_path,
        textfiles=textfiles,
        slide_svgs=slide_svgs,
        video_path=video_path,
        deskshare_path=deskshare_path,
    )
