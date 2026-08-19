"""Windows 线上会议双通道音频源。

麦克风仍用已验证稳定的 sounddevice/MME；系统播放使用
PyAudioWPatch 的 WASAPI loopback。对上层固定输出 16kHz 单声道 PCM：
  (混音录音, 麦克风/我, 系统播放/对方)
"""

import queue
import threading
import time

import numpy as np

from mic_stream import MicStream

try:
    import pyaudiowpatch as pyaudio
except ImportError:  # pragma: no cover - 由运行时给出可操作错误
    pyaudio = None


class MicrophoneStreamError(RuntimeError):
    """线上模式的麦克风通道无法启动或中途失效。"""


class SystemAudioStreamError(RuntimeError):
    """线上模式的 Windows WASAPI 回环通道无法启动或中途失效。"""


def default_loopback_info():
    if pyaudio is None:
        raise RuntimeError("线上会议缺少 PyAudioWPatch，请重新安装项目依赖")
    audio = pyaudio.PyAudio()
    try:
        info = audio.get_default_wasapi_loopback()
        return {
            "index": int(info["index"]),
            "name": str(info["name"]),
            "channels": int(info["maxInputChannels"]),
            "sampleRate": int(info["defaultSampleRate"]),
        }
    finally:
        audio.terminate()


class SystemLoopbackStream:
    def __init__(self, sample_rate=16000, frame_ms=40):
        self.sample_rate = int(sample_rate)
        self.frame_ms = int(frame_ms)
        self.output_samples = int(self.sample_rate * self.frame_ms / 1000)
        self._audio = None
        self._stream = None
        self._native_rate = 0
        self._channels = 0
        self._native_samples = 0
        self.info = None

    def __enter__(self):
        if pyaudio is None:
            raise RuntimeError("线上会议缺少 PyAudioWPatch，请重新安装项目依赖")
        self._audio = pyaudio.PyAudio()
        device = self._audio.get_default_wasapi_loopback()
        self._native_rate = int(device["defaultSampleRate"])
        self._channels = max(1, int(device["maxInputChannels"]))
        self._native_samples = int(self._native_rate * self.frame_ms / 1000)
        self.info = {
            "index": int(device["index"]),
            "name": str(device["name"]),
            "channels": self._channels,
            "sampleRate": self._native_rate,
        }
        self._stream = self._audio.open(
            format=pyaudio.paInt16,
            channels=self._channels,
            rate=self._native_rate,
            input=True,
            input_device_index=int(device["index"]),
            frames_per_buffer=self._native_samples,
        )
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._stream is not None:
            self._stream.stop_stream()
            self._stream.close()
            self._stream = None
        if self._audio is not None:
            self._audio.terminate()
            self._audio = None

    def frames(self):
        while self._stream is not None:
            raw = self._stream.read(
                self._native_samples,
                exception_on_overflow=False,
            )
            samples = np.frombuffer(raw, dtype=np.int16)
            usable = len(samples) - (len(samples) % self._channels)
            if usable <= 0:
                yield bytes(self.output_samples * 2)
                continue
            mono = samples[:usable].reshape(-1, self._channels).mean(axis=1)
            if len(mono) != self.output_samples:
                source_x = np.arange(len(mono), dtype=np.float64)
                target_x = np.linspace(
                    0,
                    max(len(mono) - 1, 0),
                    self.output_samples,
                )
                mono = np.interp(target_x, source_x, mono)
            yield np.clip(mono, -32768, 32767).astype(np.int16).tobytes()


