"""VAD 停声 → 定稿 / BotStartedSpeaking 配对。"""

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


def _vad_start(ts: float, rid: int) -> dict[str, str]:
    return _frame(ts, rid, "VADUserStartedSpeakingFrame")


def _vad_stop(ts: float, rid: int, *, direction: str = "d") -> dict[str, str]:
    return _frame(ts, rid, "VADUserStoppedSpeakingFrame", direction=direction)


def _final(
    ts: float,
    rid: int,
    *,
    source: str = "VolcengineSTTService",
) -> dict[str, str]:
    return _frame(ts, rid, "TranscriptionWithSpeakerFrame", source=source)


def _bot(ts: float, rid: int, *, direction: str = "d") -> dict[str, str]:
    return _frame(
        ts,
        rid,
        "BotStartedSpeakingFrame",
        source="FastAPIWebsocketOutputTransport",
        direction=direction,
    )


class UserHearsSoundScanTest(unittest.TestCase):
    def test_welcome_bot_started_is_ignored(self) -> None:
        sample = scan_docs(_SID, [_bot(1.0, 1)])
        self.assertEqual(sample.turns, 0)
        self.assertEqual(sample.user_hears_sound, [])

    def test_simple_vad_stop_to_bot_started(self) -> None:
        sample = scan_docs(
            _SID,
            [
                _vad_stop(10.0, 1),
                _frame(10.2, 2, "UserStoppedSpeakingFrame"),
                _bot(11.5, 3),
            ],
        )
        self.assertEqual(sample.turns, 1)
        self.assertEqual(sample.user_hears_sound, [1.5])

    def test_continuation_keeps_last_stop(self) -> None:
        sample = scan_docs(
            _SID,
            [
                _vad_stop(12.693, 1),
                _vad_start(13.909, 2),
                _vad_stop(14.796, 3),
                _bot(16.293, 4),
            ],
        )
        self.assertEqual(len(sample.user_hears_sound), 1)
        self.assertAlmostEqual(sample.user_hears_sound[0], 16.293 - 14.796)

    def test_missing_stop_is_not_sampled(self) -> None:
        sample = scan_docs(
            _SID,
            [
                _vad_start(10.0, 1),
                _bot(11.5, 2),
            ],
        )
        self.assertEqual(sample.user_hears_sound, [])

    def test_interrupt_without_new_vad_start_still_pairs(self) -> None:
        sample = scan_docs(
            _SID,
            [
                _vad_stop(10.0, 1),
                _frame(10.4, 2, "InterruptionFrame"),
                _bot(11.4, 3),
            ],
        )
        self.assertAlmostEqual(sample.user_hears_sound[0], 1.4)

    def test_orphan_vad_stop_is_not_sampled(self) -> None:
        sample = scan_docs(
            _SID,
            [
                _vad_stop(10.0, 1),
                _bot(11.5, 2),
                _vad_stop(20.0, 3),
            ],
        )
        self.assertEqual(sample.user_hears_sound, [1.5])

    def test_upstream_bot_started_is_ignored(self) -> None:
        sample = scan_docs(
            _SID,
            [
                _vad_stop(10.0, 1),
                _bot(10.2, 2, direction="u"),
                _bot(11.0, 3),
            ],
        )
        self.assertEqual(sample.user_hears_sound, [1.0])

    def test_upstream_vad_stop_is_ignored(self) -> None:
        sample = scan_docs(
            _SID,
            [
                _vad_stop(10.0, 1, direction="u"),
                _bot(11.0, 2),
            ],
        )
        self.assertEqual(sample.user_hears_sound, [])

    def test_restart_before_bot_drops_previous_stop(self) -> None:
        sample = scan_docs(
            _SID,
            [
                _vad_stop(10.0, 1),
                _vad_start(12.0, 2),
                _bot(12.5, 3),
            ],
        )
        self.assertEqual(sample.user_hears_sound, [])

    def test_report_includes_hears_stats(self) -> None:
        sample = scan_docs(
            _SID,
            [
                _vad_stop(10.0, 1),
                _bot(12.0, 2),
            ],
        )
        sample.account_id = "N1"
        sample.agent_id = "a1"
        summary = build_summary([sample], {"N1": "测试"})
        hears = summary["overall"]["user_hears_sound"]
        self.assertEqual(hears["count"], 1.0)
        self.assertEqual(hears["p50"], 2.0)


