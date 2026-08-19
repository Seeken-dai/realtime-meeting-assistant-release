"""评估会中 VAD 粒度对「我 / 对方」判定的影响（本地跑，不联网）：

    python eval_vad_granularity.py --wav <会议wav>

## 要回答的问题

段是"能单独归属给一个人"的最小单位。段太长 → 一段里混了好几个人，
再准的声纹也只能给它一个标签（HANDOFF §4.6）。会后分离已经把 VAD 调细到
8s/0.35s 并实测受益，**会中仍是 20s/0.5s**。

但会中不能照抄：段变短 → 每段的 embedding 更短、噪声更大 → 声纹分更飘，
而会中还要靠这些分数自适应找门槛。**是否该调细必须用数据回答。**

## 真值从哪来

用会后聚类的结果（`diarize_offline`）作真值：它用段与段之间的**相互**相似度，
不依赖注册信道，实测 cluster_th 0.50~0.70 全区间稳定（HANDOFF §4.7）。
把它展开成一条"谁在什么时候说话"的时间轴，任何粒度的候选分段都能按
**时间重叠**取到真值。

## 指标

按**时长加权**统计判对的比例——不是按段数。段数会让 0.8 秒的碎片和 20 秒的
长段同权，而用户感知到的是"多少时间被标错了"。

同时报告**最差前缀**：会中是边开边判的，只有最后一刻正确没有意义。
"""

from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import sherpa_onnx as so

from diarize_offline import diarize, embed_segments
from speaker_me import (
    DEFAULT_SPK_MODEL,
    DEFAULT_VAD_MODEL,
    MIN_ADAPTIVE_SEGMENTS,
    SAMPLE_RATE,
    enroll_from_wav,
    find_adaptive_cut,
    read_wav_mono16k,
)

# 会中当前值 / 会后当前值 / 更细的几档
CONFIGS = [
    (20.0, 0.50, "会中现值"),
    (12.0, 0.40, ""),
    (8.0, 0.35, "会后现值"),
    (6.0, 0.30, ""),
    (4.0, 0.25, ""),
]

FALLBACK_THRESHOLD = 0.65


def user_data_dir():
    return os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
                        "meeting-copilot-desktop")


def enroll_samples():
    folder = os.path.join(user_data_dir(), "voiceprint")
    if not os.path.isdir(folder):
        return []
    return sorted(os.path.join(folder, n) for n in os.listdir(folder)
                  if n.lower().endswith(".wav"))


def segment_with(samples, vad_model, max_speech, min_silence):
    """按给定粒度切段。与 speaker_me._make_vad 同参数，但这里要多组对比。"""
    cfg = so.VadModelConfig(
        silero_vad=so.SileroVadModelConfig(
            model=vad_model, threshold=0.5,
            min_silence_duration=min_silence,
            min_speech_duration=0.25,
            max_speech_duration=max_speech,
        ),
        sample_rate=SAMPLE_RATE,
    )
    vad = so.VoiceActivityDetector(cfg, buffer_size_in_seconds=60)
    window = cfg.silero_vad.window_size
    out, total = [], 0
    for i in range(0, len(samples), window):
        chunk = samples[i:i + window]
        if len(chunk) < window:
            break
        vad.accept_waveform(chunk)
        total += window
        while not vad.empty():
            seg = np.array(vad.front.samples, dtype=np.float32)
            end = total / SAMPLE_RATE
            out.append((max(0.0, end - len(seg) / SAMPLE_RATE), end, seg))
            vad.pop()
    vad.flush()
    while not vad.empty():
        seg = np.array(vad.front.samples, dtype=np.float32)
        end = total / SAMPLE_RATE
        out.append((max(0.0, end - len(seg) / SAMPLE_RATE), end, seg))
        vad.pop()
    return out


def overlap_by_label(truth_segments, start, end):
    """候选段与真值时间轴的重叠时长，按真值说话人分开统计。"""
    totals = {"me": 0.0, "other": 0.0}
    for seg in truth_segments:
        overlap = min(end, seg["end"]) - max(start, seg["start"])
        if overlap > 0:
            totals[seg["label"]] += overlap
    return totals


