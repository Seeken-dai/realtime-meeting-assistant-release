"""
方案 D1 运行时模块：本地声纹认「我」。

与 verify_speaker.py / eval_labeled_enroll.py 共用模型与阈值口径：
  - CAM++ onnx（poc/models/3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx）
  - Silero VAD
  - 建议阈值 0.65（真注册留出；结论见 ../docs/engineering/HANDOFF.md §4，复现见 poc/eval/README.md）

会中：对实时 PCM 做 VAD → embedding → 1:1 verify，给无说话人 ID 的 ASR
（如阿里 Paraformer）打上 me / other。

⚠️ 注册 wav 必须与开会同一麦克风、相近距离。
"""

from __future__ import annotations

import os
import threading
import wave
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence, Tuple

import numpy as np

try:
    import sherpa_onnx as so
except ImportError as exc:  # pragma: no cover
    raise ImportError("需要 sherpa-onnx：pip install sherpa-onnx") from exc


_HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SPK_MODEL = os.path.join(
    _HERE, "models", "3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx"
)
DEFAULT_VAD_MODEL = os.path.join(_HERE, "models", "silero_vad_v5.onnx")

MIN_SEG_SEC = 0.7
SAMPLE_RATE = 16000

SPEAKER_ID_ME = "me"
SPEAKER_ID_OTHER = "other"

# ── 会话内自适应判据 ────────────────────────────────────────────
# 固定阈值的致命弱点：它假设「注册信道」与「开会信道」完全一致。
# 实测（2026-07-27）这个假设很脆——系统降噪开/关就让整体分数漂移 >0.1，
# 固定 0.65 直接从「认出 1 段」变成「认出 0 段」。
#
# 但同一场会内部，「我」与「对方」的分数**成两拨**：绝对分数可以漂，相对结构不漂。
# 所以改成每场会自己找这两拨的分界。
MIN_ADAPTIVE_SEGMENTS = 6   # 段数不够时分不出两拨，先用固定阈值
ADAPTIVE_FLOOR = 0.40       # 最高分低于此 → 认为本场没有「我」，不强行切
ADAPTIVE_MIN_SPREAD = 0.10  # 分数极差太小 → 全场只有一个人，退回固定阈值
ADAPTIVE_MAX_ITER = 50      # 2-means 迭代上限（实测几轮就收敛）
HYSTERESIS = 0.04           # 迟滞：贴着切点的段不来回翻标签

# 没有句首时间戳时，一条 final 最多回看多久（秒）——防止把很早的语音卷进来
MAX_LOOKBACK_SEC = 60.0

# ── 切会议音频的 VAD 粒度 ────────────────────────────────────────
# 段是「能单独归属给一个人」的最小单位：段太长 = 一段里混了好几个人，
# 再准的声纹也只能给它一个标签，切分也切不出来。
#
# 实测对比（`python eval_vad_granularity.py --wav <会议wav>`，
# 真值取会后聚类，指标是"有多少秒语音被归给了对的人"）：
#
#   粒度        段数  段纯度  归属正确  最差前缀
#   20s/0.50s    31   94.5%   94.5%    89.6%   ← 曾经的会中默认值
#   12s/0.40s    43   98.9%   98.0%    96.2%
#    8s/0.35s    52  100.0%  100.0%    98.9%   ← 现值，会中会后一致
#    6s/0.30s    60  100.0%   98.5%    88.0%
#    4s/0.25s    69  100.0%   93.5%    86.6%
#
# 两端都会变差，原因不同：粗了段里混人（纯度掉），细了 embedding 太短、
# 分数噪声大（纯度满分但判定反而错）。8s/0.35s 是这场数据上的最优。
#
# ⚠️ 只改切会议音频的粒度，**注册仍用默认值**：上面的对比就是在
#    "注册不变、只变会议分段"的条件下测的，一起改就超出了数据支持的范围。
MEETING_VAD_MAX_SPEECH = 8.0
MEETING_VAD_MIN_SILENCE = 0.35


