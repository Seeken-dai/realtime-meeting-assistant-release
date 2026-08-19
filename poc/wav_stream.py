"""把一段录音当成麦克风回放，用来在【不开会】的情况下跑通整条会中链路。

## 为什么需要它

会中链路（阿里实时转写 → 本地声纹 → 按说话人切分 → 建议闸门 → LLM）
过去只能靠"下次开会"来验证，一次验证成本是一场真实会议，而且不可重复：
同样的话说不了第二遍，改一行代码就得再开一场会。

本模块提供与 `MicStream` **完全相同的接口**（上下文管理器 + `frames()`），
喂的是 wav 文件里的音频。于是：

  - 已经录下来的会议可以**反复重放**，改任何一处都能立刻回归验证；
  - 长跑测试（PRD 要求连续 ≥2 小时）不必真的开 2 小时会；3 小时仅用于加速压力测试；
  - 出了问题能拿同一段音频复现，而不是"上次开会好像有个现象"。

## ⚠️ 必须按真实节奏喂

建议触发依赖**墙上时间**（debounce、20s 冷却、最小增量），
一口气把音频灌进去会让这些闸门全部失真——所有 final 挤在几秒内到达，
看起来像"一场会只出一批建议"，而那是回放的假象，不是真实行为。
所以默认 `speed=1.0` 按真实时长回放：5 分钟录音就跑 5 分钟。

`speed > 1` 只适合压测吞吐，**不能用来判断建议触发是否正常**。
"""

import sys
import time
import wave


class WavStream:
    """上下文管理器：把 wav 当麦克风按真实节奏产出 PCM 帧。

    用法与 MicStream 完全一致：
        with WavStream(path, sample_rate=16000, frame_ms=40) as src:
            for pcm_bytes in src.frames():
                asr.send(pcm_bytes)
    """

    def __init__(self, path, sample_rate=16000, channels=1, frame_ms=40,
                 speed=1.0, on_progress=None):
        self.path = str(path)
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        self.frame_ms = int(frame_ms)
        self.frame_size = int(sample_rate * frame_ms / 1000)
        self.speed = max(0.1, float(speed or 1.0))
        self.on_progress = on_progress
        self._wav = None
        self.duration_sec = 0.0

    def __enter__(self):
        self._wav = wave.open(self.path, "rb")
        # 参数不符就直接报错，别静默重采样：声纹模型按 16k 单声道训练，
        # 悄悄转过来会让这里跑出的结论和真实链路对不上
        if self._wav.getnchannels() != self.channels:
            raise ValueError(
                f"需要 {self.channels} 声道，实际 {self._wav.getnchannels()}")
        if self._wav.getsampwidth() != 2:
            raise ValueError("需要 16-bit PCM")
        if self._wav.getframerate() != self.sample_rate:
            raise ValueError(
                f"需要 {self.sample_rate}Hz，实际 {self._wav.getframerate()}Hz")
        self.duration_sec = self._wav.getnframes() / float(self.sample_rate)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._wav is not None:
            self._wav.close()
            self._wav = None

    def frames(self):
        """按真实节奏产出 PCM 帧；文件读完即结束（不像麦克风那样无限等待）。"""
        started = time.time()
        sent = 0
        next_report = 0.0
        try:
            while True:
                data = self._wav.readframes(self.frame_size)
                if not data:
                    break
                # 不足一帧的尾巴补零，保证帧长恒定（ASR 与 VAD 都按定长切）
                expected = self.frame_size * 2 * self.channels
                if len(data) < expected:
                    data = data + bytes(expected - len(data))
                sent += 1
                audio_sec = sent * self.frame_ms / 1000.0
                # 按真实时长节流：音频已推进的时间 vs 墙上已过去的时间
                target = started + audio_sec / self.speed
                delay = target - time.time()
                if delay > 0:
                    time.sleep(delay)
                if self.on_progress and audio_sec >= next_report:
                    next_report = audio_sec + 15.0
                    try:
                        self.on_progress(audio_sec, self.duration_sec)
                    except Exception:
                        pass
                yield data
        except KeyboardInterrupt:
            return


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("用法：python wav_stream.py <wav> [speed]")
    speed = float(sys.argv[2]) if len(sys.argv) > 2 else 1.0
    with WavStream(sys.argv[1], speed=speed) as src:
        n = 0
        t0 = time.time()
        for _ in src.frames():
            n += 1
        print(f"共 {n} 帧 / 音频 {src.duration_sec:.1f}s / "
              f"实际耗时 {time.time() - t0:.1f}s（speed={speed}）")
