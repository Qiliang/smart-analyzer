"""日流水线：拉取 → 扫描 → 报表。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from smart_analyzer.account import fetch_company_names
from smart_analyzer.config import Settings, get_settings
from smart_analyzer.pull import find_session_log, load_docs, pull_day
from smart_analyzer.report import build_summary, write_reports
from smart_analyzer.scan import SessionSample, scan_docs

CST = timezone(timedelta(hours=8))


def yesterday(now: datetime | None = None) -> date:
    current = now or datetime.now(CST)
    return (current - timedelta(days=1)).date()


def analyze_day(
    day: date,
    session_ids: list[str],
    *,
    settings: Settings | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    cfg = settings or get_settings()
    samples: list[SessionSample] = []
    for i, sid in enumerate(session_ids, 1):
        path = find_session_log(day, sid, cfg)
        if path is None:
            continue
        docs = load_docs(path)
        samples.append(scan_docs(sid, docs))
        if on_progress is not None and (
            i == 1 or i % 5 == 0 or i == len(session_ids)
        ):
            on_progress(f"scan {i}/{len(session_ids)}")

    company_names = fetch_company_names(
        [s.account_id for s in samples], settings=cfg
    )
    summary = build_summary(samples, company_names)
    summary["date"] = day.isoformat()
    summary["sample_rate"] = cfg.normalized_sample_rate()
    return summary


def run_day(
    day: date | None = None,
    *,
    settings: Settings | None = None,
    from_cache: bool = False,
    sample_rate: int | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """跑完整日任务，返回 summary，并写 report.html / summary.json。"""
    cfg = settings or get_settings()
    target = day or yesterday()
    if on_progress is not None:
        on_progress(f"run_day {target.isoformat()} from_cache={from_cache}")

    session_ids = pull_day(
        target,
        settings=cfg,
        from_cache=from_cache,
        sample_rate=sample_rate,
        on_progress=on_progress,
    )
    summary = analyze_day(
        target, session_ids, settings=cfg, on_progress=on_progress
    )
    report_dir = Path(cfg.reports_root) / target.isoformat()
    report_path, summary_path = write_reports(report_dir, summary)
    summary["report_path"] = str(report_path)
    summary["summary_path"] = str(summary_path)
    if on_progress is not None:
        on_progress(f"wrote {report_path}")
    return summary
