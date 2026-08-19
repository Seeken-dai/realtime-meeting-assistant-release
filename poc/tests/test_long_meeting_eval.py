"""Unit tests for long-meeting and timeline quantifiers (no real user data)."""

from __future__ import annotations

from eval_long_meeting import gap_stats, quartile_activity
from eval_timeline_axis import lag_stats, monotonic_checks, sample_anchors


def test_quartile_and_gaps():
    items = [
        {"start": 0.0, "end": 10.0},
        {"start": 100.0, "end": 110.0},
        {"start": 300.0, "end": 320.0},
    ]
    buckets = quartile_activity(items, 400.0)
    assert len(buckets) == 4
    assert buckets[0]["hasSpeech"] is True
    assert buckets[3]["hasSpeech"] is True
    gaps = gap_stats(items, 400.0)
    assert gaps["maxGapSec"] >= 180
    assert gaps["gapsOver60s"] >= 1


def test_lag_and_monotonic():
    started = 1_000_000.0
    items = [
        {
            "id": "a",
            "start": 0.5,
            "end": 1.5,
            "speaker": "me",
            "_raw": {
                "at": started + 2500,
                "audioStartMs": 500,
                "audioEndMs": 1500,
            },
        },
        {
            "id": "b",
            "start": 2.0,
            "end": 3.0,
            "speaker": "other",
            "_raw": {
                "at": started + 4000,
                "audioStartMs": 2000,
                "audioEndMs": 3000,
            },
        },
    ]
    lags = lag_stats(items, started)
    assert lags["explicitRanges"] == 2
    # wall-end lag ≈ 1000ms for both
    assert lags["wallMinusAudioEndMs"]["median"] == 1000.0
    assert lags["wallMinusAudioEndMs"]["within1s"] == 1.0
    mono = monotonic_checks(items)
    assert mono["ok"] is True
    anchors = sample_anchors(items, 2)
    assert len(anchors) == 2


def main() -> None:
    test_quartile_and_gaps()
    test_lag_and_monotonic()
    print("ok: long meeting + timeline unit metrics")


if __name__ == "__main__":
    main()
