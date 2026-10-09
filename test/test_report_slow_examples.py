"""各 agent 自身 p95～p99 慢例子。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from smart_analyzer.report import build_summary, render_report, write_reports
from smart_analyzer.scan import SessionSample


def _sample(
    session_id: str,
    lags: list[float],
    *,
    account_id: str = "N1",
    agent_id: str = "a1",
) -> SessionSample:
    return SessionSample(
        session_id=session_id,
        account_id=account_id,
        agent_id=agent_id,
        stt_confirm_lags=lags,
    )


def _stt_examples(summary: dict) -> list[dict]:
    return [row for row in summary["slow_examples"] if row["metric"] == "stt_confirm"]


class SlowExamplesTest(unittest.TestCase):
    def test_picks_three_slowest_sessions_in_band(self) -> None:
        samples = [_sample(f"s{i:03d}", [float(i)]) for i in range(100)]
        summary = build_summary(samples, {})
        rows = _stt_examples(summary)
        self.assertEqual([row["session_id"] for row in rows], ["s098", "s097", "s096"])
        self.assertEqual([row["latency"] for row in rows], [98.0, 97.0, 96.0])
        self.assertEqual(rows[0]["p95"], 94.0)
        self.assertEqual(rows[0]["p99"], 98.0)
        self.assertEqual(rows[0]["account_id"], "N1")
        self.assertEqual(rows[0]["agent_id"], "a1")
        self.assertEqual(rows[0]["metric_label"], "STT 定稿")

    def test_same_session_keeps_larger_lag(self) -> None:
        samples = [_sample(f"s{i:03d}", [float(i)]) for i in range(97)]
        samples.append(_sample("dup", [97.0, 98.0]))
        samples.append(_sample("s099", [99.0]))
        summary = build_summary(samples, {})
        rows = _stt_examples(summary)
        self.assertEqual([row["session_id"] for row in rows], ["dup", "s096", "s095"])
        self.assertEqual(rows[0]["latency"], 98.0)

    def test_empty_metric_omitted(self) -> None:
        summary = build_summary([_sample("s000", [1.0])], {})
        self.assertEqual(
            {row["metric"] for row in summary["slow_examples"]},
            {"stt_confirm"},
        )
        empty = build_summary(
            [SessionSample(session_id="s", account_id="N1", agent_id="a1")],
            {},
        )
        self.assertEqual(empty["slow_examples"], [])

    def test_report_html_includes_examples(self) -> None:
        samples = [_sample(f"s{i:03d}", [float(i)]) for i in range(100)]
        html = render_report(build_summary(samples, {}))
        self.assertIn("慢延迟例子", html)
        self.assertIn("s098", html)

        empty_html = render_report(
            build_summary(
                [SessionSample(session_id="s", account_id="N1", agent_id="a1")],
                {},
            )
        )
        self.assertNotIn("慢延迟例子", empty_html)

    def test_writes_badcase_session_ids(self) -> None:
        sid = "SmartVoice-10006-bf503db7-5731-41d7-8129-717d175b55c0"
        samples = [
            _sample(f"{sid[:-3]}{i:03d}", [float(i)]) for i in range(100)
        ]
        summary = build_summary(samples, {})
        with tempfile.TemporaryDirectory() as tmp:
            write_reports(Path(tmp), summary)
            text = (Path(tmp) / "badcases.txt").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("session_id\tmetric\tagent_id\taccount_id\tlatency\n"))
        self.assertIn(f"{sid[:-3]}098\tstt_confirm\ta1\tN1\t98.000\n", text)


if __name__ == "__main__":
    unittest.main()
