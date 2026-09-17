"""聚合报表：账号会话/轮次 + STT/Agent/TTS 分位数。"""

from __future__ import annotations

import json
import unicodedata
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from smart_analyzer.account import account_label
from smart_analyzer.scan import SessionSample


def percentile(values: Sequence[float], p: float) -> float:
    """最近排名法分位数。"""
    if not values:
        return float("nan")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    idx = int(round(p * (len(ordered) - 1)))
    idx = max(0, min(len(ordered) - 1, idx))
    return ordered[idx]


def series_stats(values: Sequence[float]) -> dict[str, float]:
    if not values:
        return {"count": 0, "p50": float("nan"), "p95": float("nan")}
    return {
        "count": float(len(values)),
        "p50": percentile(values, 0.50),
        "p95": percentile(values, 0.95),
    }


def _fmt(value: float) -> str:
    return "-" if value != value else f"{value:.3f}"


def _width(text: str) -> int:
    w = 0
    for ch in text:
        if unicodedata.east_asian_width(ch) in ("F", "W"):
            w += 2
        else:
            w += 1
    return w


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _width(text))


@dataclass
class GroupBucket:
    sessions: int = 0
    turns: int = 0
    stt: list[float] = field(default_factory=list)
    agent: list[float] = field(default_factory=list)
    tts_agg: list[float] = field(default_factory=list)
    tts_first: list[float] = field(default_factory=list)


def build_summary(
    samples: Sequence[SessionSample],
    company_names: Mapping[str, str],
) -> dict[str, Any]:
    overall = GroupBucket()
    by_account: dict[str, GroupBucket] = defaultdict(GroupBucket)
    by_agent: dict[tuple[str, str], GroupBucket] = defaultdict(GroupBucket)

    for s in samples:
        for bucket in (
            overall,
            by_account[s.account_id],
            by_agent[(s.account_id, s.agent_id)],
        ):
            bucket.sessions += 1
            bucket.turns += s.turns
            bucket.stt.extend(s.stt_confirm_lags)
            bucket.agent.extend(s.agent_first_token)
            bucket.tts_agg.extend(s.tts_agg_lags)
            bucket.tts_first.extend(s.tts_first_audio)

    accounts_sorted = sorted(
        by_account.keys(),
        key=lambda aid: (
            aid in ("", "-"),
            -by_account[aid].sessions,
            aid,
        ),
    )

    def pack(bucket: GroupBucket) -> dict[str, Any]:
        return {
            "sessions": bucket.sessions,
            "turns": bucket.turns,
            "stt_confirm": series_stats(bucket.stt),
            "agent_first_token": series_stats(bucket.agent),
            "tts_aggregation": series_stats(bucket.tts_agg),
            "tts_first_audio": series_stats(bucket.tts_first),
        }

    accounts_out: list[dict[str, Any]] = []
    for account_id in accounts_sorted:
        agents = [
            (aid, ag)
            for (aid, ag) in by_agent
            if aid == account_id
        ]
        agents.sort(
            key=lambda pair: (
                -by_agent[pair].sessions,
                pair[1],
            )
        )
        accounts_out.append(
            {
                "account_id": account_id,
                "label": account_label(account_id, dict(company_names)),
                **pack(by_account[account_id]),
                "agents": [
                    {
                        "agent_id": ag,
                        "label": f"{ag}[{account_id}]",
                        **pack(by_agent[(account_id, ag)]),
                    }
                    for _, ag in agents
                ],
            }
        )

    return {
        "overall": pack(overall),
        "accounts": accounts_out,
        "session_count": overall.sessions,
    }


def render_report(summary: Mapping[str, Any]) -> str:
    lines: list[str] = []
    label_w = 70

    def row_account(label: str, sessions: int, turns: int) -> str:
        return (
            f"{_pad(label, label_w)}"
            f"{sessions:>10}"
            f"{turns:>10}"
        )

    def row_metric(
        label: str,
        stt: Mapping[str, float],
        agent: Mapping[str, float],
        agg: Mapping[str, float],
        first: Mapping[str, float],
    ) -> str:
        return (
            f"{_pad(label, label_w)}"
            f"{int(stt.get('count') or 0):>8}"
            f"{_fmt(float(stt.get('p50', float('nan')))):>10}"
            f"{_fmt(float(stt.get('p95', float('nan')))):>10}"
            f"{int(agent.get('count') or 0):>8}"
            f"{_fmt(float(agent.get('p50', float('nan')))):>10}"
            f"{_fmt(float(agent.get('p95', float('nan')))):>10}"
            f"{int(agg.get('count') or 0):>8}"
            f"{_fmt(float(agg.get('p50', float('nan')))):>10}"
            f"{int(first.get('count') or 0):>8}"
            f"{_fmt(float(first.get('p50', float('nan')))):>10}"
        )

    lines.append("账号分布（分类 / 会话 / 轮次）")
    lines.append(
        f"{_pad('分类', label_w)}{'会话':>10}{'轮次':>10}"
    )
    lines.append("-" * (label_w + 20))
    overall = summary["overall"]
    lines.append(row_account("总体", overall["sessions"], overall["turns"]))
    for acc in summary["accounts"]:
        lines.append(
            row_account(acc["label"], acc["sessions"], acc["turns"])
        )
        for ag in acc["agents"]:
            lines.append(
                row_account("  " + ag["label"], ag["sessions"], ag["turns"])
            )

    lines.append("")
    lines.append(
        "延迟指标（STT 定稿 / Agent 首字 / TTS 聚合 / TTS 首音），单位秒"
    )
    header = (
        f"{_pad('分类', label_w)}"
        f"{'STTn':>8}{'STTp50':>10}{'STTp95':>10}"
        f"{'AgTn':>8}{'AgTp50':>10}{'AgTp95':>10}"
        f"{'AggN':>8}{'AggP50':>10}"
        f"{'1stN':>8}{'1stP50':>10}"
    )
    lines.append(header)
    lines.append("-" * _width(header))

    def emit_metric_row(label: str, node: Mapping[str, Any]) -> None:
        lines.append(
            row_metric(
                label,
                node["stt_confirm"],
                node["agent_first_token"],
                node["tts_aggregation"],
                node["tts_first_audio"],
            )
        )

    emit_metric_row("总体", overall)
    for acc in summary["accounts"]:
        emit_metric_row(acc["label"], acc)
        for ag in acc["agents"]:
            emit_metric_row("  " + ag["label"], ag)

    lines.append("")
    lines.append(
        "说明: STT=火山定稿延迟(末次interim→定稿); "
        "Agent=MPAAS_AGENT MetricsFrame.value; "
        "Agg=LLMTextFrame→AggregatedTextFrame; "
        "1st=on_tts_first_audio.ttfb 或 TTS MetricsFrame.ttfb"
    )
    return "\n".join(lines) + "\n"


def write_reports(
    report_dir: Path,
    summary: Mapping[str, Any],
) -> tuple[Path, Path]:
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / "report.txt"
    summary_path = report_dir / "summary.json"
    report_path.write_text(render_report(summary), encoding="utf-8")
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return report_path, summary_path