@dataclass
class MeSegment:
    start_sec: float
    end_sec: float
    score: float
    is_me: bool


def find_adaptive_cut(scores: Sequence[float]) -> Optional[float]:
    """把一批 1:1 分数分成「我 / 对方」两拨，返回分界；分不出返回 None。

    用一维 2-means（切点取两个质心的中点）。

    ⚠️ **不要改回"找最大断层（argmax gaps）"**。那是本函数的初版，实测会错得
       很难看：对方自己的分数也是分簇的，最大断层经常落在**对方内部**。
       实测一场会（52 段，真值来自会后聚类）：
         · me/other 之间的断层 0.049，对方内部 0.231↔0.397 的断层 0.166
         · argmax 选了后者 → 切点掉到下限 0.32 → 分类准确率只有 73%
         · 后果不只是显示错：「我」说话不触发建议，那 210 秒一批建议都没出
       2-means 用的是"两拨各自的中心"，不受某一拨内部结构影响，
       同一组数据准确率 100%（各前缀平均 100%、最差 94%）。
       回归测试：`python -m tests.test_adaptive_cut`。

    会中（MeIdentifier）与会后（diarize_offline）用**同一套判据**——
    两边给出不同的「我」会让用户完全没法理解系统在干什么。
    返回 None 表示无法可靠判断，调用方应退回固定阈值。
    """
    arr = np.array([float(s) for s in scores], dtype=np.float64)
    if len(arr) < MIN_ADAPTIVE_SEGMENTS:
        return None
    # 护栏一：最高分都不够高 → 本场大概没有「我」在说话，别硬切
    if arr.max() < ADAPTIVE_FLOOR:
        return None
    # 护栏二：分数挤成一团 → 全场只有一个人，硬分成两拨只会制造假的「对方」
    if arr.max() - arr.min() < ADAPTIVE_MIN_SPREAD:
        return None

    # 一维 2-means：用极值做种子，两个质心的中点即分界
    high, low = float(arr.max()), float(arr.min())
    for _ in range(ADAPTIVE_MAX_ITER):
        cut = (high + low) / 2.0
        upper = arr[arr >= cut]
        lower = arr[arr < cut]
        if len(upper) == 0 or len(lower) == 0:
            return None
        new_high, new_low = float(upper.mean()), float(lower.mean())
        if abs(new_high - high) < 1e-9 and abs(new_low - low) < 1e-9:
            break
        high, low = new_high, new_low

    # 护栏三：分界不能低到把明显是对方的段也收进来。
    # 宁可漏认自己（少触发几条建议），也不要把对方当成我（整段不触发建议）。
    return float(max((high + low) / 2.0, ADAPTIVE_FLOOR * 0.8))


