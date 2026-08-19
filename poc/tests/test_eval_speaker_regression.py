"""说话人回归指标的纯逻辑测试；不加载模型、录音或数据库。"""

from eval_speaker_regression import check_expectations, evaluate_segments


def main() -> None:
    fixture = {
        "scope": "me-vs-other",
        "startedAt": 1_000_000,
        "audioSeconds": 12.0,
        "labels": [
            {"id": "a", "at": 1_004_000, "isMe": True},
            {"id": "b", "at": 1_008_000, "isMe": False},
            {"id": "c", "at": 1_012_000, "isMe": True},
        ],
    }
    diarization = {
        "durationSec": 12.0,
        "segments": [
            {"start": 0.0, "end": 4.0, "speakerId": "me"},
            {"start": 4.0, "end": 8.0, "speakerId": "spk1"},
            {"start": 8.0, "end": 12.0, "speakerId": "spk1"},
        ],
    }
    result = evaluate_segments(fixture, diarization)
    assert result["item"]["confusion"] == {"tp": 1, "tn": 1, "fp": 0, "fn": 1}
    assert result["item"]["accuracy"] == 0.6667
    assert result["speechWeighted"]["evaluatedSeconds"] == 12.0
    assert result["errorCount"] == 1
    assert result["largestErrors"][0]["id"] == "c"
    gate = check_expectations(
        {
            "expectations": {
                "meDecision": "cluster",
                "maxSpeakerCount": 2,
                "minItemAccuracy": 0.60,
            }
        },
        {"meDecision": "cluster", "speakerCount": 2},
        result,
    )
    assert gate["passed"]
    failed_gate = check_expectations(
        {"expectations": {"meDecision": "cluster", "minItemAccuracy": 0.90}},
        {"meDecision": "threshold", "speakerCount": 2},
        result,
    )
    assert not failed_gate["passed"]
    assert len(failed_gate["failures"]) == 2
    print("ok: speaker regression metrics")


if __name__ == "__main__":
    main()
