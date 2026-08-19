"""Multi-track WAV writer regression without devices or cloud services."""

import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

from audio_clock import RecordingSampleClock
from audio_recorder import AudioRecorder


def read_frames(path: Path) -> tuple[int, int, int]:
    with wave.open(str(path), "rb") as wav:
        return wav.getnframes(), wav.getframerate(), wav.getnchannels()


def test_abrupt_process_exit_is_still_readable(root: Path) -> None:
    """每次 write 都刷新 WAV 头，异常终止后已有帧仍可被播放器读取。"""
    target = root / "abrupt-exit.wav"
    child_code = (
        "import sys, time; "
        "from audio_recorder import AudioRecorder; "
        "recorder = AudioRecorder(sys.argv[1], 16000, 1); "
        "recorder.write(b'\\x00\\x00' * 6400); "
        "print('frame-written', flush=True); "
        "time.sleep(30)"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", child_code, str(target)],
        cwd=str(Path(__file__).resolve().parents[1]),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "frame-written"
        child.kill()
        child.wait(timeout=10)
        assert read_frames(target) == (6400, 16_000, 1)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)


def test_accelerated_three_hour_logical_run(root: Path) -> None:
    """用低采样率压缩墙钟，真实执行 10,800 秒逻辑三轨写入。"""
    sample_rate = 100
    logical_seconds = 3 * 60 * 60
    frame = b"\x00\x00" * sample_rate
    paths = {
        "mixed": str(root / "stress-mixed.wav"),
        "mic": str(root / "stress-mic.wav"),
        "system": str(root / "stress-system.wav"),
    }
    recorder = AudioRecorder(paths["mixed"], sample_rate, 1, track_paths=paths)
    clock = RecordingSampleClock(sample_rate, 1)
    started = time.perf_counter()
    for _ in range(logical_seconds):
        recorder.write_tracks(mixed=frame, mic=frame, system=frame)
        clock.advance(frame, recorded=True)
    result = recorder.close()
    elapsed = time.perf_counter() - started

    expected_frames = logical_seconds * sample_rate
    assert clock.recorded_ms == logical_seconds * 1000
    for name, path in paths.items():
        assert read_frames(Path(path)) == (expected_frames, sample_rate, 1), name
        assert result["tracks"][name]["ok"] is True
        assert result["tracks"][name]["seconds"] == float(logical_seconds)
    print(
        f"accelerated logical 3h: tracks=3 frames={expected_frames} "
        f"wall={elapsed:.2f}s"
    )


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="mc-audio-recorder-") as raw:
        root = Path(raw)
        paths = {
            "mixed": str(root / "mixed.wav"),
            "mic": str(root / "mic.wav"),
            "system": str(root / "system.wav"),
        }
        recorder = AudioRecorder(paths["mixed"], 16_000, 1, track_paths=paths)
        mic = b"\x01\x00" * 640
        system = b"\x02\x00" * 640
        for _ in range(5):
            recorder.write_tracks(mixed=bytes(640 * 2), mic=mic, system=system)
        result = recorder.close()
        assert set(result["tracks"]) == {"mixed", "mic", "system"}
        for name, path in paths.items():
            assert Path(path).is_file(), name
            assert read_frames(Path(path)) == (3200, 16_000, 1), name
            assert result["tracks"][name]["ok"] is True

        # A broken auxiliary target must not stop the mixed writer.
        broken_parent = root / "broken-parent"
        broken_parent.write_bytes(b"not a directory")
        recorder = AudioRecorder(
            str(root / "mixed-2.wav"),
            16_000,
            1,
            track_paths={"mic": str(broken_parent / "mic.wav")},
        )
        recorder.write_tracks(mixed=b"\x00\x00" * 320, mic=b"\x01\x00" * 320)
        result = recorder.close()
        assert Path(result["path"]).is_file()
        assert result["tracks"]["mixed"]["ok"] is True
        assert result["tracks"]["mic"]["ok"] is False
        assert result["tracks"]["mic"].get("error")
        test_abrupt_process_exit_is_still_readable(root)
        test_accelerated_three_hour_logical_run(root)
    print("ok: multi-track audio recorder")


if __name__ == "__main__":
    main()
