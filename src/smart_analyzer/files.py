"""session 目录安全浏览。"""

from __future__ import annotations

import mimetypes
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from smart_analyzer.config import Settings, get_settings

_TEXT_SUFFIXES = {
    ".txt",
    ".json",
    ".jsonl",
    ".log",
    ".md",
    ".html",
    ".htm",
    ".csv",
    ".yml",
    ".yaml",
    ".env",
    ".py",
}
_MAX_PREVIEW_BYTES = 512 * 1024


def session_root(settings: Settings | None = None) -> Path:
    cfg = settings or get_settings()
    root = Path(cfg.session_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def resolve_under_session(
    rel: str, *, settings: Settings | None = None
) -> Path:
    """把相对路径解析到 session_root 下；禁止逃逸。"""
    root = session_root(settings)
    cleaned = (rel or ".").strip().lstrip("/")
    if cleaned in ("", "."):
        target = root
    else:
        target = (root / cleaned).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise PermissionError("path escapes session root") from exc
    return target


def list_dir(rel: str = "", *, settings: Settings | None = None) -> dict[str, Any]:
    root = session_root(settings)
    target = resolve_under_session(rel, settings=settings)
    if not target.exists():
        raise FileNotFoundError(rel or ".")
    if not target.is_dir():
        raise NotADirectoryError(rel)

    entries: list[dict[str, Any]] = []
    for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        if child.name.startswith("."):
            continue
        stat = child.stat()
        rel_path = str(child.relative_to(root)).replace("\\", "/")
        entries.append(
            {
                "name": child.name,
                "path": rel_path,
                "is_dir": child.is_dir(),
                "size": 0 if child.is_dir() else stat.st_size,
                "mtime": datetime.fromtimestamp(
                    stat.st_mtime, tz=timezone.utc
                ).isoformat(),
            }
        )

    current = "" if target == root else str(target.relative_to(root)).replace("\\", "/")
    parent = None
    if current:
        parent_path = Path(current).parent
        parent = "" if str(parent_path) == "." else str(parent_path).replace("\\", "/")

    return {
        "root": str(root),
        "path": current,
        "parent": parent,
        "entries": entries,
    }


def read_file(
    rel: str,
    *,
    settings: Settings | None = None,
    max_bytes: int = _MAX_PREVIEW_BYTES,
) -> dict[str, Any]:
    target = resolve_under_session(rel, settings=settings)
    if not target.exists() or not target.is_file():
        raise FileNotFoundError(rel)
    size = target.stat().st_size
    suffix = target.suffix.lower()
    mime, _ = mimetypes.guess_type(target.name)
    is_text = suffix in _TEXT_SUFFIXES or (mime or "").startswith("text/")
    if not is_text:
        return {
            "path": rel,
            "name": target.name,
            "size": size,
            "mime": mime or "application/octet-stream",
            "text": False,
            "content": None,
            "truncated": False,
            "message": "二进制文件，请下载查看",
        }
    raw = target.read_bytes()
    truncated = len(raw) > max_bytes
    if truncated:
        raw = raw[:max_bytes]
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        content = raw.decode("utf-8", errors="replace")
    return {
        "path": rel,
        "name": target.name,
        "size": size,
        "mime": mime or "text/plain",
        "text": True,
        "content": content,
        "truncated": truncated,
        "message": "内容已截断" if truncated else "",
    }
