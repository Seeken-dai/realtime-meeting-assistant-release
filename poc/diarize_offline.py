"""
会后本地说话人分离（方案 B 本地版）。

流程：
  1) VAD 切段 + CAM++ 嵌入（与 verify_speaker / speaker_me 同模型）
  2) 若提供 --enroll：先 1:1 认出「我」，剩余段再聚类
  3) 若提供转写 JSON：按相对会议开始时间对齐，回写 speakerId/speaker
  4) stdout 只打一行结果 JSON（桌面端解析）

用法：
  python diarize_offline.py --wav meeting.wav
  python diarize_offline.py --wav meeting.wav --enroll enroll_me.wav --me-threshold 0.65
  python diarize_offline.py --wav meeting.wav --enroll enroll_me.wav \\
      --transcript-json t.json --started-at 1784880000000 --out-json result.json

转写 JSON 格式：[{id, speaker, speakerId, text, isFinal, at}, ...]
at 为墙上毫秒时间戳；相对秒 = (at - started_at) / 1000。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

try:
    import sherpa_onnx as so
except ImportError:
    sys.exit("请先安装：pip install sherpa-onnx")

from speaker_me import (
    DEFAULT_SPK_MODEL,
    DEFAULT_VAD_MODEL,
    MEETING_VAD_MAX_SPEECH,
    MEETING_VAD_MIN_SILENCE,
    MIN_SEG_SEC,
    SAMPLE_RATE,
    SPEAKER_ID_ME,
    enroll_from_wav,
    read_wav_mono16k,
    _embed_waveform,
    _make_vad,
)
from turn_split import Span, clip_spans, split_text_by_spans

# 聚类默认阈值：与 verify_speaker 实测可分区间一致（0.55–0.65）
DEFAULT_CLUSTER_TH = 0.60

# 首条 final 没有"上一条"可参照时，最多回看多久（秒）
MAX_ITEM_WINDOW_SEC = 60.0

# 认「我」的簇级判据：均分下限 + 与第二名的差距。
# 第二场真实会议（2026-07-29 15:03）最高两簇为 0.551 / 0.503：
# 人工核对确认最高簇大部分就是用户本人。原 0.05 门槛仅差 0.002 未过，
# 随即退回逐段 0.65，把同一人拆成「我」+「说话人1」。因此门槛收至 0.04；
# 仍保留 floor，且差距 <0.04 时继续拒绝强认，避免相近声纹误标整簇。
ME_CLUSTER_FLOOR = 0.45
ME_CLUSTER_MARGIN = 0.04


def choose_me_cluster(
    cluster_ids: List[int],
    cluster_scores: Dict[int, Optional[float]],
) -> Tuple[Optional[int], str]:
    """按簇均分选择「我」，判据不足时明确退回逐段阈值。"""
    if not cluster_ids:
        return None, "threshold"
    ranked = sorted(
        cluster_ids, key=lambda c: (cluster_scores[c] or 0.0), reverse=True
    )
    top = ranked[0]
    top_score = cluster_scores[top] or 0.0
    runner = (cluster_scores[ranked[1]] or 0.0) if len(ranked) > 1 else -1.0
    if top_score >= ME_CLUSTER_FLOOR and (
        len(ranked) == 1 or top_score - runner >= ME_CLUSTER_MARGIN
    ):
        return top, "cluster"
    return None, "threshold"


def assess_diarization_confidence(
    result: Dict[str, Any], expected_speaker_count: Optional[int] = None
) -> Dict[str, Any]:
    """Classify an offline result without forcing it to a requested count."""

    segments = result.get("segments") or []
    speakers = result.get("speakers") or []
    reasons: List[str] = []
    if not segments:
        return {
            "status": "not_recommended",
            "score": 0.0,
            "reasons": ["没有足够有效语音段"],
        }
    if result.get("enrollUsed") and result.get("meDecision") == "threshold":
        reasons.append("簇级认我判据不足，退回逐段阈值")
    if expected_speaker_count:
        actual = len(speakers)
        if actual > int(expected_speaker_count) + 1:
            reasons.append("人数明显超出提示，可能存在声纹漂移或碎簇")
        elif actual < max(1, int(expected_speaker_count) - 1):
            reasons.append("人数少于提示，部分远端可能被合并")
    short_clusters = [
        speaker
        for speaker in speakers
        if float(speaker.get("seconds") or 0) < 8.0
        or int(speaker.get("segments") or 0) < 3
    ]
    if short_clusters:
        reasons.append("存在过短簇，已按粗分处理")
    if result.get("meDecision") == "none":
        reasons.append("没有注册声纹，只能区分聚类编号")
    if not reasons:
        return {"status": "high", "score": 0.9, "reasons": []}
    return {
        "status": "coarse" if len(segments) >= 2 else "not_recommended",
        "score": 0.65 if len(segments) >= 2 else 0.2,
        "reasons": reasons,
    }


def segment_speech(
    samples: np.ndarray, vad_model: str
) -> List[Tuple[float, float, np.ndarray]]:
    """切段粒度与会中统一（MEETING_VAD_*，取值理由与实测对比见 speaker_me）。

    ⚠️ 会中会后必须用同一个粒度：同一场会在两个页面上把话切在不同地方、
       归属还不一样，用户没法理解系统在干什么。
    """
    vad, window = _make_vad(
        vad_model,
        min_silence_duration=MEETING_VAD_MIN_SILENCE,
        max_speech_duration=MEETING_VAD_MAX_SPEECH,
    )
    segments: List[Tuple[float, float, np.ndarray]] = []
    # sherpa VAD front.start 是内部偏移；用累计采样对齐全局时间
    stream_samples = 0
    buf_start = 0
    for i in range(0, len(samples), window):
        chunk = samples[i : i + window]
        if len(chunk) < window:
            break
        vad.accept_waveform(chunk)
        stream_samples += window
        while not vad.empty():
            front = vad.front
            seg = np.array(front.samples, dtype=np.float32)
            end = stream_samples / SAMPLE_RATE
            start = max(0.0, end - len(seg) / SAMPLE_RATE)
            # 若 front.start 可用且合理则优先
            try:
                s0 = float(front.start) / SAMPLE_RATE
                if 0 <= s0 <= end:
                    start = s0
                    end = start + len(seg) / SAMPLE_RATE
            except Exception:
                pass
            segments.append((start, end, seg))
            vad.pop()
    vad.flush()
    while not vad.empty():
        front = vad.front
        seg = np.array(front.samples, dtype=np.float32)
        end = stream_samples / SAMPLE_RATE
        start = max(0.0, end - len(seg) / SAMPLE_RATE)
        try:
            s0 = float(front.start) / SAMPLE_RATE
            if 0 <= s0 <= end + 1:
                start = s0
                end = start + len(seg) / SAMPLE_RATE
        except Exception:
            pass
        segments.append((start, end, seg))
        vad.pop()
    return segments


def embed_segments(extractor, segments):
    vectors = []
    kept = []
    for start, end, samples in segments:
        if end - start < MIN_SEG_SEC:
            continue
        vec = _embed_waveform(extractor, samples)
        if vec is None:
            continue
        vectors.append(vec)
        kept.append((start, end))
    if not vectors:
        return np.zeros((0, 1), dtype=np.float32), []
    return np.array(vectors, dtype=np.float32), kept


def cluster(vectors: np.ndarray, threshold: float) -> np.ndarray:
    """average-linkage 余弦聚类，返回每段簇 id。"""
    n = len(vectors)
    if n == 0:
        return np.empty(0, dtype=int)
    if n == 1:
        return np.array([0], dtype=int)
    sim = (vectors @ vectors.T).astype(np.float64)
    np.fill_diagonal(sim, -np.inf)
    size = np.ones(n)
    alive = np.ones(n, dtype=bool)
    members = {i: [i] for i in range(n)}
    while alive.sum() > 1:
        masked = np.where(alive[:, None] & alive[None, :], sim, -np.inf)
        a, b = np.unravel_index(np.argmax(masked), masked.shape)
        if masked[a, b] < threshold:
            break
        merged = (sim[a] * size[a] + sim[b] * size[b]) / (size[a] + size[b])
        sim[a, :] = merged
        sim[:, a] = merged
        sim[a, a] = -np.inf
        size[a] += size[b]
        alive[b] = False
        members[a].extend(members.pop(b))
    labels = np.empty(n, dtype=int)
    for new_id, group in enumerate(members.values()):
        for i in group:
            labels[i] = new_id
    return labels



def merge_small_clusters(
    vectors: np.ndarray,
    labels: np.ndarray,
    *,
    min_segments: int = 3,
    min_seconds: float = 8.0,
    kept_times: Optional[List[Tuple[float, float]]] = None,
) -> np.ndarray:
    """把过小的簇并入余弦中心最接近的大簇。"""
    labels = np.array(labels, dtype=int).copy()
    if len(labels) == 0:
        return labels

    def cluster_dur(cid: int) -> float:
        if not kept_times:
            return float((labels == cid).sum())
        return sum(
            kept_times[i][1] - kept_times[i][0]
            for i in range(len(labels))
            if labels[i] == cid
        )

    def is_small(cid: int) -> bool:
        n = int((labels == cid).sum())
        return n < min_segments or cluster_dur(cid) < min_seconds

    # 质心
    def centroid(cid: int) -> np.ndarray:
        idx = np.where(labels == cid)[0]
        v = vectors[idx].mean(axis=0)
        return v / (np.linalg.norm(v) + 1e-9)

    changed = True
    guard = 0
    while changed and guard < 100:
        guard += 1
        changed = False
        ids = sorted(set(int(x) for x in labels))
        big = [c for c in ids if not is_small(c)]
        small = [c for c in ids if is_small(c)]
        if not small:
            break
        if not big:
            # 全是小簇：只保留最大的几个（按段数）
            sizes = Counter(int(x) for x in labels)
            keep = [c for c, _ in sizes.most_common(max(1, min(6, len(sizes))))]
            big = keep
            small = [c for c in ids if c not in big]
        cents = {c: centroid(c) for c in ids}
        for s in small:
            if s not in cents:
                continue
            target = max(big, key=lambda b: float(cents[s] @ cents[b]))
            labels[labels == s] = target
            changed = True
    # 重新压缩 id
    uniq = sorted(set(int(x) for x in labels))
    remap = {old: i for i, old in enumerate(uniq)}
    return np.array([remap[int(x)] for x in labels], dtype=int)


def diarize(
    wav_path: str,
    *,
    enroll_wav: Optional[str] = None,
    me_threshold: float = 0.65,
    cluster_th: float = DEFAULT_CLUSTER_TH,
    min_cluster_segments: int = 3,
    min_cluster_seconds: float = 8.0,
    spk_model: str = DEFAULT_SPK_MODEL,
    vad_model: str = DEFAULT_VAD_MODEL,
    num_threads: int = 2,
    expected_speaker_count: Optional[int] = None,
) -> Dict[str, Any]:
    t0 = time.time()
    samples = read_wav_mono16k(wav_path)
    duration = len(samples) / SAMPLE_RATE

    extractor = so.SpeakerEmbeddingExtractor(
        so.SpeakerEmbeddingExtractorConfig(model=spk_model, num_threads=num_threads)
    )
    me_manager = None
    enroll_segs = 0
    if enroll_wav:
        extractor, me_manager, enroll_segs = enroll_from_wav(
            enroll_wav,
            spk_model=spk_model,
            vad_model=vad_model,
            num_threads=num_threads,
        )

    raw_segs = segment_speech(samples, vad_model)
    vectors, kept = embed_segments(extractor, raw_segs)
    if len(kept) == 0:
        return {
            "ok": False,
            "error": "未检出有效语音段（≥0.7s）",
            "durationSec": round(duration, 1),
            "segments": [],
            "speakers": [],
        }

    me_scores: List[Optional[float]] = (
        [float(me_manager.score("我", vec.tolist())) for vec in vectors]
        if me_manager is not None
        else [None] * len(vectors)
    )

    # ⭐ 先把【全部】段聚类，再决定哪个簇是「我」。
    #
    # ⚠️ 别改回「先按阈值挑出我、剩下的再聚类」。那样等于用一个绝对分数去切
    #    连续分布，同一个人跨在阈值两边就会被拆成两个说话人 ——
    #    实测（meeting-1785145910139）固定 0.65 把用户自己拆成「我」+「说话人2」，
    #    而被拆开的相邻分数只差 0.003（0.654 / 0.648）。
    #    聚类用的是段与段之间的相互相似度，不依赖注册信道，实测在
    #    cluster_th 0.50–0.70 全区间都稳定给出同样的 2 簇，
    #    两簇的 me 分完全不重叠（0.540~0.755 vs 0.231~0.446）。
    clabels = cluster(vectors, cluster_th)
    clabels = merge_small_clusters(
        vectors,
        clabels,
        min_segments=min_cluster_segments,
        min_seconds=min_cluster_seconds,
        kept_times=kept,
    )
    cluster_ids = sorted(set(int(c) for c in clabels))

    def cluster_seconds(cid: int) -> float:
        return sum(
            kept[i][1] - kept[i][0]
            for i in range(len(clabels))
            if int(clabels[i]) == cid
        )

    def cluster_me_score(cid: int) -> Optional[float]:
        """簇的 me 分：按时长加权平均——短段的分数噪声大，不该和长段同权。"""
        if me_manager is None:
            return None
        total = weighted = 0.0
        for i in range(len(clabels)):
            if int(clabels[i]) != cid:
                continue
            dur = kept[i][1] - kept[i][0]
            total += dur
            weighted += dur * float(me_scores[i] or 0.0)
        return (weighted / total) if total > 0 else None

    # 哪个簇是「我」：分最高的那个，且要过下限、且与第二名拉开差距。
    # 判据放在【簇】上而不是【段】上：单段分数会飘，整簇的均值稳得多。
    me_cluster: Optional[int] = None
    me_decision = "none"
    cluster_scores = {cid: cluster_me_score(cid) for cid in cluster_ids}
    if me_manager is not None and cluster_ids:
        me_cluster, me_decision = choose_me_cluster(cluster_ids, cluster_scores)
        # 兜底：簇判据不成立（分数整体太低 / 前两簇过近）时退回逐段阈值。
        # 宁可漏认自己，也不要把对方整簇标成我。

    labels: List[str] = [""] * len(kept)
    other_clusters: List[int] = []
    for cid in cluster_ids:
        if cid != me_cluster:
            other_clusters.append(cid)
    # 对方簇按时长排序，大的叫 spk1
    other_clusters.sort(key=lambda c: -cluster_seconds(c))
    remap = {cid: i for i, cid in enumerate(other_clusters)}
    for i, c in enumerate(clabels):
        cid = int(c)
        if cid == me_cluster:
            labels[i] = SPEAKER_ID_ME
        elif me_decision == "threshold" and (me_scores[i] or 0.0) >= me_threshold:
            labels[i] = SPEAKER_ID_ME
        else:
            labels[i] = f"spk{remap[cid] + 1}"
    n_clusters = len(other_clusters)

    segments_out = []
    for i, ((start, end), lab, sc) in enumerate(zip(kept, labels, me_scores)):
        if not lab:
            lab = "spk1"
            labels[i] = lab
        segments_out.append(
            {
                "start": round(float(start), 3),
                "end": round(float(end), 3),
                "speakerId": lab,
                "meScore": None if sc is None else round(sc, 3),
            }
        )

    # speakers 汇总
    by_id: Dict[str, List[int]] = defaultdict(list)
    for i, lab in enumerate(labels):
        by_id[lab].append(i)

    speakers = []
    for sid, idxs in sorted(
        by_id.items(),
        key=lambda kv: (0 if kv[0] == SPEAKER_ID_ME else 1, kv[0]),
    ):
        sec = sum(kept[i][1] - kept[i][0] for i in idxs)
        name = "我" if sid == SPEAKER_ID_ME else f"说话人{sid.replace('spk', '')}"
        cluster_score = None
        if me_manager is not None and idxs:
            total = sum(kept[i][1] - kept[i][0] for i in idxs)
            if total > 0:
                cluster_score = sum(
                    (kept[i][1] - kept[i][0]) * float(me_scores[i] or 0.0)
                    for i in idxs
                ) / total
        speaker_quality = (
            "high"
            if len(idxs) >= 3 and sec >= 8.0 and (sid != SPEAKER_ID_ME or me_decision == "cluster")
            else "coarse"
        )
        speakers.append(
            {
                "id": sid,
                "name": name,
                "isMe": sid == SPEAKER_ID_ME,
                "segments": len(idxs),
                "seconds": round(sec, 1),
                "meScore": None if cluster_score is None else round(cluster_score, 3),
                "confidence": speaker_quality,
            }
        )

    quality = assess_diarization_confidence(
        {
            "segments": segments_out,
            "speakers": speakers,
            "enrollUsed": bool(enroll_wav),
            "meDecision": me_decision,
        },
        expected_speaker_count,
    )
    return {
        "ok": True,
        "durationSec": round(duration, 1),
        "enrollUsed": bool(enroll_wav),
        "enrollSegments": enroll_segs,
        "meThreshold": me_threshold,
        # cluster = 按簇均分认出「我」（正常路径）；threshold = 簇判据不成立，
        # 退回逐段固定阈值；none = 没提供注册声纹
        "meDecision": me_decision,
        "clusterScores": {
            f"c{cid}": None if cluster_scores[cid] is None else round(cluster_scores[cid], 3)
            for cid in cluster_ids
        },
        "clusterThreshold": cluster_th,
        "expectedSpeakerCount": expected_speaker_count,
        "quality": quality,
        "segmentCount": len(segments_out),
        "speakerCount": len(speakers),
        "otherClusters": n_clusters,
        "elapsedSec": round(time.time() - t0, 2),
        "segments": segments_out,
        "speakers": speakers,
        "note": (
            "离线聚类结果供人工确认命名；时间对齐优先继承实时 PCM 录音轴；"
            "旧记录才使用 ASR 送达时刻兼容估算。"
        ),
    }


def coerce_online_remote_clusters(
    result: Dict[str, Any], max_remote_clusters: int = 2
) -> Dict[str, Any]:
    """Make a system-only diarization safe for an online meeting.

    The microphone never enters this clustering result, so it cannot compete
    with the user's identity.  When the model emits more remote clusters than
    the optional participant hint permits, keep the longest clusters and merge
    the rest into the longest one.  This is deliberately a coarse fallback,
    not a fabricated precise identity.
    """

    max_remote_clusters = max(1, int(max_remote_clusters or 1))
    segments = list(result.get("segments") or [])
    durations: Dict[str, float] = defaultdict(float)
    for segment in segments:
        sid = str(segment.get("speakerId") or "remote-1")
        durations[sid] += max(
            0.0, float(segment.get("end", 0.0)) - float(segment.get("start", 0.0))
        )
    ordered = sorted(durations, key=lambda sid: (-durations[sid], sid))
    if not ordered:
        return {
            **result,
            "ok": True,
            "systemAudioOnly": True,
            "microphoneFixed": "me",
            "remoteClusters": 0,
            "speakerCount": 1,
            "confidence": "not_recommended",
            "quality": {
                "status": "not_recommended",
                "score": 0.0,
                "reasons": ["系统音轨没有检出足够语音"],
            },
            "segments": [],
            "speakers": [],
            "note": "系统音轨没有检出足够语音，线上会后版只保留麦克风=我的身份约束。",
        }

    keep = ordered[:max_remote_clusters]
    primary = keep[0]
    remap = {sid: f"remote-{index + 1}" for index, sid in enumerate(keep)}
    # Any overflow is intentionally merged into the longest remote cluster.
    for sid in ordered[max_remote_clusters:]:
        remap[sid] = remap[primary]
    out_segments = []
    for segment in segments:
        old = str(segment.get("speakerId") or primary)
        out = dict(segment)
        out["speakerId"] = remap.get(old, "remote-1")
        out_segments.append(out)
    actual_clusters = len(set(item["speakerId"] for item in out_segments))
    coarse = len(ordered) > max_remote_clusters
    speakers = [
        {
            "id": f"remote-{index + 1}",
            "name": "对方" if actual_clusters == 1 else f"远端{index + 1}",
            "isMe": False,
            "segments": sum(1 for item in out_segments if item["speakerId"] == f"remote-{index + 1}"),
            "seconds": round(
                sum(
                    max(0.0, float(item.get("end", 0.0)) - float(item.get("start", 0.0)))
                    for item in out_segments
                    if item["speakerId"] == f"remote-{index + 1}"
                ),
                1,
            ),
        }
        for index in range(actual_clusters)
    ]
    return {
        **result,
        "ok": True,
        "systemAudioOnly": True,
        "microphoneFixed": "me",
        "remoteClusters": actual_clusters,
        "speakerCount": actual_clusters + 1,
        "confidence": "coarse" if coarse else "high",
        "segments": out_segments,
        "speakers": speakers,
        "note": (
            "线上会后版固定麦克风=我，只对系统回环聚类；"
            + ("远端簇数超出提示，已合并小簇为粗分。" if coarse else "")
        ),
    }


def diarize_online(
    system_wav: str,
    *,
    speaker_count: Optional[int] = None,
    cluster_th: float = DEFAULT_CLUSTER_TH,
    min_cluster_segments: int = 3,
    min_cluster_seconds: float = 8.0,
    spk_model: str = DEFAULT_SPK_MODEL,
    vad_model: str = DEFAULT_VAD_MODEL,
) -> Dict[str, Any]:
    """Separate only the system/remote track for an online meeting."""

    remote_limit = max(1, int(speaker_count) - 1) if speaker_count else 2
    result = diarize(
        system_wav,
        enroll_wav=None,
        cluster_th=cluster_th,
        min_cluster_segments=min_cluster_segments,
        min_cluster_seconds=min_cluster_seconds,
        spk_model=spk_model,
        vad_model=vad_model,
    )
    if not result.get("ok"):
        # No system speech is not a reason to discard the microphone channel.
        return {
            **result,
            "ok": True,
            "systemAudioOnly": True,
            "microphoneFixed": "me",
            "remoteClusters": 0,
            "speakerCount": 1,
            "confidence": "not_recommended",
            "quality": {
                "status": "not_recommended",
                "score": 0.0,
                "reasons": ["系统音轨没有可分离语音"],
            },
            "segments": [],
            "speakers": [],
            "note": "系统音轨没有可分离语音；麦克风通道仍固定为我。",
        }
    coerced = coerce_online_remote_clusters(result, remote_limit)
    # 线上没有注册声纹是预期设计，不把“没有 enroll”误报为失败；
    # 但系统音轨的短簇/超额簇仍然要通过粗分状态显式暴露。
    if coerced.get("confidence") == "coarse":
        coerced["quality"] = {
            "status": "coarse",
            "score": 0.65,
            "reasons": ["系统音轨远端簇数超出提示，已合并为粗分"],
        }
    else:
        coerced["quality"] = {
            "status": "high",
            "score": 0.8,
            "reasons": ["线上身份约束来自独立麦克风音轨；远端仅按系统音轨聚类"],
        }
    return coerced


def _estimate_offset(
    segments: List[dict], transcript: List[dict], started_at: float, audio_dur: float
) -> float:
    """估 ASR 墙上时间相对录音轴的偏移（秒）。

    转写 at 是识别结果送达时刻；录音 t=0 是开录时刻。二者相差一个"识别延迟"：
    ASR 要等静音才判句尾，再加网络往返。

    ⚠️ 别只用最后一条 final 当锚点（本文件此前就是这么做的）。停止录音时会把
       正在说的那句 flush 出来，它的送达时刻远晚于任何语音段的结束
       —— 实测这一条把偏移估成 4.1s，比真实延迟大一倍多，
       于是每条 final 的时间窗都往前挪，把上一个人的话尾巴卷进来，
       切分结果里出现「那」「做实」「亲子」这种孤儿碎片。

    改用所有 final 各自到最近一个语音段结束点的距离取中位数：
    单条异常（flush、长静音、漏检）动摇不了中位数。
    """
    finals = [
        t
        for t in transcript
        if t.get("isFinal", True) and t.get("at") is not None and (t.get("text") or "").strip()
    ]
    if not finals or not segments:
        return 0.0
    ends = [float(s["end"]) for s in segments]
    lags: List[float] = []
    for item in finals:
        rel_raw = (float(item["at"]) - started_at) / 1000.0
        # 该条 final 的音频必然结束于某个语音段的末尾之后不久
        behind = [rel_raw - e for e in ends if 0.0 <= rel_raw - e <= 8.0]
        if behind:
            lags.append(min(behind))
    if not lags:
        return 0.0
    offset = float(np.median(lags))
    if offset < 0 or offset > 30:
        return 0.0
    return offset


def _window_spans(
    segments: List[dict], window_start: float, window_end: float
) -> List[Span]:
    """取覆盖 [window_start, window_end] 的语音段（含首尾渗漏碎片的剔除）。

    窗口边界来自 ASR 送达时刻（带延迟），不会正好落在语音间隙上，
    具体处理见 turn_split.clip_spans。
    """
    lo = window_start - 0.3
    hi = window_end + 0.4
    spans = [
        Span(float(seg["start"]), float(seg["end"]), seg["speakerId"])
        for seg in segments
        if float(seg["end"]) > lo and float(seg["start"]) < hi
    ]
    return clip_spans(spans, lo, hi)


def _valid_audio_range(start_ms: Any, end_ms: Any) -> Optional[Tuple[float, float]]:
    try:
        start = float(start_ms) / 1000.0
        end = float(end_ms) / 1000.0
    except (TypeError, ValueError):
        return None
    if not np.isfinite(start) or not np.isfinite(end) or end <= start:
        return None
    return max(0.0, start), max(0.0, end)


def align_transcript(
    result: Dict[str, Any],
    transcript: List[dict],
    started_at: float,
    *,
    split: bool = True,
) -> List[dict]:
    """把分离结果回写到转写，并按说话人边界把过长的 final **切开**。

    ⚠️ 只给整条 final 打一个标签是不够的：阿里实时转写只在静音处断句，
       两个人来回接话时一条能覆盖 40 秒、跨 4 次换人（实测 47s/262字）。
       这种条目无论标给谁都有一半是错的，用户也没法只改其中一小段。
       所以这里按 VAD 段把它切成若干条，每条各自归属、各自可改派。
    """
    segments = result.get("segments") or []
    if not segments:
        return transcript

    offset = _estimate_offset(
        segments, transcript, started_at, float(result.get("durationSec") or 0)
    )
    speakers_by_id = {s["id"]: s for s in result.get("speakers") or []}

    def name_of(sid: Optional[str]) -> str:
        if not sid:
            return "对方"
        sp = speakers_by_id.get(sid) or {}
        return sp.get("name") or ("我" if sid == SPEAKER_ID_ME else str(sid))

    def label_at(rel_sec: float) -> Tuple[str, str]:
        # 覆盖优先，否则退到最近的语音段。
        # ⚠️ 不要在"离得远"时返回 "other"：本场的说话人只有 me / spk1 / spk2…，
        #    凭空造一个 other 会让这条发言在界面上既改不了名也归不了类
        #    （speakers 表里根本没有它）。宁可给最近那段的归属，用户能一眼看出来并改。
        covering = [
            s
            for s in segments
            if s["start"] - 0.2 <= rel_sec <= s["end"] + 0.4
        ]
        if covering:
            # 多段覆盖时取时长最长的
            best = max(covering, key=lambda s: s["end"] - s["start"])
        else:
            best = min(segments, key=lambda s: abs(s["end"] - rel_sec))
        sid = best["speakerId"]
        return sid, name_of(sid)

    used_ids = {str(t.get("id")) for t in transcript if t.get("id") is not None}

    def unique_id(base: str, index: int) -> str:
        """第一片沿用原 id，其余追加 #pN；重跑时靠 used_ids 保证不撞号。"""
        candidate = base if index == 0 else f"{base}#p{index + 1}"
        n = index + 1
        while candidate in used_ids:
            n += 1
            candidate = f"{base}#p{n}"
        used_ids.add(candidate)
        return candidate

    out: List[dict] = []
    split_count = 0
    prev_rel: Optional[float] = None
    prev_at: Optional[float] = None
    audio_duration = float(result.get("durationSec") or 0)
    for item in transcript:
        row = dict(item)
        at = row.get("at")
        if not row.get("isFinal", True) or at is None:
            out.append(row)
            continue
        precise_range = (
            _valid_audio_range(row.get("audioStartMs"), row.get("audioEndMs"))
        )
        if precise_range:
            # 新会议的实时段已经由 PCM 采样钟映射过；会后只允许在父段
            # 区间内重新分配说话人，不能再用 ASR 送达时刻估整场偏移。
            rel_start, rel = precise_range
            if audio_duration > 0:
                rel_start = min(max(rel_start, 0.0), audio_duration)
                rel = min(max(rel, rel_start), audio_duration)
            window_start = rel_start
        else:
            rel = (float(at) - started_at) / 1000.0 - offset
            # 这条 final 覆盖的音频窗口：上一条结束 → 本条结束。
            # 连续对话里两条 final 之间的语音必然属于后一条。
            window_start = (
                prev_rel if prev_rel is not None else rel - MAX_ITEM_WINDOW_SEC
            )
            window_start = max(0.0, min(window_start, rel))
        prev_rel = rel

        text = (row.get("text") or "").strip()
        spans = _window_spans(segments, window_start, rel)
        if precise_range and spans:
            spans = clip_spans(spans, window_start, rel)
        chunks = split_text_by_spans(text, spans) if (split and text) else []

        if len(chunks) <= 1:
            label = chunks[0].label if chunks else None
            if label:
                sid, name = label, name_of(label)
            else:
                sid, name = label_at(rel)
            row["speakerId"] = sid
            row["speaker"] = name
            if precise_range:
                row["audioStartMs"] = int(round(window_start * 1000))
                row["audioEndMs"] = int(round(rel * 1000))
            elif spans:
                row["audioStartMs"] = int(round(min(span.start for span in spans) * 1000))
                row["audioEndMs"] = int(round(max(span.end for span in spans) * 1000))
            else:
                # 极少数尾条可能落在 VAD 最后一段之外。仍给播放器一个保守区间，
                # 否则整场播放到这里会突然失去高亮；长度只用于回放兜底，不参与归属。
                audio_end = max(
                    0.0,
                    min(float(result.get("durationSec") or rel), rel),
                )
                estimated = min(22.0, max(0.9, len(text) * 0.18))
                row["audioStartMs"] = int(round(max(0.0, audio_end - estimated) * 1000))
                row["audioEndMs"] = int(round(audio_end * 1000))
            prev_at = float(row["at"])
            out.append(row)
            continue

        split_count += 1
        base_id = str(row.get("id") or f"seg-{len(out)}")
        # 这条自己的 id 让给第一片用，否则第一片会被顶成 #p2
        used_ids.discard(base_id)
        for index, chunk in enumerate(chunks):
            sid = chunk.label or label_at(rel)[0]
            piece = dict(row)
            piece["id"] = unique_id(base_id, index)
            piece["text"] = chunk.text.strip()
            piece["speakerId"] = sid
            piece["speaker"] = name_of(sid)
            if chunk.start is not None:
                piece["audioStartMs"] = int(round(chunk.start * 1000))
            if chunk.end is not None:
                piece["audioEndMs"] = int(round(chunk.end * 1000))
            if precise_range:
                # 音频区间是唯一精确轴；at 仅保留原始送达时间用于历史排序。
                piece_at = float(row.get("at") or started_at)
                if index and prev_at is not None:
                    piece_at = prev_at + 1
            else:
                # at 仍是「ASR 送达时刻」口径（+offset 换算回去），保证
                # 列表按 at 排列时顺序不变，也能被再次对齐。
                chunk_end = chunk.end if chunk.end is not None else rel
                piece_at = started_at + (min(chunk_end, rel) + offset) * 1000.0
            if prev_at is not None and piece_at <= prev_at:
                piece_at = prev_at + 1
            piece["at"] = int(round(piece_at))
            prev_at = float(piece["at"])
            out.append(piece)

    result["splitItems"] = split_count
    result["transcriptItems"] = len([r for r in out if r.get("isFinal", True)])
    return out


