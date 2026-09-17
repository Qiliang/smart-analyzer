"""按日发现已结束会话、哈希采样、落盘压缩 txt 日志。"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from smart_analyzer.config import Settings, get_settings
from smart_analyzer.es import ElasticClient

SESSION_END_MARKER = "Pipeline finished, cleaning up"
_ID_RE = re.compile(r"\[ID: (?P<sid>[^\]]+)\]")
# 原文: ``... | INFO    | [ID: SmartVoice-...] - body`` → ``... | INFO    | body``
_ID_STRIP_RE = re.compile(r" \| \[ID: [^\]]+\] - ")
_COMPACT_LOG_RE = re.compile(
    r"^(?P<time>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}) \| "
    r"(?P<level>\s*\w+\s*) \| (?P<body>.*)$",
    re.S,
)
CST = timezone(timedelta(hours=8))


def day_bounds(day: date) -> tuple[datetime, datetime]:
    """上海自然日 [00:00, 次日 00:00)。"""
    start = datetime(day.year, day.month, day.day, tzinfo=CST)
    return start, start + timedelta(days=1)


def _range_filter(start: datetime, end: datetime) -> dict[str, Any]:
    return {
        "range": {
            "@timestamp": {
                "gte": start.astimezone(UTC).isoformat(),
                "lt": end.astimezone(UTC).isoformat(),
            }
        }
    }


def _safe_sid(session_id: str) -> str:
    return (
        session_id.replace("/", "_")
        .replace("\\", "_")
        .replace(":", "_")
    )


def session_uuid(session_id: str) -> str:
    """``SmartVoice-{ivr}-{uuid}`` → uuid；否则原样返回。"""
    if session_id.startswith("SmartVoice-"):
        parts = session_id.split("-", 2)
        if len(parts) == 3 and parts[2]:
            return parts[2]
    return session_id


def session_in_sample(session_id: str, rate: int) -> bool:
    """稳定哈希：``md5(session_id) % 100 < rate``，整通取舍。"""
    if rate >= 100:
        return True
    if rate <= 0:
        return False
    digest = hashlib.md5(session_id.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 100 < rate


def logs_dir_for(day: date, settings: Settings | None = None) -> Path:
    cfg = settings or get_settings()
    return cfg.logs_root / day.isoformat()


def session_log_path(
    day: date, session_id: str, settings: Settings | None = None
) -> Path:
    """新格式路径：``*.txt``。"""
    return logs_dir_for(day, settings) / f"{_safe_sid(session_id)}.txt"


def session_log_path_legacy(
    day: date, session_id: str, settings: Settings | None = None
) -> Path:
    return logs_dir_for(day, settings) / f"{_safe_sid(session_id)}.jsonl"


def find_session_log(
    day: date, session_id: str, settings: Settings | None = None
) -> Path | None:
    """优先 txt，其次旧 jsonl；不存在返回 None。"""
    txt = session_log_path(day, session_id, settings)
    if txt.exists() and txt.stat().st_size > 0:
        return txt
    legacy = session_log_path_legacy(day, session_id, settings)
    if legacy.exists() and legacy.stat().st_size > 0:
        return legacy
    return None


def discover_sessions(
    es: ElasticClient,
    *,
    namespace: str,
    start: datetime,
    end: datetime,
    on_progress: Callable[[int], None] | None = None,
) -> list[str]:
    """返回时间窗内已结束的会话 id 列表（去重，顺序不保证）。"""
    query = {
        "bool": {
            "filter": [
                {"term": {"kubernetes.namespace.keyword": namespace}},
                {"match_phrase": {"message": SESSION_END_MARKER}},
                _range_filter(start, end),
            ]
        }
    }
    found: set[str] = set()
    for hit in es.scroll(
        query,
        source=("message", "@timestamp"),
        order="desc",
    ):
        message = (hit.get("_source") or {}).get("message") or ""
        if SESSION_END_MARKER not in message:
            continue
        m = _ID_RE.search(message)
        if not m:
            continue
        sid = m.group("sid").strip()
        if sid and sid != "-":
            found.add(sid)
            if on_progress is not None:
                on_progress(len(found))
    return sorted(found)


def fetch_session_docs(
    es: ElasticClient,
    session_id: str,
    *,
    namespace: str,
) -> list[dict[str, Any]]:
    """按 ``[ID: {session_id}]`` 拉取该会话全部日志行。"""
    query = {
        "bool": {
            "filter": [
                {"term": {"kubernetes.namespace.keyword": namespace}},
                {"match_phrase": {"message": f"[ID: {session_id}]"}},
            ]
        }
    }
    docs: list[dict[str, Any]] = []
    for hit in es.scroll(
        query, source=("message", "@timestamp", "kubernetes.pod.name")
    ):
        source = hit.get("_source") or {}
        message = source.get("message")
        if isinstance(message, str) and message:
            docs.append(source)
    return docs


def compact_message(message: str) -> str:
    """去掉每行重复的 ``[ID: ...]``，保留 time | level | body。"""
    return _ID_STRIP_RE.sub(" | ", message, count=1)


def expand_message(line: str, session_id: str) -> str:
    """把压缩行还原成带 ``[ID: ...]`` 的完整 loguru 行，供扫描器复用。"""
    if "[ID:" in line:
        return line
    m = _COMPACT_LOG_RE.match(line)
    if not m:
        return line
    return (
        f"{m.group('time')} | {m.group('level')} | "
        f"[ID: {session_id}] - {m.group('body')}"
    )


def save_session_txt(
    path: Path, session_id: str, docs: list[dict[str, Any]]
) -> None:
    """落盘压缩 txt：头部写 session_id / uuid，正文只留 message 并去重 ID。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    uid = session_uuid(session_id)
    with path.open("w", encoding="utf-8") as fh:
        fh.write(f"# session_id: {session_id}\n")
        fh.write(f"# uuid: {uid}\n")
        for doc in docs:
            message = doc.get("message")
            if not isinstance(message, str) or not message:
                continue
            fh.write(compact_message(message) + "\n")