def pcm16_to_float32(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0


def read_wav_mono16k(path: str) -> np.ndarray:
    with wave.open(path, "rb") as f:
        if f.getnchannels() != 1:
            raise ValueError(f"需要单声道 wav，实际 {f.getnchannels()} 声道")
        if f.getsampwidth() != 2:
            raise ValueError("需要 16-bit PCM")
        if f.getframerate() != SAMPLE_RATE:
            raise ValueError(
                f"需要 16kHz，实际 {f.getframerate()}Hz（模型按 16k 训练）"
            )
        raw = f.readframes(f.getnframes())
    return pcm16_to_float32(raw)


def _make_vad(
    vad_model: str,
    *,
    min_silence_duration: float = 0.5,
    max_speech_duration: float = 20.0,
) -> Tuple["so.VoiceActivityDetector", int]:
    """默认值给【注册】用；切会议音频请显式传 MEETING_VAD_*（见下）。"""
    cfg = so.VadModelConfig(
        silero_vad=so.SileroVadModelConfig(
            model=vad_model,
            threshold=0.5,
            min_silence_duration=min_silence_duration,
            min_speech_duration=0.25,
            max_speech_duration=max_speech_duration,
        ),
        sample_rate=SAMPLE_RATE,
    )
    vad = so.VoiceActivityDetector(cfg, buffer_size_in_seconds=60)
    return vad, cfg.silero_vad.window_size


def _embed_waveform(extractor, samples: np.ndarray) -> Optional[np.ndarray]:
    if len(samples) < int(MIN_SEG_SEC * SAMPLE_RATE):
        return None
    stream = extractor.create_stream()
    stream.accept_waveform(sample_rate=SAMPLE_RATE, waveform=samples)
    stream.input_finished()
    vec = np.array(extractor.compute(stream), dtype=np.float32)
    return vec / (np.linalg.norm(vec) + 1e-9)


def _vad_segment_samples(vad_model: str, samples: np.ndarray) -> List[np.ndarray]:
    vad, window = _make_vad(vad_model)
    out: List[np.ndarray] = []
    for i in range(0, len(samples), window):
        chunk = samples[i : i + window]
        if len(chunk) < window:
            break
        vad.accept_waveform(chunk)
        while not vad.empty():
            out.append(np.array(vad.front.samples, dtype=np.float32))
            vad.pop()
    vad.flush()
    while not vad.empty():
        out.append(np.array(vad.front.samples, dtype=np.float32))
        vad.pop()
    return out


def enroll_from_wav(
    enroll_wav,
    *,
    spk_model: str = DEFAULT_SPK_MODEL,
    vad_model: str = DEFAULT_VAD_MODEL,
    num_threads: int = 2,
) -> Tuple["so.SpeakerEmbeddingExtractor", "so.SpeakerEmbeddingManager", int]:
    """从注册 wav 构建 extractor + manager。返回 (extractor, manager, n_segs)。

    `enroll_wav` 可以是单个路径，也可以是路径列表 —— 多段注册把所有样本的
    embedding 一起喂给 manager。多次短录（不同时间、不同状态）比一次长录
    更能覆盖你的音色变化，实测单次 20s 只切得出 3 段，样本太少。
    """
    paths = [enroll_wav] if isinstance(enroll_wav, (str, bytes, os.PathLike)) else list(enroll_wav)
    paths = [str(p) for p in paths if p]
    if not paths:
        raise FileNotFoundError("没有提供任何注册 wav")
    missing = [p for p in paths if not os.path.isfile(p)]
    if missing:
        raise FileNotFoundError(f"找不到注册 wav：{missing[0]}")
    if not os.path.isfile(spk_model):
        raise FileNotFoundError(f"找不到声纹模型：{spk_model}")
    if not os.path.isfile(vad_model):
        raise FileNotFoundError(f"找不到 VAD 模型：{vad_model}")

    extractor = so.SpeakerEmbeddingExtractor(
        so.SpeakerEmbeddingExtractorConfig(model=spk_model, num_threads=num_threads)
    )
    vectors = []
    for path in paths:
        samples = read_wav_mono16k(path)
        for seg in _vad_segment_samples(vad_model, samples):
            vec = _embed_waveform(extractor, seg)
            if vec is not None:
                vectors.append(vec)
    if not vectors:
        raise RuntimeError(
            "注册音频未检出有效语音（请用同一支开会麦克风连续说满 15–20 秒）"
        )
    manager = so.SpeakerEmbeddingManager(int(vectors[0].shape[0]))
    manager.add("我", [v.tolist() for v in vectors])
    return extractor, manager, len(vectors)


class MeIdentifier:
    """实时「是不是我」判别器。feed_pcm 在采集线程；label_at 在 ASR 回调线程。"""

    def __init__(
        self,
        enroll_wav,          # 单个路径或路径列表（多段注册）
        *,
        threshold: float = 0.65,
        spk_model: Optional[str] = None,
        vad_model: Optional[str] = None,
        num_threads: int = 2,
        on_segment: Optional[Callable[[MeSegment], None]] = None,
        adaptive: bool = True,
    ):
        self.threshold = float(threshold)
        self.adaptive = bool(adaptive)
        self._adaptive_cut: Optional[float] = None
        self.on_segment = on_segment
        self._lock = threading.Lock()
        self._segments: List[MeSegment] = []
        self._stream_samples = 0
        self._vad_buf = np.zeros(0, dtype=np.float32)
        self._ready = False
        self._error: Optional[str] = None
        self._enroll_segments = 0

        try:
            spk = spk_model or DEFAULT_SPK_MODEL
            vad_path = vad_model or DEFAULT_VAD_MODEL
            self._extractor, self._manager, n = enroll_from_wav(
                enroll_wav,
                spk_model=spk,
                vad_model=vad_path,
                num_threads=num_threads,
            )
            self._enroll_segments = n
            # 会中切会议音频用细粒度；注册（enroll_from_wav 内部）仍用默认值
            self._vad, self._window = _make_vad(
                vad_path,
                min_silence_duration=MEETING_VAD_MIN_SILENCE,
                max_speech_duration=MEETING_VAD_MAX_SPEECH,
            )
            self._ready = True
        except Exception as exc:
            self._error = str(exc)
            self._ready = False

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def error(self) -> Optional[str]:
        return self._error

    @property
    def enroll_segments(self) -> int:
        return self._enroll_segments

    def _score(self, vec: np.ndarray) -> float:
        return float(self._manager.score("我", vec.tolist()))

    def _compute_cut_locked(self) -> Optional[float]:
        """从本场已有分数里找「我 / 对方」的断层切点。须持锁调用。

        返回 None 表示"无法可靠判断"，调用方应退回固定阈值。
        """
        if not self.adaptive:
            return None
        return find_adaptive_cut([s.score for s in self._segments])

    def _reclassify_locked(self) -> None:
        """按当前切点重标全部段落，并施加时序迟滞。须持锁调用。

        为什么要重标历史段：切点随本场样本变化，早期用固定阈值判错的段
        应该被纠正——`label_at` 读的就是这里写的 is_me。
        """
        cut = self._compute_cut_locked()
        self._adaptive_cut = cut
        eff = cut if cut is not None else self.threshold
        prev_me = False
        for seg in self._segments:
            # 迟滞只让「已经是我」的状态更黏，不抬高入选门槛 ——
            # 否则 threshold 的语义被悄悄改严（实测会把 0.652 挡在 0.65 之外）。
            bar = eff - HYSTERESIS if prev_me else eff
            seg.is_me = seg.score >= bar
            prev_me = seg.is_me

    @property
    def adaptive_cut(self) -> Optional[float]:
        return self._adaptive_cut

    def _finish_segment(
        self, samples: np.ndarray, start_sample: int, end_sample: int
    ) -> None:
        if (end_sample - start_sample) / SAMPLE_RATE < MIN_SEG_SEC:
            return
        vec = _embed_waveform(self._extractor, samples)
        if vec is None:
            return
        score = self._score(vec)
        seg = MeSegment(
            start_sec=start_sample / SAMPLE_RATE,
            end_sec=end_sample / SAMPLE_RATE,
            score=score,
            is_me=score >= self.threshold,
        )
        with self._lock:
            self._segments.append(seg)
            if len(self._segments) > 4000:
                self._segments = self._segments[-2000:]
            self._reclassify_locked()
            seg = self._segments[-1]      # 取回重标后的判定
        if self.on_segment:
            try:
                self.on_segment(seg)
            except Exception:
                pass

    def _drain_vad_locked(self) -> List[Tuple[np.ndarray, int, int]]:
        jobs: List[Tuple[np.ndarray, int, int]] = []
        while not self._vad.empty():
            front = self._vad.front
            seg_samples = np.array(front.samples, dtype=np.float32)
            end_sample = self._stream_samples
            start_sample = max(0, end_sample - len(seg_samples))
            self._vad.pop()
            jobs.append((seg_samples, start_sample, end_sample))
        return jobs

    def feed_pcm(self, pcm: bytes) -> None:
        if not self._ready or not pcm:
            return
        samples = pcm16_to_float32(pcm)
        jobs: List[Tuple[np.ndarray, int, int]] = []
        with self._lock:
            self._vad_buf = np.concatenate([self._vad_buf, samples])
            while len(self._vad_buf) >= self._window:
                chunk = self._vad_buf[: self._window]
                self._vad_buf = self._vad_buf[self._window :]
                self._vad.accept_waveform(chunk)
                self._stream_samples += self._window
                jobs.extend(self._drain_vad_locked())
        for seg_samples, start_s, end_s in jobs:
            self._finish_segment(seg_samples, start_s, end_s)

    def flush(self) -> None:
        if not self._ready:
            return
        jobs: List[Tuple[np.ndarray, int, int]] = []
        with self._lock:
            self._vad.flush()
            jobs = self._drain_vad_locked()
        for seg_samples, start_s, end_s in jobs:
            self._finish_segment(seg_samples, start_s, end_s)

    def label_at(self, end_ms: Optional[float] = None) -> Tuple[str, float]:
        """返回 (speakerId, score)。无段时 other/0 —— 宁可漏认自己，勿把对方当我。"""
        with self._lock:
            segs = list(self._segments)
        if not segs:
            return SPEAKER_ID_OTHER, 0.0

        if end_ms is not None and end_ms > 0:
            t = float(end_ms) / 1000.0
            covering = [
                s for s in segs if s.start_sec - 0.15 <= t <= s.end_sec + 0.35
            ]
            if covering:
                me_hits = [s for s in covering if s.is_me]
                best = (
                    max(me_hits, key=lambda s: s.score)
                    if me_hits
                    else max(covering, key=lambda s: s.end_sec)
                )
                return (
                    SPEAKER_ID_ME if best.is_me else SPEAKER_ID_OTHER,
                    best.score,
                )
            nearest = min(segs, key=lambda s: abs(s.end_sec - t))
            if abs(nearest.end_sec - t) <= 2.5:
                return (
                    SPEAKER_ID_ME if nearest.is_me else SPEAKER_ID_OTHER,
                    nearest.score,
                )

        last = segs[-1]
        return (
            SPEAKER_ID_ME if last.is_me else SPEAKER_ID_OTHER,
            last.score,
        )

    def spans_between(
        self, begin_ms: Optional[float], end_ms: Optional[float]
    ) -> List[MeSegment]:
        """取覆盖 [begin_ms, end_ms] 的语音段，供把一条长 final 切成多段。

        ⚠️ `label_at` 只回答"句尾那一刻是谁"。阿里的一条 final 可以长达 40 秒、
           中间换好几次人（实测），只看句尾必然把别人的话算到一个人头上。
           要按说话人切开这条 final，就得拿到这段时间里**全部**语音段。

        begin_ms 缺失时退回「上一段之后的全部」不安全（会把很早的历史卷进来），
        因此只取 end 之前 MAX_LOOKBACK_SEC 内的段。
        """
        if end_ms is None or end_ms <= 0:
            return []
        end_sec = float(end_ms) / 1000.0
        if begin_ms is None or begin_ms <= 0:
            start_sec = end_sec - MAX_LOOKBACK_SEC
        else:
            start_sec = float(begin_ms) / 1000.0
        with self._lock:
            segs = list(self._segments)
        # 端点各留一点余量：VAD 边界与 ASR 句界不会严丝合缝
        lo = start_sec - 0.30
        hi = end_sec + 0.35
        return [s for s in segs if s.end_sec > lo and s.start_sec < hi]

    def stats(self) -> dict:
        with self._lock:
            segs = list(self._segments)
        return {
            "ready": self._ready,
            "error": self._error,
            "threshold": self.threshold,
            "adaptive": self.adaptive,
            # None = 尚未启用自适应（段数不足/无明显断层），此时用的是固定阈值
            "adaptiveCut": self._adaptive_cut,
            "enrollSegments": self._enroll_segments,
            "segments": len(segs),
            "meSegments": sum(1 for s in segs if s.is_me),
        }