def evaluate(scores, times, overlaps):
    """模拟会中：逐前缀求切点，算【被正确归属的语音时长占比】。

    ⚠️ 关键是按"这一段里有多少秒真的属于被判定的那个人"计分，
       而不是"这一段的标签是否等于它的主要说话人"。
       后者会把粗粒度的问题藏起来：一个 25 秒、两个人各说一半的段，
       无论标给谁都算"和主要说话人一致"，看上去 100% 正确，
       可实际上有一半的时间被归错了人——而这正是段太长的代价。
    """
    accs = []
    for n in range(MIN_ADAPTIVE_SEGMENTS, len(scores) + 1):
        cut = find_adaptive_cut(scores[:n])
        effective = FALLBACK_THRESHOLD if cut is None else cut
        good = total = 0.0
        for score, over in zip(scores[:n], overlaps[:n]):
            predicted = "me" if score >= effective else "other"
            good += over[predicted]
            total += over["me"] + over["other"]
        accs.append(good / total if total else 0.0)
    return (accs[-1], min(accs)) if accs else (0.0, 0.0)


def purity(overlaps):
    """段纯度：每段里主要说话人占的时长比例，按时长加权平均。

    它单独回答"段切得够不够细"——纯度低就说明一段里混了不止一个人，
    这种段无论标签给谁都必然错掉一部分。
    """
    good = total = 0.0
    for over in overlaps:
        both = over["me"] + over["other"]
        if both <= 0:
            continue
        good += max(over["me"], over["other"])
        total += both
    return good / total if total else 0.0


def main():
    ap = argparse.ArgumentParser(description="会中 VAD 粒度对比")
    ap.add_argument("--wav", required=True)
    ap.add_argument("--enroll", action="append")
    args = ap.parse_args()

    enroll = args.enroll or enroll_samples()
    if not enroll:
        sys.exit("找不到声纹样本，用 --enroll 指定")

    print("① 先跑会后分离，拿聚类结果当真值…")
    truth = diarize(args.wav, enroll_wav=enroll)
    if not truth.get("ok"):
        sys.exit(f"分离失败：{truth.get('error')}")
    truth_segments = [
        {"start": s["start"], "end": s["end"],
         "label": "me" if s["speakerId"] == "me" else "other"}
        for s in truth["segments"]
    ]
    me_sec = sum(s["end"] - s["start"] for s in truth_segments if s["label"] == "me")
    other_sec = sum(s["end"] - s["start"] for s in truth_segments if s["label"] != "me")
    print(f"   真值：我 {me_sec:.0f}s / 对方 {other_sec:.0f}s"
          f"（判据 {truth.get('meDecision')}）\n")

    extractor, manager, n_enroll = enroll_from_wav(enroll, spk_model=DEFAULT_SPK_MODEL)
    samples = read_wav_mono16k(args.wav)
    print(f"② 逐个粒度评估（注册 {n_enroll} 个 embedding）\n")

    print(f"{'max/silence':>12s} {'备注':<9s} {'段数':>4s} {'中位':>6s} {'最长':>6s} "
          f"{'段纯度':>7s} {'归属正确时长':>12s} {'最差前缀':>9s} {'切点':>7s}")
    print("-" * 82)
    for max_speech, min_silence, note in CONFIGS:
        raw = segment_with(samples, DEFAULT_VAD_MODEL, max_speech, min_silence)
        vectors, kept = embed_segments(extractor, raw)
        if len(kept) < MIN_ADAPTIVE_SEGMENTS:
            print(f"{max_speech:5.0f}s/{min_silence:.2f}s  段数不足，跳过")
            continue
        scores = [float(manager.score("我", v.tolist())) for v in vectors]
        overlaps = [overlap_by_label(truth_segments, a, b) for a, b in kept]
        full, worst = evaluate(scores, kept, overlaps)
        durations = sorted(b - a for a, b in kept)
        cut = find_adaptive_cut(scores)
        print(f"{max_speech:5.0f}s/{min_silence:.2f}s {note:<9s} {len(kept):4d} "
              f"{durations[len(durations) // 2]:5.1f}s {max(durations):5.1f}s "
              f"{purity(overlaps) * 100:6.1f}% {full * 100:11.1f}% "
              f"{worst * 100:8.1f}% {'None' if cut is None else f'{cut:6.3f}'}")

    print("\n段纯度 = 每段里主要说话人占的时长比例。它低就说明一段里混了不止一个人，"
          "\n         这种段无论标签给谁都必然错掉一部分，切分也切不出来。"
          "\n归属正确时长 = 端到端指标：按最终判定，有多少秒的语音被归给了对的人。"
          "\n\n判断口径：更细的粒度要【同时】提高纯度且不降低归属正确率才值得改。"
          "\n段数变多本身不是收益。")


if __name__ == "__main__":
    main()
