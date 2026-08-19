"""Pure logic checks for the privacy-safe real meeting evaluator."""

import tempfile
import wave
from pathlib import Path

from eval_real_meetings import (
    anchor_rows,
    intersection_seconds,
    me_metrics,
    percentile,
    timeline_metrics,
    union_intervals,
    version_items,
)
from verify_online_tracks import build_report


def write_test_wav(path: Path, value: int, seconds: float = 2.0) -> None:
    frames = int(16_000 * seconds)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16_000)
        wav.writeframes((int(value).to_bytes(2, "little", signed=True)) * frames)


def main() -> None:
    assert percentile([1, 2, 3, 4], 0.5) == 2.5
    assert union_intervals([(0, 1), (0.5, 2), (3, 4)]) == [(0, 2), (3, 4)]
    assert intersection_seconds([(0, 2), (4, 5)], [(1, 3), (4.5, 6)]) == 1.5

    realtime = version_items(
        {
            "transcript": [
                {"id": "a", "speakerId": "me", "isFinal": True, "audioStartMs": 0, "audioEndMs": 1000},
                {"id": "b", "speakerId": "other", "isFinal": True, "audioStartMs": 1000, "audioEndMs": 2000},
            ]
        },
        1_000_000,
        2.0,
    )
    offline = version_items(
        {
            "transcript": [
                {"id": "a", "speakerId": "me", "isFinal": True, "audioStartMs": 100, "audioEndMs": 1100},
                {"id": "b", "speakerId": "spk1", "isFinal": True, "audioStartMs": 1100, "audioEndMs": 2000},
            ]
        },
        1_000_000,
        2.0,
    )
    metrics = me_metrics(realtime, offline)
    assert metrics["mePrecision"] == 0.9
    assert metrics["meRecall"] == 0.9
    timeline = timeline_metrics(realtime, offline)
    assert timeline["matchedItems"] == 2
    assert timeline["combinedMs"]["median"] == 100.0

    anchors = anchor_rows(
        {"caseId": "case", "anchorCount": 2}, realtime, offline
    )
    assert len(anchors) == 2
    assert anchors[0]["goldIsMe"] is True
    assert anchors[1]["goldRemoteSpeaker"] == "remote_unlabeled"

    with tempfile.TemporaryDirectory(prefix="mc-online-track-test-") as raw:
        root = Path(raw)
        meeting_id = "meeting-online-track-test"
        write_test_wav(root / f"{meeting_id}.wav", 900)
        write_test_wav(root / f"{meeting_id}.mic.wav", 500)
        write_test_wav(root / f"{meeting_id}.system.wav", 700)
        track_report = build_report(
            root / "missing.sqlite",
            root,
            meeting_id=meeting_id,
            require=True,
        )
        assert track_report["gate"]["passed"] is True
        assert track_report["candidates"][0]["durationDeltaMs"] == 0.0
        (root / f"{meeting_id}.system.wav").unlink()
        missing_report = build_report(
            root / "missing.sqlite",
            root,
            meeting_id=meeting_id,
            require=True,
        )
        assert missing_report["gate"]["passed"] is False
    print("ok: real meeting evaluation metrics")


if __name__ == "__main__":
    main()
