"""Pure tests for confidence labels and safe downgrade metadata."""

from diarize_offline import assess_diarization_confidence


def main() -> None:
    high = assess_diarization_confidence(
        {
            "segments": [{"speakerId": "me"}] * 6,
            "speakers": [
                {"id": "me", "segments": 6, "seconds": 42},
                {"id": "spk1", "segments": 5, "seconds": 31},
            ],
            "enrollUsed": True,
            "meDecision": "cluster",
        },
        expected_speaker_count=2,
    )
    assert high["status"] == "high"
    assert high["reasons"] == []

    coarse = assess_diarization_confidence(
        {
            "segments": [{"speakerId": "spk1"}] * 2,
            "speakers": [{"id": "spk1", "segments": 2, "seconds": 3}],
            "enrollUsed": True,
            "meDecision": "threshold",
        },
        expected_speaker_count=2,
    )
    assert coarse["status"] == "coarse"
    assert coarse["reasons"]

    empty = assess_diarization_confidence({"segments": [], "speakers": []})
    assert empty["status"] == "not_recommended"
    assert empty["score"] == 0.0

    print("ok: diarization confidence and safe downgrade")


if __name__ == "__main__":
    main()
