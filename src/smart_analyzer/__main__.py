"""命令行入口：``uv run python -m smart_analyzer --date 2026-09-16``。"""

from __future__ import annotations

import argparse
from datetime import date

from smart_analyzer.pipeline import run_day, yesterday


def main() -> None:
    parser = argparse.ArgumentParser(description="Smart-voice 日分析")
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="YYYY-MM-DD，默认昨天（上海时区）",
    )
    parser.add_argument(
        "--from-cache",
        action="store_true",
        help="只分析本地 session/logs，不打 ES",
    )
    parser.add_argument(
        "--sample-rate",
        type=int,
        default=None,
        help="采样率 5..100，覆盖环境变量",
    )
    args = parser.parse_args()
    target = yesterday() if not args.date else date.fromisoformat(args.date)
    summary = run_day(
        target,
        from_cache=args.from_cache,
        sample_rate=args.sample_rate,
        on_progress=lambda msg: print(msg, flush=True),
    )
    print(
        f"done date={summary.get('date')} sessions={summary.get('session_count')} "
        f"report={summary.get('report_path')}",
        flush=True,
    )


if __name__ == "__main__":
    main()
