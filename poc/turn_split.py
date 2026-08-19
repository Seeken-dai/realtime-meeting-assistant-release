"""把一条过长的 ASR final 按【说话人时间轴】切成可编辑的小段。

## 为什么需要它

阿里 Paraformer 只在「静音」处断句。真实会议里两个人来回接话中间几乎没有静音，
实测（2026-07-27，meeting-1785145910139）一条 final 能覆盖 **47 秒 / 262 字 / 4 次换人**。
而说话人标签是**整条**打的（`speaker_me.label_at(end_ms)` 只看句尾那一刻），
于是：

  - 区分必然错：一条里有两个人，只能给一个标签；
  - 用户改不动：整条是一个可改派单元，想只改其中一小段无从下手；
  - 读不了：界面上堆成一大坨。

三个症状同一个病因——**转写的最小单元太大**。本模块负责把它切小：
时间轴上说话人换了就切，静音够长也切，切完仍过长按标点再切。

## 时间轴对齐

`spans` 与 `char_times` 必须与文本用**同一条时间轴**（秒）：
会中是「开录以来的秒数」（阿里 end_time / VAD `_stream_samples` 同源），
会后是「录音文件内的秒数」。跨轴使用会切在完全无关的位置。

没有词级时间戳时按**语音时长比例**分配字符——粗糙但可用：
断点最终都会吸附到最近的标点，比"整条一个标签"错得少得多。
"""

from __future__ import annotations

import math
from bisect import bisect_left
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

# 句末标点：优先在这里断，断出来的段落读着完整
STRONG_PUNCT = "。！？!?…；;"
# 句中标点：找不到句末标点时的次选
WEAK_PUNCT = "，,、：:"

# ── 默认参数（都可按调用方覆盖）────────────────────────────────
# 同一说话人两段语音间隔超过它 → 认为是两个段落
DEFAULT_PAUSE_SEC = 1.5
# 单段上限：超过就按标点再切，纯粹为了可读 / 可改派
DEFAULT_MAX_CHARS = 110
DEFAULT_MAX_SECONDS = 22.0
# 比这还短的碎片并回同说话人的邻段（不同说话人的短插话保留）
DEFAULT_MIN_CHARS = 4


@dataclass
class Span:
    """一段连续语音及其说话人。start/end 为秒。"""

    start: float
    end: float
    label: Optional[str]

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class Chunk:
    """切分结果的一小段。"""

    text: str
    start: Optional[float]
    end: Optional[float]
    label: Optional[str]


def group_spans(
    spans: Sequence[Span], pause_sec: float = DEFAULT_PAUSE_SEC
) -> List[Span]:
    """把连续同说话人的语音段并成「一次发言」。

    ⚠️ 只有**紧邻**且间隔小于 pause_sec 才合并：同一个人说完一段停了 5 秒再说，
       那是两个段落，合起来读的人分不出节奏（与 App.tsx 的停顿分行同一口径）。
    """
    groups: List[Span] = []
    for span in spans:
        if span.duration <= 0:
            continue
        last = groups[-1] if groups else None
        if last and last.label == span.label and span.start - last.end < pause_sec:
            last.end = span.end
            continue
        groups.append(Span(span.start, span.end, span.label))
    return groups


def _snap_index(text: str, target: int, window: int) -> Optional[int]:
    """把切点吸附到附近的标点后面。附近没有标点就返回 None —— **不切**。

    ⚠️ 别在找不到标点时退回原位置切。时间→字符的换算本来就是估的，
       估偏一点就会切在词中间，界面上出现「但他其实现在」/「还那你的…」
       这种半句（实测出现过）。ASR 没在那里点标点，就是没有句读证据，
       此时"整段归给说话时间更长的人"比"切在错误位置"错得少。
    返回切点下标（该下标之前的字符归上一段）。
    """
    lo = max(1, target - window)
    hi = min(len(text) - 1, target + window)
    if lo > hi:
        return None
    for charset in (STRONG_PUNCT, WEAK_PUNCT):
        best = None
        for i in range(lo, hi + 1):
            # text[i-1] 是标点 → 在它后面切，标点跟着上一段走
            if text[i - 1] in charset:
                dist = abs(i - target)
                if best is None or dist < best[0]:
                    best = (dist, i)
        if best:
            return best[1]
    return None


