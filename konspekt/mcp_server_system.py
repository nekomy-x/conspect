"""MCP-сервер «system»: полный доступ к файлам, папкам, командам.

Даёт агенту (Claude Desktop / ТГ-боту) возможности как у OpenClaw/Claude Code:
- выполнять shell-команды
- читать/писать/удалять файлы
- листать директории, искать
- управлять моделями (whisper и т.д.)

Безопасность: whitelist команд, запись только в проект-директорию.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
from pathlib import Path

from mcp.server.mcpserver import MCPServer

log = logging.getLogger("konspect.mcp.system")
mcp = MCPServer("konspekt-system",
                description="Полный доступ: shell, файлы, папки, модели",
                version="0.1.0")

# корень проекта (единственная зона записи)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ALLOWED_WRITE_ROOTS = [PROJECT_ROOT, Path.home() / "Downloads"]

# whitelist команд (всё остальное — отказ)
ALLOWED_CMDS = {
    "ls", "cat", "head", "tail", "grep", "find", "wc", "file", "du", "df",
    "pwd", "echo", "date", "whoami", "uname", "free", "uptime", "ps",
    "mkdir", "rm", "cp", "mv", "touch", "ln", "chmod", "chown",
    "tar", "zip", "unzip", "gzip", "gunzip",
    "curl", "wget", "ping", "nc", "dig", "nslookup",
    "python3", "pip3", "node", "npm", "git",
    "ffmpeg", "ffprobe", "yt-dlp",
    "systemctl", "journalctl", "service",
    "docker", "docker-compose",
    "ssh", "scp", "rsync",
    "top", "htop", "kill", "pkill",
    "history", "which", "env", "export",
    "sed", "awk", "sort", "uniq", "diff", "patch",
    "tree", "stat", "md5sum", "sha256sum",
    "open", "code",
}

# запрещённые паттерны (даже в whitelist-командах)
FORBIDDEN_PATTERNS = [
    r"rm\s+-rf\s+/(?!opt/konspekt)",   # rm -rf / но не /opt/konspekt
    r"mkfs", r"dd\s+if=", r":\(\)\s*{",  # fork bomb
    r">\s*/dev/sd", r"shutdown", r"reboot", r"poweroff",
    r"chmod\s+-R\s+777\s+/",
    r"curl.*\|\s*(ba)?sh", r"wget.*\|\s*(ba)?sh",
]

# файлы, которые агенту читать ЗАПРЕЩЕНО (секреты)
SECRET_FILES = {".env", ".env.local", ".env.production", "credentials",
                ".credentials.yaml", "id_rsa", "id_ed25519"}


def _is_secret_path(path: str) -> bool:
    """Проверяет что путь ведёт к секретному файлу."""
    from pathlib import Path
    p = Path(path).expanduser()
    name = p.name.lower()
    if name in SECRET_FILES:
        return True
    if name.startswith(".env"):
        return True
    if p.suffix in (".pem", ".key", ".p12"):
        return True
    return False


def _check_command(cmd: str) -> str | None:
    """Возвращает ошибку если команда запрещена, иначе None."""
    for pat in FORBIDDEN_PATTERNS:
        if re.search(pat, cmd):
            return f"запрещено: {pat}"
    first = cmd.strip().split()[0] if cmd.strip() else ""
    base = os.path.basename(first)
    if base not in ALLOWED_CMDS:
        return f"команда '{base}' не в whitelist"
    return None


def _check_write_path(path: str) -> str | None:
    """Проверяет что путь записи в разрешённой зоне."""
    p = Path(path).expanduser().resolve()
    for root in ALLOWED_WRITE_ROOTS:
        try:
            p.relative_to(root)
            return None
        except ValueError:
            continue
    return f"запись вне разрешённых зон ({[str(r) for r in ALLOWED_WRITE_ROOTS]})"


@mcp.tool()
async def shell_exec(command: str, cwd: str = "", timeout: int = 60) -> dict:
    """Выполнить shell-команду. Полный доступ к системе (whitelist).

    Примеры: 'ls -la', 'rm -rf data/old', 'cat .env', 'systemctl restart konspect',
    'ffmpeg -i video.mp4 ...', 'git status'."""
    err = _check_command(command)
    if err:
        return {"error": err, "blocked": True}
    # блокируем чтение секретов через shell
    for sf in SECRET_FILES:
        if re.search(rf"(cat|head|tail|less|more|grep|source)\s+.*{re.escape(sf)}", command):
            return {"error": f"доступ к {sf} запрещён (секрет)", "blocked": True}
    if re.search(r"cat\s+.*\.env", command):
        return {"error": "доступ к .env запрещён (секрет)", "blocked": True}
    workdir = cwd or str(PROJECT_ROOT)
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=workdir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return {"error": f"таймаут {timeout}с", "killed": True}
        return {
            "exit_code": proc.returncode,
            "stdout": stdout.decode(errors="replace")[:20000],
            "stderr": stderr.decode(errors="replace")[:5000],
            "command": command,
        }
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


@mcp.tool()
async def read_file(path: str, max_lines: int = 500) -> dict:
    """Прочитать файл (текст) с номерами строк. Секретные файлы запрещены."""
    if _is_secret_path(path):
        return {"error": f"доступ к {path} запрещён (секретный файл)", "blocked": True}
    p = Path(path).expanduser()
    if not p.exists():
        return {"error": f"нет файла: {path}"}
    if p.stat().st_size > 10 * 1024 * 1024:
        return {"error": "файл >10МБ — используй shell_exec с head/tail"}
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        numbered = [f"{i+1}: {l}" for i, l in enumerate(lines[:max_lines])]
        return {"path": str(p), "lines": len(lines),
                "content": "\n".join(numbered)[:30000]}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


@mcp.tool()
async def write_file(path: str, content: str) -> dict:
    """Записать/создать файл. Только в проект-директорию и ~/Downloads.
    Секретные файлы (.env и т.п.) — только через /password в боте."""
    err = _check_write_path(path)
    if err:
        return {"error": err, "blocked": True}
    if _is_secret_path(path):
        return {"error": f"запись в {path} запрещена (секрет)", "blocked": True}
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return {"ok": True, "path": str(p), "bytes": len(content.encode())}


@mcp.tool()
async def list_dir(path: str = ".", recursive: bool = False,
                   max_items: int = 200) -> dict:
    """Список файлов/папок. recursive=True — дерево."""
    p = Path(path).expanduser()
    if not p.exists():
        return {"error": f"нет пути: {path}"}
    items = []
    if recursive:
        for i, f in enumerate(sorted(p.rglob("*"))):
            if i >= max_items:
                break
            items.append(str(f.relative_to(p)) +
                         ("/" if f.is_dir() else ""))
    else:
        for f in sorted(p.iterdir()):
            items.append(f.name + ("/" if f.is_dir() else ""))
            if len(items) >= max_items:
                break
    return {"path": str(p), "count": len(items), "items": items}


@mcp.tool()
async def search_files(pattern: str, path: str = ".",
                       file_glob: str = "*") -> dict:
    """Поиск по содержимому файлов (grep). pattern — regex."""
    p = Path(path).expanduser()
    results = []
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return {"error": f"bad regex: {e}"}
    for f in p.rglob(file_glob):
        if not f.is_file() or f.stat().st_size > 5 * 1024 * 1024:
            continue
        try:
            for i, line in enumerate(f.read_text(errors="replace").splitlines()):
                if rx.search(line):
                    results.append(f"{f}:{i+1}: {line[:200]}")
                    if len(results) >= 50:
                        return {"results": results, "truncated": True}
        except Exception:  # noqa: BLE001
            continue
    return {"results": results}


@mcp.tool()
async def delete_path(path: str) -> dict:
    """Удалить файл или папку (rm -rf). Только в разрешённых зонах."""
    err = _check_write_path(path)
    if err:
        return {"error": err, "blocked": True}
    p = Path(path).expanduser()
    if not p.exists():
        return {"error": f"нет: {path}"}
    if p.is_dir():
        shutil.rmtree(p)
    else:
        p.unlink()
    return {"ok": True, "deleted": str(p)}


@mcp.tool()
async def move_path(src: str, dst: str) -> dict:
    """Переместить/переименовать файл или папку."""
    for path in (src, dst):
        err = _check_write_path(path)
        if err:
            return {"error": err, "blocked": True}
    shutil.move(str(Path(src).expanduser()), str(Path(dst).expanduser()))
    return {"ok": True, "from": src, "to": dst}


@mcp.tool()
async def system_info() -> dict:
    """Информация о системе: OS, CPU, RAM, диск, load."""
    import platform
    info = {"os": platform.platform(), "python": platform.python_version(),
            "cpu_count": os.cpu_count()}
    try:
        with open("/proc/loadavg") as f:
            info["load"] = f.read().split()[:3]
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal") or line.startswith("MemAvailable"):
                    info[line.split(":")[0]] = line.split(":")[1].strip()
    except Exception:  # noqa: BLE001
        pass
    return info


@mcp.tool()
async def manage_model(action: str, model_name: str = "small") -> dict:
    """Управление Whisper-моделями: download / list / delete."""
    cache = PROJECT_ROOT / "data" / ".whisper_cache"
    if action == "list":
        models = [f.name for f in cache.glob("*.pt")] if cache.exists() else []
        return {"models": models, "cache_dir": str(cache)}
    if action == "download":
        try:
            import whisper
            whisper.load_model(model_name, download_root=str(cache))
            return {"ok": True, "model": model_name, "downloaded": True}
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)}
    if action == "delete":
        for f in cache.glob(f"*{model_name}*"):
            f.unlink()
        return {"ok": True, "deleted": model_name}
    return {"error": f"неизвестное действие: {action}"}


async def main() -> None:
    await mcp.run_stdio_async()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
