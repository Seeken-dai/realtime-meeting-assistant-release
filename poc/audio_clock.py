"""录音与 ASR 共用的采样时钟。

供应商时间戳通常相对“当前 ASR 会话”，而播放器时间相对 WAV 文件。
两者只有在没有暂停、丢帧、重连时才碰巧相等。本类以实际写入 WAV 的
PCM 采样数为真值，并记录“送给 ASR 的采样位置 → WAV 采样位置”的映射。
"""

from __future__ import annotations

import threading
from typing import List, Optional, Tuple


class RecordingSampleClock:
    """把 ASR 会话毫秒映射为 WAV 文件毫秒。"""

    def __init__(self, sample_rate: int, channels: int = 1):
        self.sample_rate = max(1, int(sample_rate))
        self.channels = max(1, int(channels))
        self._bytes_per_frame = 2 * self.channels  # 16-bit PCM
        self._lock = threading.Lock()
        self._recorded_frames = 0
        self._session_submitted_frames = 0
        self._accepting_audio = True
        # (ASR会话开始帧, ASR会话结束帧, WAV开始帧)
        self._runs: List[Tuple[int, int, int]] = []

    def set_accepting_audio(self, accepting: bool) -> None:
        """标记当前 ASR 连接是否真的接收音频。"""
        with self._lock:
            self._accepting_audio = bool(accepting)

    def reset_asr_session(self) -> None:
        """ASR 重连后供应商时间归零，但 WAV 时间继续累计。"""
        with self._lock:
            self._session_submitted_frames = 0
            self._runs.clear()
            self._accepting_audio = True

    def advance(self, pcm: bytes, *, recorded: bool) -> None:
        """登记一块 PCM。

        recorded=True 表示该块已写入 WAV。暂停时仍可能给 ASR 发送等长静音，
        此时 recorded=False，映射会自动压掉暂停区间。
        """
        frames = len(pcm) // self._bytes_per_frame
        if frames <= 0:
            return
        with self._lock:
            submitted_start = self._session_submitted_frames
            recorded_start = self._recorded_frames
            if self._accepting_audio:
                submitted_end = submitted_start + frames
                if recorded:
                    if (
                        self._runs
                        and self._runs[-1][1] == submitted_start
                        and self._runs[-1][2]
                        + (self._runs[-1][1] - self._runs[-1][0])
                        == recorded_start
                    ):
                        run_start, _, run_recorded_start = self._runs[-1]
                        self._runs[-1] = (
                            run_start,
                            submitted_end,
                            run_recorded_start,
                        )
                    else:
                        self._runs.append(
                            (submitted_start, submitted_end, recorded_start)
                        )
                self._session_submitted_frames = submitted_end
            if recorded:
                self._recorded_frames += frames

    def map_ms(self, session_ms: object) -> Optional[int]:
        """将供应商会话时间映射到 WAV 毫秒；无法证明有效时返回 None。"""
        if session_ms is None:
            return None
        try:
            value = float(session_ms)
        except (TypeError, ValueError):
            return None
        if value < 0:
            return None
        target = int(round(value * self.sample_rate / 1000.0))
        with self._lock:
            # 容许供应商四舍五入超过当前已送帧一个 20–40ms 音频块。
            tolerance = max(1, self.sample_rate // 20)
            if target > self._session_submitted_frames + tolerance:
                return None
            target = min(target, self._session_submitted_frames)
            previous_recorded_end = self._runs[0][2] if self._runs else self._recorded_frames
            for stream_start, stream_end, recorded_start in self._runs:
                if target < stream_start:
                    # 落在暂停区间：WAV 时钟停在上一块真实音频的末尾。
                    return self._frames_to_ms(previous_recorded_end)
                if target <= stream_end:
                    return self._frames_to_ms(
                        recorded_start + max(0, target - stream_start)
                    )
                previous_recorded_end = recorded_start + (stream_end - stream_start)
            return self._frames_to_ms(previous_recorded_end)

    @property
    def recorded_ms(self) -> int:
        with self._lock:
            return self._frames_to_ms(self._recorded_frames)

    def _frames_to_ms(self, frames: int) -> int:
        return int(round(frames * 1000.0 / self.sample_rate))