def _time_to_index(char_times: Sequence[float], moment: float) -> int:
    """词级时间戳可用时，把时刻换算成字符下标。"""
    return bisect_left(list(char_times), moment)


def _split_long_chunk(
    chunk: Chunk,
    *,
    max_chars: int,
    max_seconds: float,
) -> List[Chunk]:
    """单个说话人说太久时，按标点再分段（只为可读，不改说话人）。"""
    text = chunk.text
    duration = (
        (chunk.end - chunk.start)
        if chunk.start is not None and chunk.end is not None
        else 0.0
    )
    pieces = max(
        1,
        math.ceil(len(text) / max_chars) if max_chars > 0 else 1,
        math.ceil(duration / max_seconds) if max_seconds > 0 and duration > 0 else 1,
    )
    if pieces <= 1 or len(text) < 2:
        return [chunk]

    cuts: List[int] = []
    window = max(6, len(text) // (pieces * 3) or 6)
    for i in range(1, pieces):
        target = round(len(text) * i / pieces)
        idx = _snap_index(text, target, window)
        if idx is None or (cuts and idx <= cuts[-1]):
            continue
        cuts.append(idx)
    if not cuts:
        return [chunk]

    out: List[Chunk] = []
    bounds = [0, *cuts, len(text)]
    for i in range(len(bounds) - 1):
        piece = text[bounds[i] : bounds[i + 1]]
        if not piece:
            continue
        start = end = None
        if chunk.start is not None and chunk.end is not None and len(text):
            start = chunk.start + duration * bounds[i] / len(text)
            end = chunk.start + duration * bounds[i + 1] / len(text)
        out.append(Chunk(piece, start, end, chunk.label))
    return out or [chunk]


def _merge_tiny(chunks: List[Chunk], min_chars: int) -> List[Chunk]:
    """把过短的碎片并回**同说话人**的邻段。

    ⚠️ 不同说话人的短段不并 —— 「嗯」「对」这种插话本来就是独立一轮，
       并进去等于又把两个人揉回一段，正是本模块要消除的错误。
    """
    if min_chars <= 0:
        return chunks
    out: List[Chunk] = []
    for chunk in chunks:
        if (
            out
            and len(chunk.text.strip()) < min_chars
            and out[-1].label == chunk.label
        ):
            out[-1].text += chunk.text
            out[-1].end = chunk.end
            continue
        out.append(chunk)
    # 首段过短时并入后一段（同说话人才并）
    if len(out) > 1 and len(out[0].text.strip()) < min_chars and out[0].label == out[1].label:
        out[1].text = out[0].text + out[1].text
        out[1].start = out[0].start
        out.pop(0)
    return out


def split_text_by_spans(
    text: str,
    spans: Sequence[Span],
    *,
    char_times: Optional[Sequence[float]] = None,
    pause_sec: float = DEFAULT_PAUSE_SEC,
    max_chars: int = DEFAULT_MAX_CHARS,
    max_seconds: float = DEFAULT_MAX_SECONDS,
    min_chars: int = DEFAULT_MIN_CHARS,
) -> List[Chunk]:
    """把一条 final 切成若干 Chunk。

    text       : 该条 final 的完整文本
    spans      : 覆盖这条 final 的语音段（含说话人标签），同一时间轴，按时间排序
    char_times : 可选的每个字符的时刻（秒）。有词级时间戳时给它，切点更准；
                 没有就按语音时长比例分配。

    没有 spans 时原样返回一段（label=None）——**宁可不切，也不瞎切**。
    """
    text = (text or "").strip()
    if not text:
        return []
    groups = group_spans([s for s in spans if s.duration > 0], pause_sec)
    if not groups:
        return [Chunk(text, None, None, None)]

    total = sum(g.duration for g in groups)
    if total <= 0:
        return [Chunk(text, groups[0].start, groups[-1].end, groups[0].label)]

    # 组边界 → 字符下标。切不动的边界（附近没有标点）直接放弃，
    # 相邻两组合并成一段，说话人取其中说得更久的那个。
    cuts: List[Tuple[int, int]] = []   # (字符下标, 该切点右边第一个组的序号)
    cum = 0.0
    window = max(6, min(16, len(text) // 6))
    for i, group in enumerate(groups[:-1]):
        cum += group.duration
        if char_times and len(char_times) >= len(text):
            target = _time_to_index(char_times, group.end)
        else:
            target = round(len(text) * cum / total)
        idx = _snap_index(text, target, window)
        if idx is None or idx <= 0 or idx >= len(text):
            continue
        if cuts and idx <= cuts[-1][0]:
            continue
        cuts.append((idx, i + 1))

    chunks: List[Chunk] = []
    bounds = [0, *[c[0] for c in cuts], len(text)]
    group_bounds = [0, *[c[1] for c in cuts], len(groups)]
    for i in range(len(bounds) - 1):
        piece = text[bounds[i] : bounds[i + 1]]
        if not piece:
            continue
        members = groups[group_bounds[i] : group_bounds[i + 1]] or [groups[-1]]
        dominant = max(members, key=lambda g: g.duration)
        chunks.append(Chunk(piece, members[0].start, members[-1].end, dominant.label))

    expanded: List[Chunk] = []
    for chunk in chunks:
        expanded.extend(
            _split_long_chunk(chunk, max_chars=max_chars, max_seconds=max_seconds)
        )
    return _merge_tiny(expanded, min_chars)


def clip_spans(
    spans: Sequence[Span],
    window_start: float,
    window_end: float,
    *,
    edge_min_sec: float = 0.8,
    edge_min_ratio: float = 0.4,
) -> List[Span]:
    """把语音段裁到时间窗内，并剔掉首尾的"渗漏碎片"。

    ⚠️ 窗口边界不会正好落在语音间隙上。首段常常只被切进来零点几秒——
       那是相邻那条 final 的尾音，让它参与分字就会在开头挤出
       「那」「做实」这种孤儿碎片（实测出现过）。
       判据是"被切掉大半且剩得很短"，所以完整落在窗口内的短插话不受影响。
    """
    picked: List[Tuple[Span, float]] = []
    for span in spans:
        start = max(span.start, window_start)
        end = min(span.end, window_end)
        if end - start <= 0:
            continue
        picked.append((Span(start, end, span.label), span.duration))

    def bleeding(item: Tuple[Span, float]) -> bool:
        clipped, original = item
        if original <= 0:
            return False
        return (
            clipped.duration < edge_min_sec
            and clipped.duration < original * edge_min_ratio
        )

    while len(picked) > 1 and bleeding(picked[0]):
        picked.pop(0)
    while len(picked) > 1 and bleeding(picked[-1]):
        picked.pop()
    return [span for span, _ in picked]


def spans_from_segments(
    segments: Sequence[dict],
    *,
    start_key: str = "start",
    end_key: str = "end",
    label_key: str = "speakerId",
    window: Optional[Tuple[float, float]] = None,
) -> List[Span]:
    """diarize_offline 的 segments（dict 列表）→ Span 列表，可按时间窗裁剪。"""
    out: List[Span] = []
    for seg in segments:
        start = float(seg.get(start_key) or 0.0)
        end = float(seg.get(end_key) or 0.0)
        if window:
            start = max(start, window[0])
            end = min(end, window[1])
        if end - start <= 0:
            continue
        out.append(Span(start, end, seg.get(label_key)))
    return out