class SttConfirmScanTest(unittest.TestCase):
    def test_simple_vad_stop_to_final(self) -> None:
        sample = scan_docs(
            _SID,
            [
                _vad_stop(10.0, 1),
                _final(10.911, 2),
            ],
        )
        self.assertAlmostEqual(sample.stt_confirm_lags[0], 0.911)

    def test_continuation_keeps_last_stop(self) -> None:
        sample = scan_docs(
            _SID,
            [
                _vad_stop(10.0, 1),
                _vad_start(11.0, 2),
                _vad_stop(12.0, 3),
                _final(13.0, 4),
            ],
        )
        self.assertEqual(sample.stt_confirm_lags, [1.0])

    def test_missing_stop_is_not_sampled(self) -> None:
        sample = scan_docs(
            _SID,
            [
                _frame(10.0, 1, "InterimTranscriptionFrame", source="VolcengineSTTService"),
                _final(11.0, 2),
            ],
        )
        self.assertEqual(sample.stt_confirm_lags, [])

    def test_start_without_stop_is_not_sampled(self) -> None:
        sample = scan_docs(
            _SID,
            [
                _vad_start(10.0, 1),
                _final(10.3, 2),
            ],
        )
        self.assertEqual(sample.stt_confirm_lags, [])

    def test_late_stop_after_consumed_final_does_not_pair_next(self) -> None:
        sample = scan_docs(
            _SID,
            [
                _vad_stop(10.0, 1),
                _final(11.0, 2),
                _vad_stop(12.0, 3),
                _final(13.0, 4),
            ],
        )
        self.assertEqual(sample.stt_confirm_lags, [1.0])

    def test_skipped_final_does_not_lock_later_stop(self) -> None:
        sample = scan_docs(
            _SID,
            [
                _vad_start(10.0, 1),
                _final(11.0, 2),
                _vad_stop(12.0, 3),
                _final(13.0, 4),
            ],
        )
        self.assertEqual(sample.stt_confirm_lags, [1.0])

    def test_same_stop_feeds_stt_and_hears(self) -> None:
        sample = scan_docs(
            _SID,
            [
                _vad_stop(10.0, 1),
                _final(11.0, 2),
                _bot(12.0, 3),
            ],
        )
        self.assertEqual(sample.stt_confirm_lags, [1.0])
        self.assertEqual(sample.user_hears_sound, [2.0])

    def test_upstream_vad_stop_is_ignored(self) -> None:
        sample = scan_docs(
            _SID,
            [
                _vad_stop(10.0, 1, direction="u"),
                _vad_stop(11.0, 2),
                _final(12.0, 3),
            ],
        )
        self.assertEqual(sample.stt_confirm_lags, [1.0])

    def test_non_volc_final_does_not_consume_anchor(self) -> None:
        sample = scan_docs(
            _SID,
            [
                _vad_stop(10.0, 1),
                _final(11.0, 2, source="XfyunSTTService"),
                _final(12.0, 3),
            ],
        )
        self.assertEqual(sample.stt_confirm_lags, [2.0])

    def test_transcription_frame_is_not_a_final(self) -> None:
        sample = scan_docs(
            _SID,
            [
                _vad_stop(10.0, 1),
                _frame(11.0, 2, "TranscriptionFrame", source="VolcengineSTTService"),
                _final(12.0, 3),
            ],
        )
        self.assertEqual(sample.stt_confirm_lags, [2.0])


if __name__ == "__main__":
    unittest.main()
