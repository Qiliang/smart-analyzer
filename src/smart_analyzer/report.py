"""聚合报表：账号会话/轮次 + STT/Agent/TTS 分位数。"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from html import escape
from pathlib import Path
from typing import Any

import pandas as pd
from great_tables import GT, loc, md, style

from smart_analyzer.account import account_label
from smart_analyzer.scan import SessionSample

_NAN = float("nan")
_FONT_STACK = [
    "IBM Plex Sans",
    "Noto Sans SC",
    "PingFang SC",
    "Microsoft YaHei",
    "sans-serif",
]
_NOTE = (
    "STT=火山定稿延迟(末次interim→定稿); "
    "Agent=MPAAS_AGENT MetricsFrame.value; "
    "Agg=LLMTextFrame→AggregatedTextFrame; "
    "1st=on_tts_first_audio.ttfb 或 TTS MetricsFrame.ttfb; "
    "听到=出声前最后一次 UserStoppedSpeaking→首次 BotStartedSpeaking"
    "（续说覆盖；含垫词/嗯，不含欢迎语）"
)


def percentile(values: Sequence[float], p: float) -> float:
    """最近排名法分位数。"""
    if not values:
        return _NAN
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    idx = int(round(p * (len(ordered) - 1)))
    idx = max(0, min(len(ordered) - 1, idx))
    return ordered[idx]


def series_stats(values: Sequence[float]) -> dict[str, float]:
    if not values:
        return {"count": 0, "p50": _NAN, "p95": _NAN, "p99": _NAN}
    return {
        "count": float(len(values)),
        "p50": percentile(values, 0.50),
        "p95": percentile(values, 0.95),
        "p99": percentile(values, 0.99),
    }


@dataclass
class GroupBucket:
    sessions: int = 0
    turns: int = 0
    stt: list[float] = field(default_factory=list)
    agent: list[float] = field(default_factory=list)
    tts_agg: list[float] = field(default_factory=list)
    tts_first: list[float] = field(default_factory=list)
    hears: list[float] = field(default_factory=list)


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
            bucket.hears.extend(s.user_hears_sound)

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
            "user_hears_sound": series_stats(bucket.hears),
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


def _stat(node: Mapping[str, Any], key: str, field: str) -> float:
    raw = node.get(key) or {}
    try:
        return float(raw.get(field, _NAN))
    except (TypeError, ValueError):
        return _NAN


def _iter_rows(summary: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    def push(kind: str, label: str, node: Mapping[str, Any]) -> None:
        rows.append(
            {
                "kind": kind,
                "label": label,
                "sessions": int(node.get("sessions") or 0),
                "turns": int(node.get("turns") or 0),
                "stt_n": int(_stat(node, "stt_confirm", "count") or 0),
                "stt_p50": _stat(node, "stt_confirm", "p50"),
                "stt_p95": _stat(node, "stt_confirm", "p95"),
                "stt_p99": _stat(node, "stt_confirm", "p99"),
                "agent_n": int(_stat(node, "agent_first_token", "count") or 0),
                "agent_p50": _stat(node, "agent_first_token", "p50"),
                "agent_p95": _stat(node, "agent_first_token", "p95"),
                "agent_p99": _stat(node, "agent_first_token", "p99"),
                "agg_n": int(_stat(node, "tts_aggregation", "count") or 0),
                "agg_p50": _stat(node, "tts_aggregation", "p50"),
                "agg_p95": _stat(node, "tts_aggregation", "p95"),
                "agg_p99": _stat(node, "tts_aggregation", "p99"),
                "first_n": int(_stat(node, "tts_first_audio", "count") or 0),
                "first_p50": _stat(node, "tts_first_audio", "p50"),
                "first_p95": _stat(node, "tts_first_audio", "p95"),
                "first_p99": _stat(node, "tts_first_audio", "p99"),
                "hears_n": int(_stat(node, "user_hears_sound", "count") or 0),
                "hears_p50": _stat(node, "user_hears_sound", "p50"),
                "hears_p95": _stat(node, "user_hears_sound", "p95"),
                "hears_p99": _stat(node, "user_hears_sound", "p99"),
            }
        )

    push("overall", "总体", summary["overall"])
    for acc in summary.get("accounts") or []:
        push("account", str(acc.get("label") or acc.get("account_id") or "-"), acc)
        for ag in acc.get("agents") or []:
            push("agent", str(ag.get("label") or ag.get("agent_id") or "-"), ag)
    return rows


def _style_table(tbl: GT, rows: Sequence[Mapping[str, Any]], *, table_width: str) -> GT:
    parent_labels = [r["label"] for r in rows if r["kind"] != "agent"]
    agent_labels = [r["label"] for r in rows if r["kind"] == "agent"]
    overall_labels = [r["label"] for r in rows if r["kind"] == "overall"]

    tbl = (
        tbl.tab_stubhead(label="分类")
        .opt_align_table_header("left")
        .opt_row_striping()
        .opt_horizontal_padding(scale=1.15)
        .opt_table_font(font=_FONT_STACK)
        .tab_options(
            table_width=table_width,
            table_font_size="13px",
            table_font_color="#1a1f24",
            table_border_top_style="solid",
            table_border_top_width="2px",
            table_border_top_color="#0f6e56",
            table_border_bottom_style="solid",
            table_border_bottom_width="2px",
            table_border_bottom_color="#0f6e56",
            heading_align="left",
            heading_title_font_weight="600",
            heading_background_color="#ffffff",
            column_labels_font_weight="600",
            column_labels_border_top_color="#0f6e56",
            column_labels_border_bottom_color="#0f6e56",
            column_labels_border_bottom_width="2px",
            stub_background_color="#ffffff",
            stub_font_weight="normal",
            stub_border_color="#d7dee5",
            stub_indent_length="18px",
            row_striping_background_color="#f4f7f6",
            source_notes_font_size="12px",
        )
        .cols_hide(columns="kind")
    )
    if agent_labels:
        tbl = tbl.tab_stub_indent(rows=agent_labels, indent=2)
    if parent_labels:
        tbl = tbl.tab_style(
            style=style.text(weight="bold"),
            locations=[
                loc.body(rows=parent_labels),
                loc.stub(rows=parent_labels),
            ],
        )
    if overall_labels:
        tbl = tbl.tab_style(
            style=[style.fill(color="#e6f4ef"), style.text(weight="bold")],
            locations=[
                loc.body(rows=overall_labels),
                loc.stub(rows=overall_labels),
            ],
        )
    return tbl


def _distribution_table(df: pd.DataFrame, subtitle: str) -> GT:
    tbl = GT(
        df[["label", "kind", "sessions", "turns"]],
        rowname_col="label",
        id="acct-dist",
    )
    tbl = (
        tbl.tab_header(title="账号分布", subtitle=subtitle)
        .cols_label(sessions="会话", turns="轮次")
        .fmt_integer(columns=["sessions", "turns"])
        .cols_width(sessions="90px", turns="90px")
    )
    return _style_table(tbl, df.to_dict("records"), table_width="100%")


def _latency_table(df: pd.DataFrame, subtitle: str) -> GT:
    metric_cols = [
        "stt_n",
        "stt_p50",
        "stt_p95",
        "stt_p99",
        "agent_n",
        "agent_p50",
        "agent_p95",
        "agent_p99",
        "agg_n",
        "agg_p50",
        "agg_p95",
        "agg_p99",
        "first_n",
        "first_p50",
        "first_p95",
        "first_p99",
        "hears_n",
        "hears_p50",
        "hears_p95",
        "hears_p99",
    ]
    percentile_cols = [
        "stt_p50",
        "stt_p95",
        "stt_p99",
        "agent_p50",
        "agent_p95",
        "agent_p99",
        "agg_p50",
        "agg_p95",
        "agg_p99",
        "first_p50",
        "first_p95",
        "first_p99",
        "hears_p50",
        "hears_p95",
        "hears_p99",
    ]
    tbl = GT(
        df[["label", "kind", *metric_cols]],
        rowname_col="label",
        id="latency",
    )
    tbl = (
        tbl.tab_header(
            title="延迟指标",
            subtitle=subtitle,
        )
        .tab_spanner(
            label="STT 定稿",
            columns=["stt_n", "stt_p50", "stt_p95", "stt_p99"],
        )
        .tab_spanner(
            label="Agent 首字",
            columns=["agent_n", "agent_p50", "agent_p95", "agent_p99"],
        )
        .tab_spanner(
            label="TTS 聚合",
            columns=["agg_n", "agg_p50", "agg_p95", "agg_p99"],
        )
        .tab_spanner(
            label="TTS 首音",
            columns=["first_n", "first_p50", "first_p95", "first_p99"],
        )
        .tab_spanner(
            label="听到声音",
            columns=["hears_n", "hears_p50", "hears_p95", "hears_p99"],
        )
        .cols_label(
            stt_n="n",
            stt_p50="p50",
            stt_p95="p95",
            stt_p99="p99",
            agent_n="n",
            agent_p50="p50",
            agent_p95="p95",
            agent_p99="p99",
            agg_n="n",
            agg_p50="p50",
            agg_p95="p95",
            agg_p99="p99",
            first_n="n",
            first_p50="p50",
            first_p95="p95",
            first_p99="p99",
            hears_n="n",
            hears_p50="p50",
            hears_p95="p95",
            hears_p99="p99",
        )
        .fmt_integer(columns=["stt_n", "agent_n", "agg_n", "first_n", "hears_n"])
        .fmt_number(columns=percentile_cols, decimals=3)
        .sub_missing(missing_text="-")
        .tab_source_note(source_note=md(_NOTE))
    )
    return _style_table(tbl, df.to_dict("records"), table_width="100%")


def render_report(summary: Mapping[str, Any]) -> str:
    day = str(summary.get("date") or "")
    sample_rate = summary.get("sample_rate")
    session_count = summary.get("session_count")
    bits: list[str] = []
    if day:
        bits.append(day)
    if sample_rate is not None:
        bits.append(f"采样率 {sample_rate}%")
    if session_count is not None:
        bits.append(f"会话 {session_count}")
    subtitle = " · ".join(bits) or "日分析"

    rows = _iter_rows(summary)
    df = pd.DataFrame(rows)
    dist_html = _distribution_table(df, subtitle).as_raw_html()
    latency_html = _latency_table(df, "单位：秒").as_raw_html()
    title = f"Smart-voice 日分析{f' · {day}' if day else ''}"

    return (
        "<!DOCTYPE html>\n"
        '<html lang="zh-CN">\n'
        "<head>\n"
        '<meta charset="utf-8"/>\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1"/>\n'
        f"<title>{escape(title)}</title>\n"
        "<style>\n"
        "body{margin:0;background:#f4f6f8;color:#1a1f24;"
        "font-family:IBM Plex Sans,Noto Sans SC,PingFang SC,sans-serif;}\n"
        ".page{max-width:1480px;margin:0 auto;padding:28px 20px 48px;}\n"
        "h1{margin:0 0 6px;font-size:24px;letter-spacing:-0.02em;}\n"
        ".meta{margin:0 0 22px;color:#5c6770;font-size:14px;}\n"
        ".block{margin:0 0 28px;overflow-x:auto;background:#fff;"
        "border:1px solid #d7dee5;border-radius:14px;padding:16px;"
        "box-shadow:0 10px 30px rgba(26,31,36,0.04);}\n"
        "</style>\n"
        "</head>\n"
        "<body>\n"
        '<div class="page">\n'
        f"<h1>{escape(title)}</h1>\n"
        f'<p class="meta">{escape(subtitle)}</p>\n'
        f'<div class="block">{dist_html}</div>\n'
        f'<div class="block">{latency_html}</div>\n'
        "</div>\n"
        "</body>\n"
        "</html>\n"
    )


def write_reports(
    report_dir: Path,
    summary: Mapping[str, Any],
) -> tuple[Path, Path]:
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / "report.html"
    summary_path = report_dir / "summary.json"
    report_path.write_text(render_report(summary), encoding="utf-8")
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return report_path, summary_path
