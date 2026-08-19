"""Online diarization safety invariants without loading audio models."""

import inspect

from diarize_offline import (
    align_online_transcript,
    coerce_online_remote_clusters,
    diarize_online,
)


def main() -> None:
    online_params = inspect.signature(diarize_online).parameters
    assert "speaker_count" in online_params
    assert "expected_speaker_count" not in online_params
    result = coerce_online_remote_clusters(
        {
            "ok": True,
            "durationSec": 12.0,
            "segments": [
                {"start": 0.0, "end": 4.0, "speakerId": "c0"},
                {"start": 4.0, "end": 8.0, "speakerId": "c1"},
                {"start": 8.0, "end": 12.0, "speakerId": "c2"},
            ],
            "speakers": [],
        },
        max_remote_clusters=2,
    )
    assert result["systemAudioOnly"] is True
    assert result["microphoneFixed"] == "me"
    assert result["remoteClusters"] == 2
    assert result["speakerCount"] == 3
    assert result["confidence"] == "coarse"
    assert all(item["speakerId"] != "me" for item in result["segments"])

    aligned = align_online_transcript(
        {
            **result,
            "segments": [
                {"start": 0.0, "end": 4.0, "speakerId": "remote-1"},
                {"start": 4.0, "end": 8.0, "speakerId": "remote-2"},
            ],
            "speakers": [
                {"id": "remote-1", "name": "远端1"},
                {"id": "remote-2", "name": "远端2"},
            ],
        },
        [
            {"id": "m", "speakerId": "me", "speaker": "我", "text": "本地", "isFinal": True, "at": 1_002_000},
            {"id": "r", "speakerId": "other", "speaker": "对方", "text": "远端", "isFinal": True, "at": 1_006_000},
        ],
        1_000_000,
        split=False,
    )
    assert aligned[0]["speakerId"] == "me"
    assert all(item["speakerId"] != "me" for item in aligned[1:])
    print("ok: online system-only diarization")


if __name__ == "__main__":
    main()