class OnlineMeetingStream:
    """把两个实时源对齐为固定 40ms 帧，并生成兼容旧回放的单声道混音。"""

    def __init__(
        self,
        sample_rate=16000,
        channels=1,
        frame_ms=40,
        device=None,
        mic_source=None,
        system_source=None,
    ):
        self.sample_rate = int(sample_rate)
        self.frame_ms = int(frame_ms)
        self.frame_samples = int(self.sample_rate * self.frame_ms / 1000)
        self.frame_bytes = self.frame_samples * 2
        self.mic = mic_source or MicStream(
            sample_rate=sample_rate,
            channels=channels,
            frame_ms=frame_ms,
            device=device,
        )
        self.system = system_source or SystemLoopbackStream(
            sample_rate=sample_rate,
            frame_ms=frame_ms,
        )
        self._mic_q = queue.Queue(maxsize=8)
        self._system_q = queue.Queue(maxsize=8)
        self._stop = threading.Event()
        self.loopback_info = None
        self._source_lock = threading.Lock()
        self._runtime_errors = {}
        self._dead_channels = set()

    @staticmethod
    def _put_latest(target, pcm):
        try:
            target.put_nowait(pcm)
        except queue.Full:
            try:
                target.get_nowait()
            except queue.Empty:
                pass
            target.put_nowait(pcm)

    def _record_runtime_error(self, channel, error_type, message):
        """记住一次运行中断，并让调用方继续消费等长静音帧。"""
        with self._source_lock:
            if channel in self._runtime_errors:
                return
            self._runtime_errors[channel] = {
                "channel": channel,
                "stage": "system_audio" if channel == "system" else "microphone",
                "message": str(message) or f"{channel} 音频源已停止",
            }
            self._dead_channels.add(channel)

    def _pump(self, source, target, error_type, channel):
        try:
            for pcm in source.frames():
                if self._stop.is_set():
                    break
                self._put_latest(target, pcm)
        except Exception as exc:
            if not self._stop.is_set():
                self._record_runtime_error(channel, error_type, str(exc))
                # 唤醒等待中的 _next；它会把这一路切成静音，而不是
                # 让另一条仍然正常的物理通道一起被杀掉。
                self._put_latest(target, error_type(str(exc)))
        else:
            if not self._stop.is_set():
                message = "系统回环音频源已停止" if channel == "system" else "麦克风音频源已停止"
                self._record_runtime_error(channel, error_type, message)
                self._put_latest(target, error_type(message))

    def __enter__(self):
        try:
            self.mic.__enter__()
        except Exception as exc:
            raise MicrophoneStreamError(str(exc)) from exc
        try:
            self.system.__enter__()
        except Exception as exc:
            self.mic.__exit__(None, None, None)
            raise SystemAudioStreamError(str(exc)) from exc
        self.loopback_info = self.system.info
        threading.Thread(
            target=self._pump,
            args=(self.mic, self._mic_q, MicrophoneStreamError, "microphone"),
            daemon=True,
        ).start()
        threading.Thread(
            target=self._pump,
            args=(self.system, self._system_q, SystemAudioStreamError, "system"),
            daemon=True,
        ).start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._stop.set()
        self.system.__exit__(exc_type, exc_val, exc_tb)
        self.mic.__exit__(exc_type, exc_val, exc_tb)

    def _next(self, target, channel):
        try:
            item = target.get(timeout=max(self.frame_ms / 500, 0.08))
        except queue.Empty:
            return bytes(self.frame_bytes)
        if isinstance(item, Exception):
            error_type = (
                MicrophoneStreamError
                if channel == "microphone"
                else SystemAudioStreamError
            )
            self._record_runtime_error(channel, error_type, str(item))
            return bytes(self.frame_bytes)
        if len(item) != self.frame_bytes:
            error_type = (
                MicrophoneStreamError
                if channel == "microphone"
                else SystemAudioStreamError
            )
            self._record_runtime_error(
                channel,
                error_type,
                f"{channel} 音频帧长度异常，已切换为静音",
            )
            return bytes(self.frame_bytes)
        return item

    def drain_runtime_errors(self):
        """返回尚未上报给桌面端的运行时音频源错误。"""
        with self._source_lock:
            errors = list(self._runtime_errors.values())
            self._runtime_errors.clear()
            return errors

    def source_status(self):
        """返回两路物理源是否仍在产出，供 ended 事件留痕。"""
        with self._source_lock:
            return {
                "microphone": {
                    "ok": "microphone" not in self._dead_channels,
                    "degraded": "microphone" in self._dead_channels,
                },
                "system": {
                    "ok": "system" not in self._dead_channels,
                    "degraded": "system" in self._dead_channels,
                },
            }

    def frames(self):
        while not self._stop.is_set():
            mic_pcm = self._next(self._mic_q, "microphone")
            system_pcm = self._next(self._system_q, "system")
            mic = np.frombuffer(mic_pcm, dtype=np.int16).astype(np.int32)
            system = np.frombuffer(system_pcm, dtype=np.int16).astype(np.int32)
            # 各留 6dB 余量，避免两路同时讲话时削波。
            mixed = np.clip((mic + system) // 2, -32768, 32767).astype(np.int16)
            yield mixed.tobytes(), mic_pcm, system_pcm
            time.sleep(0)