def load_docs(path: Path) -> list[dict[str, Any]]:
    """读本地日志，返回 ``[{message: 完整loguru行}, ...]``。

    支持新 ``.txt``（自动还原 ID）与旧 ``.jsonl``。
    """
    if path.suffix.lower() == ".jsonl":
        docs: list[dict[str, Any]] = []
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                docs.append(json.loads(line))
        return docs

    session_id = path.stem
    lines_out: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for raw in fh:
            line = raw.rstrip("\n")
            if not line:
                continue
            if line.startswith("#"):
                if line.lower().startswith("# session_id:"):
                    session_id = line.split(":", 1)[1].strip() or session_id
                continue
            lines_out.append({"message": expand_message(line, session_id)})
    return lines_out


def list_cached_session_ids(day_dir: Path) -> list[str]:
    """本地已有日志的 session_id（txt 优先，兼容 jsonl）。"""
    found: dict[str, Path] = {}
    for pattern in ("*.txt", "*.jsonl"):
        for p in day_dir.glob(pattern):
            if not p.is_file() or p.stat().st_size <= 0:
                continue
            sid = p.stem
            if sid in found and found[sid].suffix == ".txt":
                continue
            if pattern == "*.txt" or sid not in found:
                found[sid] = p
    return sorted(found)


def pull_day(
    day: date,
    *,
    settings: Settings | None = None,
    from_cache: bool = False,
    sample_rate: int | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> list[str]:
    """发现 → 采样 → 落盘。返回最终本地可用的 session_id 列表。

    ``from_cache=True`` 时不打 ES，只返回当日已有日志对应的会话。
    """
    cfg = settings or get_settings()
    rate = cfg.normalized_sample_rate() if sample_rate is None else sample_rate
    day_dir = logs_dir_for(day, cfg)
    day_dir.mkdir(parents=True, exist_ok=True)

    if from_cache:
        ids = list_cached_session_ids(day_dir)
        if on_progress is not None:
            on_progress(f"from-cache sessions={len(ids)}")
        return ids

    start, end = day_bounds(day)
    with ElasticClient(
        cfg.elasticsearch_host,
        cfg.elasticsearch_index,
        page_size=cfg.elasticsearch_page_size,
        retries=cfg.elasticsearch_retries,
    ) as es:
        if on_progress is not None:
            on_progress(f"discover {day.isoformat()}")
        all_ids = discover_sessions(
            es,
            namespace=cfg.elasticsearch_namespace,
            start=start,
            end=end,
            on_progress=(
                (lambda n: on_progress(f"discovered={n}")) if on_progress else None
            ),
        )
        sampled = [sid for sid in all_ids if session_in_sample(sid, rate)]
        if on_progress is not None:
            on_progress(
                f"discovered={len(all_ids)} sampled={len(sampled)} rate={rate}%"
            )

        kept: list[str] = []
        for i, sid in enumerate(sampled, 1):
            existing = find_session_log(day, sid, cfg)
            if existing is not None:
                kept.append(sid)
                if on_progress is not None and (
                    i == 1 or i % 5 == 0 or i == len(sampled)
                ):
                    on_progress(f"pull {i}/{len(sampled)} (cached)")
                continue
            docs = fetch_session_docs(
                es, sid, namespace=cfg.elasticsearch_namespace
            )
            if not docs:
                if on_progress is not None and (
                    i == 1 or i % 5 == 0 or i == len(sampled)
                ):
                    on_progress(f"pull {i}/{len(sampled)} (empty)")
                continue
            save_session_txt(session_log_path(day, sid, cfg), sid, docs)
            kept.append(sid)
            if on_progress is not None and (
                i == 1 or i % 5 == 0 or i == len(sampled)
            ):
                on_progress(f"pull {i}/{len(sampled)}")
        return kept
