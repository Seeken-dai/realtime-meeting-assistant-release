"""
麦克风采集模块（厂商无关）。

采集系统默认（或指定）麦克风，输出 16kHz / 单声道 / 16-bit PCM 的音频帧，
通过回调交给上层 ASR 适配器发送。

依赖：sounddevice、numpy
"""

import queue
import sys

import numpy as np
import sounddevice as sd


def list_input_devices():
    """打印所有可用的音频输入设备，方便用户挑选 device index。"""
    print("可用音频输入设备：")
    for idx, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] > 0:
            default_mark = " (默认)" if idx == sd.default.device[0] else ""
            print(f"  [{idx}] {dev['name']}{default_mark} "
                  f"— {dev['max_input_channels']}ch @ {int(dev['default_samplerate'])}Hz")


class MicStream:
    """
    上下文管理器：进入后开始采集，退出后停止。

    用法：
        with MicStream(sample_rate=16000, frame_ms=40) as mic:
            for pcm_bytes in mic.frames():
                asr.send(pcm_bytes)
    """

    def __init__(self, sample_rate=16000, channels=1, frame_ms=40, device=None):
        self.sample_rate = sample_rate
        self.channels = channels
        self.frame_size = int(sample_rate * frame_ms / 1000)  # 每帧采样点数
        self.device = device
        self._q = queue.Queue()
        self._stream = None

    def _callback(self, indata, frames, time_info, status):
        if status:
            print(f"[mic] 状态告警: {status}", file=sys.stderr)
        # indata 为 float32 [-1,1]，转成 16-bit PCM 小端字节流
        pcm16 = (indata[:, 0] * 32767).astype(np.int16)
        self._q.put(pcm16.tobytes())

    def __enter__(self):
        self._stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=self.channels,
            dtype="float32",
            blocksize=self.frame_size,
            device=self.device,
            callback=self._callback,
        )
        self._stream.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()

    def frames(self):
        """生成器：持续产出 PCM 音频帧（bytes）。Ctrl+C 时优雅退出。"""
        try:
            while True:
                yield self._q.get()
        except KeyboardInterrupt:
            return


if __name__ == "__main__":
    # 单独运行本文件可查看设备列表并做一次拾音自检（打印音量）
    list_input_devices()
    print("\n开始拾音自检，对着麦克风说话，观察音量条。Ctrl+C 结束。\n")
    with MicStream() as mic:
        for pcm in mic.frames():
            level = np.abs(np.frombuffer(pcm, dtype=np.int16)).mean()
            bar = "#" * int(level / 200)
            print(f"\r音量 {int(level):5d} |{bar:<40}|", end="", flush=True)
