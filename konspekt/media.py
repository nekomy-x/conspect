"""Скачивание записи BBB напрямую + Whisper-транскрипт + слайды из структуры записи.

Структура записи BigBlueButton (проверено на bbb.edu.vsu.ru):
  /presentation/<ID>/metadata.xml                       — метаданные (имя, курс)
  /presentation/<ID>/shapes.svg                         — тайминг слайдов
  /presentation/<ID>/presentation_text.json             — тексты всех слайдов
  /presentation/<ID>/presentation/<P>/svgs/slideN.svg    — слайды (SVG)
  /presentation/<ID>/presentation/<P>/textfiles/slide-N.txt
  /presentation/<ID>/presentation/<P>/thumbnails/thumb-N.png
  /presentation/<ID>/video/webcams.webm                 — видео+аудио вебкам
  /presentation/<ID>/deskshare/deskshare.webm           — общий экран
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
import unicodedata
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import httpx

from .config import cfg

log = logging.getLogger("konspect.media")

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


def safe_name(s: str, maxlen: int = 60) -> str:
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"[^\w\s-]", "", s, flags=re.U)
    s = re.sub(r"\s+", "_", s.strip())
    return s[:maxlen]


def recording_id_from_url(playback_url: str) -> str | None:
    m = re.search(r"([0-9a-f]{40}-\d{13})", playback_url)
    return m.group(1) if m else None


async def fetch_recording_info(playback_url: str) -> dict[str, Any]:
    """Метаданные + список слайдов записи (без скачивания видео).

    Поддерживает прямой bbb URL (hash-timestamp) и Moodle bbb_view.php
    (резолвится HEAD-редиректом → playback/<id>).
    """
    rid = recording_id_from_url(playback_url)
    if not rid and "bbb_view.php" in playback_url:
        # Moodle URL → редирект на реальный playback URL
        try:
            async with httpx.AsyncClient(timeout=30, headers={"User-Agent": UA},
                                         follow_redirects=False) as c:
                r = await c.head(playback_url)
                loc = r.headers.get("location", "")
                if loc and "playback" in loc:
                    playback_url = loc
                    rid = recording_id_from_url(loc)
        except Exception as e:  # noqa: BLE001
            log.warning("bbb_view resolve: %s", e)
    if not rid:
        raise ValueError(f"Не удалось извлечь ID записи из {playback_url}")
    # Файлы записи: пробуем /presentation/ (старый путь), затем
    # /bigbluebutton/presentation/ (новые записи). Moodle может резолвить сам.
    base = f"{cfg.bbb_base}/presentation/{rid}"
    try:
        import httpx as _hx
        probe = await _hx.AsyncClient(timeout=10).get(f"{base}/metadata.xml")
        if probe.status_code == 200 and "returncode" not in probe.text[:200]:
            pass  # старый путь работает
        else:
            base = f"{cfg.bbb_base}/bigbluebutton/presentation/{rid}"
    except Exception:
        pass
    async with httpx.AsyncClient(timeout=60, headers={"User-Agent": UA},
                                 follow_redirects=True) as c:
        meta_r = await c.get(f"{base}/metadata.xml")
        info: dict[str, Any] = {"id": rid, "base": base}
        if meta_r.status_code == 200:
            try:
                root = ET.fromstring(meta_r.text)
                meeting = root.find("meeting")
                if meeting is not None:
                    info["name"] = meeting.get("name", "")
                    info["meeting_id"] = meeting.get("id", "")
                meta = root.find("meta")
                if meta is not None:
                    for child in meta:
                        tag = child.tag.lower().replace("bbb-", "")
                        info[tag] = (child.text or "").strip()
                st = root.find("start_time")
                en = root.find("end_time")
                if st is not None and st.text and en is not None and en.text:
                    info["duration_s"] = int(int(en.text) - int(st.text)) / 1000
            except ET.ParseError:
                log.warning("metadata parse error")

        # тексты слайдов
        try:
            pt_r = await c.get(f"{base}/presentation_text.json")
            info["slide_texts"] = pt_r.json() if pt_r.status_code == 200 else {}
        except Exception:  # noqa: BLE001
            info["slide_texts"] = {}

        # тайминг слайдов из shapes.svg
        slides: list[dict] = []
        try:
            sh_r = await c.get(f"{base}/shapes.svg")
            if sh_r.status_code == 200:
                for m in re.finditer(
                        r'<image[^>]+in="([\d.]+)"[^>]+out="([\d.]+)"[^>]+'
                        r'xlink:href="([^"]+)"[^>]+(?:text="([^"]+)")?', sh_r.text):
                    t_in, t_out, href, textfile = m.groups()
                    if "deskshare" in href:
                        continue
                    slides.append({
                        "in": float(t_in), "out": float(t_out),
                        "svg": href, "textfile": textfile or "",
                    })
        except Exception as e:  # noqa: BLE001
            log.warning("shapes parse: %s", e)
        info["slides"] = slides

        # наличие видео
        for key, path in (("webcams_url", "video/webcams.webm"),
                          ("deskshare_url", "deskshare/deskshare.webm")):
            r = await c.head(f"{base}/{path}")
            info[key] = f"{base}/{path}" if r.status_code == 200 else None
    return info


async def download_file(url: str, dest: Path) -> Path | None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=None, headers={"User-Agent": UA},
                                 follow_redirects=True) as c:
        try:
            with open(dest, "wb") as f:
                async with c.stream("GET", url) as r:
                    if r.status_code != 200:
                        log.warning("download %s -> %s", url, r.status_code)
                        return None
                    async for chunk in r.aiter_bytes(1 << 20):
                        f.write(chunk)
        except Exception as e:  # noqa: BLE001
            log.error("download %s failed: %s", url, e)
            return None
    return dest


async def download_slides(info: dict, out_dir: Path, width: int = 1600) -> list[dict]:
    """Скачивает слайды: тексты + рендер SVG→PNG (rsvg-convert)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    slides = info.get("slides", [])
    slide_texts: dict = info.get("slide_texts", {})
    result: list[dict] = []

    # тексты: presentation/<preskey>/slide-N
    pres_texts: dict[str, dict] = {}
    for pres_key, slide_map in slide_texts.items():
        pres_texts[pres_key] = slide_map

    for i, s in enumerate(slides, 1):
        href: str = s["svg"]  # presentation/<P>/svgs/slideN.svg (относительно BBB)
        # правильный префикс: /presentation/<ID записи>/presentation/<P>/svgs/slideN.svg
        sub = href.lstrip("/")
        url = f"{info['base']}/{sub}"
        png = out_dir / f"slide_{i:02d}.png"
        if not png.exists():
            svg_path = out_dir / f"slide_{i:02d}.svg"
            await download_file(url, svg_path)
            if svg_path.exists() and svg_path.stat().st_size > 1000:
                try:
                    proc = await asyncio.create_subprocess_exec(
                        "rsvg-convert", "-w", str(width), str(svg_path), "-o", str(png),
                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                    await proc.communicate()
                except FileNotFoundError:
                    log.warning("rsvg-convert не найден — слайд без PNG")
            if svg_path.exists():
                svg_path.unlink()
        text = ""
        # текст слайда: по ключу презентации
        m = re.search(r"presentation/([^/]+)/svgs/slide(\d+)\.svg", href)
        if m:
            pkey, n = m.group(1), f"slide-{m.group(2)}"
            text = pres_texts.get(pkey, {}).get(n, "")
        if not text:
            # textfiles fallback
            tf = s.get("textfile")
            if tf:
                tf_dest = out_dir / f"slide_{i:02d}.txt"
                await download_file(f"{info['base']}/{tf.lstrip('/')}", tf_dest)
                if tf_dest.exists():
                    text = tf_dest.read_text(encoding="utf-8", errors="replace")
                    tf_dest.unlink()
        result.append({"n": i, "png": str(png) if png.exists() else None,
                       "text": text.strip(), "t_in": s["in"], "t_out": s["out"]})
    return result


async def extract_video_frames(video: Path, out_dir: Path, every: int = 120,
                               max_frames: int = 60) -> list[Path]:
    """Кадры из видео (для десктар-шеринга, если слайдов нет)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    dur = await video_duration(video)
    if not dur:
        return []
    step = max(every, int(dur / max_frames))
    paths: list[Path] = []
    t = 0.0
    while t < dur and len(paths) < max_frames:
        name = out_dir / f"frame_{int(t):05d}.png"
        cmd = ["ffmpeg", "-y", "-ss", str(int(t)), "-i", str(video),
               "-frames:v", "1", "-loglevel", "error", str(name)]
        p = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL,
                                                  stderr=asyncio.subprocess.PIPE)
        _, err = await p.communicate()
        if name.exists():
            paths.append(name)
        t += step
    return paths


async def video_duration(video: Path) -> float:
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration",
           "-of", "json", str(video)]
    p = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                             stderr=asyncio.subprocess.DEVNULL)
    out, _ = await p.communicate()
    try:
        return float(json.loads(out.decode())["format"]["duration"])
    except Exception:  # noqa: BLE001
        return 0.0


def _whisper_cache() -> None:
    # кэш моделей — внутрь проекта, иначе sandbox режет ~/.cache
    import os
    cache = cfg.data_dir / ".whisper_cache"
    cache.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("XDG_CACHE_HOME", str(cfg.data_dir / ".cache"))
    (cfg.data_dir / ".cache").mkdir(parents=True, exist_ok=True)


def transcribe(audio: Path, out_dir: Path, model_name: str = "small") -> Path:
    """Локальный Whisper по аудио. Возвращает transcript.txt с таймкодами."""
    _whisper_cache()
    out_dir.mkdir(parents=True, exist_ok=True)
    import whisper

    model = whisper.load_model(model_name, download_root=str(cfg.data_dir / ".whisper_cache"))
    result = model.transcribe(str(audio), language="ru", verbose=False)
    lines = []
    for seg in result.get("segments", []):
        stamp = f"[{int(seg['start'])//60:02d}:{int(seg['start'])%60:02d}]"
        lines.append(f"{stamp} {seg['text'].strip()}")
    txt_path = out_dir / "transcript.txt"
    txt_path.write_text("\n".join(lines), encoding="utf-8")
    _write_srt(result.get("segments", []), out_dir / "transcript.srt")
    return txt_path


def _write_srt(segments: list[dict], path: Path) -> None:
    def ts(t: float) -> str:
        h, rem = divmod(int(t), 3600)
        m, s = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d},{int((t % 1) * 1000):03d}"

    with open(path, "w", encoding="utf-8") as f:
        for i, seg in enumerate(segments, 1):
            f.write(f"{i}\n{ts(seg['start'])} --> {ts(seg['end'])}\n{seg['text'].strip()}\n\n")


async def transcribe_chunked(audio: Path, out_dir: Path,
                             model_name: str = "small",
                             chunk_sec: int = 600) -> Path:
    """Whisper по чанкам (10 мин) — не грузит всю запись в память.
    Таймкоды склеиваются со смещением. Промежуточные чанки удаляются."""
    import glob as _glob
    import asyncio as _aio

    _whisper_cache()
    out_dir.mkdir(parents=True, exist_ok=True)
    chunks_dir = out_dir / "_chunks"
    chunks_dir.mkdir(exist_ok=True)
    # сегментация
    proc = await _aio.create_subprocess_exec(
        "ffmpeg", "-y", "-i", str(audio), "-f", "segment",
        "-segment_time", str(chunk_sec), "-ac", "1", "-ar", "16000",
        "-loglevel", "error", str(chunks_dir / "chunk_%03d.wav"),
        stdout=_aio.subprocess.DEVNULL, stderr=_aio.subprocess.PIPE)
    _, err = await proc.communicate()
    chunks = sorted(chunks_dir.glob("chunk_*.wav"))
    if not chunks:
        log.error("chunking failed: %s", err.decode()[:200] if err else "?")
        return await transcribe_async(audio, out_dir, model_name)

    def _work() -> Path:
        import whisper
        model = whisper.load_model(model_name,
                                   download_root=str(cfg.data_dir / ".whisper_cache"))
        all_segments = []
        offset = 0.0
        for ch in chunks:
            result = model.transcribe(str(ch), language="ru", verbose=False)
            for s in result.get("segments", []):
                all_segments.append({"start": s["start"] + offset,
                                     "end": s["end"] + offset,
                                     "text": s["text"].strip()})
            offset += chunk_sec
        lines = [f"[{int(s['start'])//60:02d}:{int(s['start'])%60:02d}] {s['text']}"
                 for s in all_segments]
        txt = out_dir / "transcript.txt"
        txt.write_text("\n".join(lines), encoding="utf-8")
        _write_srt(all_segments, out_dir / "transcript.srt")
        # чанки не нужны
        for ch in chunks:
            ch.unlink(missing_ok=True)
        chunks_dir.rmdir()
        return txt

    return await _aio.to_thread(_work)


async def transcribe_async(audio: Path, out_dir: Path, model_name: str = "small") -> Path:
    return await asyncio.to_thread(transcribe, audio, out_dir, model_name)


async def extract_audio(video: Path, out_dir: Path) -> Path:
    """webm → 16k wav для Whisper."""
    out_dir.mkdir(parents=True, exist_ok=True)
    wav = out_dir / "audio.wav"
    cmd = ["ffmpeg", "-y", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000",
           "-loglevel", "error", str(wav)]
    p = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                             stderr=asyncio.subprocess.PIPE)
    _, err = await p.communicate()
    if not wav.exists():
        raise RuntimeError(f"ffmpeg audio extract failed: {err.decode()[:300]}")
    return wav
