"""UserStoppedSpeaking → BotStartedSpeaking 配对。"""

from __future__ import annotations

import unittest

from smart_analyzer.report import build_summary
from smart_analyzer.scan import scan_docs

_SID = "SmartVoice-1-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _line(body: str) -> dict[str, str]:
    return {
        "message": f"2026-09-17 17:00:00.000 | INFO    | [ID: {_SID}] - {body}"
    }


def _frame(
    ts: float,
    rid: int,
    name: str,
    *,
    source: str = "LLMUserAggregator",
    direction: str = "d",
) -> dict[str, str]:
    return _line(
        f"w1 {ts:.6f} F {rid} {name}#{rid} {source}#{rid} {direction} {rid} -"
    )


class UserHearsSoundScanTest(unittest.TestCase):
    def test_welcome_bot_started_is_ignored(self) -> None:
        sample = scan_docs(
            _SID,
            [
                _frame(
                    1.0,
                    1,
                    "BotStartedSpeakingFrame",
                    source="FastAPIWebsocketOutputTransport",
                )
            ],
        )
        self.assertEqual(sample.turns, 0)
        self.assertEqual(sample.user_hears_sound, [])

    def test_simple_user_stop_to_bot_started(self) -> None:
        sample = scan_docs(
            _SID,
            [
                _frame(10.0, 1, "UserStoppedSpeakingFrame"),
                _frame(
                    11.5,
                    2,
                    "BotStartedSpeakingFrame",
                    source="FastAPIWebsocketOutputTransport",
                ),
            ],
        )
        self.assertEqual(sample.turns, 1)
        self.assertEqual(sample.user_hears_sound, [1.5])

    def test_continuation_overwrites_start(self) -> None:
        sample = scan_docs(
            _SID,
            [
                _frame(12.693, 1, "UserStoppedSpeakingFrame"),
                _frame(13.909, 2, "InterruptionFrame"),
                _frame(14.796, 3, "UserStoppedSpeakingFrame"),
                _frame(
                    16.293,
                    4,
                    "BotStartedSpeakingFrame",
                    source="FastAPIWebsocketOutputTransport",
                ),
            ],
        )
        self.assertEqual(sample.turns, 2)
        self.assertEqual(len(sample.user_hears_sound), 1)
        self.assertAlmostEqual(sample.user_hears_sound[0], 16.293 - 14.796)

    def test_interrupt_without_second_stop_still_pairs(self) -> None:
        sample = scan_docs(
            _SID,
            [
                _frame(10.0, 1, "UserStoppedSpeakingFrame"),
                _frame(10.4, 2, "InterruptionFrame"),
                _frame(
                    11.4,
                    3,
                    "BotStartedSpeakingFrame",
                    source="FastAPIWebsocketOutputTransport",
                ),
            ],
        )
        self.assertEqual(len(sample.user_hears_sound), 1)
        self.assertAlmostEqual(sample.user_hears_sound[0], 1.4)

    def test_orphan_user_stop_is_not_sampled(self) -> None:
        sample = scan_docs(
            _SID,
            [
                _frame(10.0, 1, "UserStoppedSpeakingFrame"),
                _frame(
                    11.5,
                    2,
                    "BotStartedSpeakingFrame",
                    source="FastAPIWebsocketOutputTransport",
                ),
                _frame(20.0, 3, "UserStoppedSpeakingFrame"),
            ],
        )
        self.assertEqual(sample.turns, 2)
        self.assertEqual(sample.user_hears_sound, [1.5])

    def test_upstream_bot_started_is_ignored(self) -> None:
        sample = scan_docs(
            _SID,
            [
                _frame(10.0, 1, "UserStoppedSpeakingFrame"),
                _frame(
                    10.2,
                    2,
                    "BotStartedSpeakingFrame",
                    source="FastAPIWebsocketOutputTransport",
                    direction="u",
                ),
                _frame(
                    11.0,
                    3,
                    "BotStartedSpeakingFrame",
                    source="FastAPIWebsocketOutputTransport",
                ),
            ],
        )
        self.assertEqual(sample.user_hears_sound, [1.0])

    def test_report_includes_hears_stats(self) -> None:
        sample = scan_docs(
            _SID,
            [
                _frame(10.0, 1, "UserStoppedSpeakingFrame"),
                _frame(
                    12.0,
                    2,
                    "BotStartedSpeakingFrame",
                    source="FastAPIWebsocketOutputTransport",
                ),
            ],
        )
        sample.account_id = "N1"
        sample.agent_id = "a1"
        summary = build_summary([sample], {"N1": "测试"})
        hears = summary["overall"]["user_hears_sound"]
        self.assertEqual(hears["count"], 1.0)
        self.assertEqual(hears["p50"], 2.0)


if __name__ == "__main__":
    unittest.main()