def align_online_transcript(
    result: Dict[str, Any],
    transcript: List[dict],
    started_at: float,
    *,
    split: bool = True,
) -> List[dict]:
    """Keep microphone items fixed as ``me`` and align remote items to system VAD.

    The two captured tracks share the same PCM zero.  We therefore never pass
    the mixed track through identity clustering and never allow a system
    cluster to become ``me``.
    """

    remote_items = [
        dict(item)
        for item in transcript
        if item.get("speakerId") != SPEAKER_ID_ME
    ]
    mic_items = [
        dict(item)
        for item in transcript
        if item.get("speakerId") == SPEAKER_ID_ME
    ]
    if remote_items and result.get("segments"):
        aligned_remote = align_transcript(
            result, remote_items, started_at, split=split
        )
    else:
        fallback = "remote-1"
        aligned_remote = []
        for item in remote_items:
            row = dict(item)
            row["speakerId"] = fallback
            row["speaker"] = "对方"
            aligned_remote.append(row)

    aligned_mic: List[dict] = []
    for item in mic_items:
        row = dict(item)
        row["speakerId"] = SPEAKER_ID_ME
        row["speaker"] = "我"
        aligned_mic.append(row)
    out = sorted(
        aligned_mic + aligned_remote,
        key=lambda item: (float(item.get("at") or 0), str(item.get("id") or "")),
    )
    result["transcriptItems"] = len([item for item in out if item.get("isFinal", True)])
    result["microphoneItems"] = len(aligned_mic)
    result["remoteItems"] = len(aligned_remote)
    result["aligned"] = True
    return out


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    ap = argparse.ArgumentParser(description="会后本地说话人分离")
    ap.add_argument("--wav")
    ap.add_argument(
        "--meeting-mode",
        choices=("in_person", "online"),
        default="in_person",
    )
    ap.add_argument("--mic-wav", help="线上麦克风音轨（仅记录来源，不参与远端聚类）")
    ap.add_argument("--system-wav", help="线上系统回环音轨；线上模式只分离此文件")
    ap.add_argument("--speaker-count", type=int, help="本场总人数软约束（线上远端数=总人数-1）")
    ap.add_argument("--enroll", action="append",
                    help="「我」的注册 wav（可选）；可重复传多个（多段注册）")
    ap.add_argument("--me-threshold", type=float, default=0.65)
    ap.add_argument("--cluster-th", type=float, default=DEFAULT_CLUSTER_TH)
    ap.add_argument("--min-cluster-segments", type=int, default=3)
    ap.add_argument("--min-cluster-seconds", type=float, default=8.0)
    ap.add_argument(
        "--no-split",
        action="store_true",
        help="只重新打标签，不按说话人边界切开过长的 final",
    )
    ap.add_argument("--transcript-json", help="转写数组 JSON 文件")
    ap.add_argument("--started-at", type=float, help="会议 startedAt 毫秒时间戳")
    ap.add_argument("--out-json", help="完整结果写入文件；默认只 stdout 一行")
    ap.add_argument("--model", default=DEFAULT_SPK_MODEL)
    ap.add_argument("--vad", default=DEFAULT_VAD_MODEL)
    args = ap.parse_args()

    wav_path = args.system_wav if args.meeting_mode == "online" and args.system_wav else args.wav
    if not wav_path or not os.path.isfile(wav_path):
        print(json.dumps({"ok": False, "error": f"找不到 wav：{wav_path}"}, ensure_ascii=False))
        sys.exit(2)

    try:
        if args.meeting_mode == "online":
            result = diarize_online(
                wav_path,
                speaker_count=args.speaker_count,
                cluster_th=args.cluster_th,
                min_cluster_segments=args.min_cluster_segments,
                min_cluster_seconds=args.min_cluster_seconds,
                spk_model=args.model,
                vad_model=args.vad,
            )
        else:
            result = diarize(
                wav_path,
                enroll_wav=args.enroll,
                me_threshold=args.me_threshold,
                cluster_th=args.cluster_th,
                min_cluster_segments=args.min_cluster_segments,
                min_cluster_seconds=args.min_cluster_seconds,
                spk_model=args.model,
                vad_model=args.vad,
            )
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        sys.exit(1)

    if args.transcript_json and result.get("ok"):
        if args.started_at is None:
            result["alignWarning"] = "未提供 --started-at，跳过转写对齐"
        else:
            with open(args.transcript_json, "r", encoding="utf-8") as f:
                transcript = json.load(f)
            if not isinstance(transcript, list):
                result["alignWarning"] = "transcript-json 必须是数组"
            else:
                aligned = (
                    align_online_transcript(
                        result,
                        transcript,
                        float(args.started_at),
                        split=not args.no_split,
                    )
                    if args.meeting_mode == "online"
                    else align_transcript(
                        result,
                        transcript,
                        float(args.started_at),
                        split=not args.no_split,
                    )
                )
                result["transcript"] = aligned
                result["aligned"] = True

    text = json.dumps(result, ensure_ascii=False)
    if args.out_json:
        with open(args.out_json, "w", encoding="utf-8") as f:
            f.write(text)
    print(text)


if __name__ == "__main__":
    main()
