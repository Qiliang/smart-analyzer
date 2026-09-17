"""按会话扫描目标帧/事件，抽出账号与四类指标样本（无 Turn 状态机）。"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

_LOG_RE = re.compile(
    r"^(?P<time>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}) \| \s*\w+\s* \| "
    r"\[ID: (?P<sid>[^\]]*)\] - (?P<body>.*)$",
    re.S,
)
_W1_RE = re.compile(r"^w1 (?P<ts>\d+\.\d+) (?P<tag>[SF]) (?P<rest>.*)$", re.S)
_AGENT_ID_RE = re.compile(r"\bagent_id=['\"]([^'\"]+)['\"]")
_ACCOUNT_ID_RE = re.compile(r"\baccount_id=['\"]?(N\d+)['\"]?")


@dataclass
class _Frame:
    ts: float
    name: str
    source: str
    direction: str
    payload: dict[str, Any]


@dataclass
class SessionSample:
    """单会话扫描结果。"""

    session_id: str
    account_id: str = ""
    agent_id: str = ""
    turns: int = 0
    stt_confirm_lags: list[float] = field(default_factory=list)
    agent_first_token: list[float] = field(default_factory=list)
    tts_agg_lags: list[float] = field(default_factory=list)
    tts_first_audio: list[float] = field(default_factory=list)


def _session_id_matches(line_sid: str, query: str) -> bool:
    if not line_sid or not query:
        return False
    return (
        line_sid == query
        or line_sid.endswith("-" + query)
        or query.endswith("-" + line_sid)
    )


def _parse_frame(ts: float, rest: str) -> _Frame | None:
    parts = rest.split(" ", 5)
    if len(parts) < 6:
        return None
    _rid_s, name, source, direction, _, payload_s = parts
    payload: dict[str, Any] = {}
    if payload_s != "-":
        try:
            parsed = json.loads(payload_s)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            payload = parsed
    return _Frame(
        ts=ts,
        name=name.split("#", 1)[0],
        source=source.split("#", 1)[0],
        direction=direction,
        payload=payload,
    )


def _parse_event(body: str) -> dict[str, Any] | None:
    stripped = body.strip()
    if "'event': 'run_bot'" in stripped or '"event": "run_bot"' in stripped:
        agent = _AGENT_ID_RE.search(stripped)
        account = _ACCOUNT_ID_RE.search(stripped)
        return {
            "name": "run_bot",
            "agent_id": agent.group(1) if agent else "",
            "account_id": account.group(1) if account else "",
        }
    if not stripped.startswith("{'event'") and not stripped.startswith('{"event"'):
        return None
    try:
        data = ast.literal_eval(stripped)
    except (ValueError, SyntaxError):
        return None
    if not isinstance(data, dict):
        return None
    name = data.get("event")
    if not isinstance(name, str):
        return None
    out = dict(data)
    out["name"] = name
    return out


def _mpaas_first_token_value(payload: dict[str, Any]) -> float | None:
    """MetricsFrame 中 model=MPAAS_AGENT 且带 value、不含 ttfat 的条目。"""
    data = payload.get("data")
    if not isinstance(data, list):
        return None
    for item in data:
        if not isinstance(item, dict):
            continue
        if item.get("model") != "MPAAS_AGENT":
            continue
        if "ttfat" in item:
            continue
        value = item.get("value")
        if isinstance(value, (int, float)) and value > 0:
            return float(value)
    return None


def _tts_first_audio_from_metrics(frame: _Frame) -> float | None:
    """TTS MetricsFrame 里显式带 ttfb 的条目。"""
    if "TTS" not in frame.source and "Tts" not in frame.source:
        return None
    data = frame.payload.get("data")
    if not isinstance(data, list):
        return None
    for item in data:
        if not isinstance(item, dict):
            continue
        if item.get("model") == "MPAAS_AGENT":
            continue
        ttfb = item.get("ttfb")
        if isinstance(ttfb, (int, float)) and ttfb >= 0:
            return float(ttfb)
    return None


def scan_docs(session_id: str, docs: Sequence[dict[str, Any]]) -> SessionSample:
    """扫一遍日志，只认需求相关帧/事件。"""
    sample = SessionSample(session_id=session_id)
    last_volc_interim_ts: float | None = None
    pending_llm_text_ts: float | None = None
    # 每次 Agent 响应开始后只收第一条 MPAAS_AGENT value（首字符）
    agent_first_armed = False
    saw_tts_first_audio_event = False
    tts_metrics_fallback: list[float] = []

    for source in docs:
        message = source.get("message") or ""
        if not isinstance(message, str):
            continue
        m = _LOG_RE.match(message)
        if not m or not _session_id_matches(m.group("sid").strip(), session_id):
            continue
        body = m.group("body")

        w1 = _W1_RE.match(body)
        if w1 is not None:
            if w1.group("tag") != "F":
                continue
            frame = _parse_frame(float(w1.group("ts")), w1.group("rest"))
            if frame is None or frame.direction != "d":
                continue

            if frame.name == "UserStoppedSpeakingFrame":
                sample.turns += 1
                continue

            if frame.name == "LLMFullResponseStartFrame":
                agent_first_armed = True
                continue

            if frame.name == "InterimTranscriptionFrame":
                if "VolcengineSTT" in frame.source:
                    last_volc_interim_ts = frame.ts
                continue

            if frame.name in (
                "TranscriptionWithSpeakerFrame",
                "TranscriptionFrame",
            ):
                if "VolcengineSTT" in frame.source and last_volc_interim_ts is not None:
                    lag = frame.ts - last_volc_interim_ts
                    if lag >= 0:
                        sample.stt_confirm_lags.append(lag)
                last_volc_interim_ts = None
                continue

            if frame.name == "MetricsFrame":
                if agent_first_armed:
                    agent_v = _mpaas_first_token_value(frame.payload)
                    if agent_v is not None:
                        sample.agent_first_token.append(agent_v)
                        agent_first_armed = False
                tts_v = _tts_first_audio_from_metrics(frame)
                if tts_v is not None:
                    tts_metrics_fallback.append(tts_v)
                continue

            if frame.name == "LLMTextFrame":
                if pending_llm_text_ts is None:
                    pending_llm_text_ts = frame.ts
                continue

            if frame.name == "AggregatedTextFrame":
                if pending_llm_text_ts is not None:
                    lag = frame.ts - pending_llm_text_ts
                    if lag >= 0:
                        sample.tts_agg_lags.append(lag)
                    pending_llm_text_ts = None
                continue
            continue

        event = _parse_event(body)
        if event is None:
            continue
        if event["name"] == "run_bot":
            if event.get("account_id"):
                sample.account_id = str(event["account_id"])
            if event.get("agent_id"):
                sample.agent_id = str(event["agent_id"])
        elif event["name"] == "on_tts_first_audio":
            ttfb = event.get("ttfb")
            if isinstance(ttfb, (int, float)) and ttfb >= 0:
                sample.tts_first_audio.append(float(ttfb))
                saw_tts_first_audio_event = True

    if not saw_tts_first_audio_event and tts_metrics_fallback:
        sample.tts_first_audio.extend(tts_metrics_fallback)

    if not sample.account_id:
        sample.account_id = "-"
    if not sample.agent_id:
        sample.agent_id = "-"
    return sample
