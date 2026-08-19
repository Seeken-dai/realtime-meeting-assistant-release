"""线上会议双通道混音的无网络回归测试。"""

import numpy as np

from online_audio_stream import (
    MicrophoneStreamError,
    OnlineMeetingStream,
    SystemAudioStreamError,
)


class FakeStream:
    def __init__(self, pcm, info=None):
        self.pcm = pcm
        self.info = info
        self.opened = False

    def __enter__(self):
        self.opened = True
        return self

    def __exit__(self, *_args):
        self.opened = False

    def frames(self):
        while self.opened:
            yield self.pcm


class FailingStream(FakeStream):
    def __init__(self, message):
        super().__init__(b"")
        self.message = message

    def __enter__(self):
        raise RuntimeError(self.message)


class RuntimeFailingStream(FakeStream):
    def __init__(self, pcm, message):
        super().__init__(pcm)
        self.message = message

    def frames(self):
        yield self.pcm
        raise RuntimeError(self.message)


def test_channel_identity_and_mix():
    samples = 640
    mic_pcm = np.full(samples, 1000, dtype=np.int16).tobytes()
    system_pcm = np.full(samples, 2000, dtype=np.int16).tobytes()
    system = FakeStream(
        system_pcm,
        info={
            "index": 17,
            "name": "测试扬声器 [Loopback]",
            "channels": 2,
            "sampleRate": 48000,
        },
    )
    stream = OnlineMeetingStream(
        sample_rate=16000,
        frame_ms=40,
        mic_source=FakeStream(mic_pcm),
        system_source=system,
    )
    with stream:
        mixed, actual_mic, actual_system = next(stream.frames())
    assert actual_mic == mic_pcm
    assert actual_system == system_pcm
    assert np.all(np.frombuffer(mixed, dtype=np.int16) == 1500)
    assert stream.loopback_info["name"] == "测试扬声器 [Loopback]"


def test_open_failure_is_attributed_to_the_right_channel():
    working = FakeStream(bytes(640 * 2))
    try:
        with OnlineMeetingStream(
            mic_source=FailingStream("麦克风被占用"),
            system_source=working,
        ):
            pass
    except MicrophoneStreamError as exc:
        assert "被占用" in str(exc)
    else:
        raise AssertionError("麦克风启动失败没有被分类")

    try:
        with OnlineMeetingStream(
            mic_source=working,
            system_source=FailingStream("找不到默认回环设备"),
        ):
            pass
    except SystemAudioStreamError as exc:
        assert "回环设备" in str(exc)
    else:
        raise AssertionError("系统回环启动失败没有被分类")


def test_runtime_failure_degrades_only_the_failed_channel():
    samples = 640
    mic_pcm = np.full(samples, 1000, dtype=np.int16).tobytes()
    system_pcm = np.full(samples, 2000, dtype=np.int16).tobytes()
    stream = OnlineMeetingStream(
        sample_rate=16000,
        frame_ms=40,
        mic_source=RuntimeFailingStream(mic_pcm, "麦克风被拔出"),
        system_source=FakeStream(system_pcm),
    )
    with stream:
        frames = stream.frames()
        first = next(frames)
        second = next(frames)
        assert first[1] == mic_pcm
        assert first[2] == system_pcm
        # 麦克风源已结束，系统源和混音仍继续，失败通道用等长静音填充。
        assert second[1] == bytes(len(mic_pcm))
        assert second[2] == system_pcm
        assert np.all(np.frombuffer(second[0], dtype=np.int16) == 1000)
        errors = stream.drain_runtime_errors()
        assert errors == [
            {
                "channel": "microphone",
                "stage": "microphone",
                "message": "麦克风被拔出",
            }
        ]
        assert stream.source_status()["microphone"] == {
            "ok": False,
            "degraded": True,
        }
        assert stream.source_status()["system"]["ok"] is True


if __name__ == "__main__":
    test_channel_identity_and_mix()
    test_open_failure_is_attributed_to_the_right_channel()
    test_runtime_failure_degrades_only_the_failed_channel()
    print("ok: online channels + mixing + startup/runtime failure attribution")
