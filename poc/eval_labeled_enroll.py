"""
用「人工/转写标注」做真注册留出验证（方案 D1 · 6b）。

输入：
  - 16k 单声道 wav（可用 ffmpeg 从 mp3 转）
  - 转写 md：每段标题行为「说话人  HH:MM:SS」

默认把名字含「（我）」或匹配 --me 的说话人当「我」：
  前半段注册、后半段测误拒；其他人测误纳。

用法：
  python eval_labeled_enroll.py \\
    --wav eval/_work_16k.wav \\
    --md  "eval/07-21 工作交接与职责梳理.md" \\
    --me "宸（我）"
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
import wave

import numpy as np

try:
    import sherpa_onnx as so
except ImportError:
    sys.exit("请先安装：pip install sherpa-onnx")

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_SPK = os.path.join(
    _HERE, "models", "3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx")

MIN_SEG_SEC = 0.7
HEADER = re.compile(r"^(.+?)\s+(\d{2}:\d{2}:\d{2})\s*$")


def parse_ts(ts: str) -> float:
    h, m, s = ts.split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)


def parse_md(path: str):
    """返回 [(start_sec, speaker), ...] 按时间排序。"""
    text = open(path, encoding="utf-8").read()
    items = []
    for line in text.splitlines():
        m = HEADER.match(line.strip())
        if not m:
            continue
        sp, ts = m.group(1).strip(), m.group(2)
        items.append((parse_ts(ts), sp))
    items.sort(key=lambda x: x[0])
    # 去重：同一秒同一人只留一条
    out, seen = [], set()
    for t, sp in items:
        key = (round(t, 2), sp)
        if key in seen:
            continue
        seen.add(key)
        out.append((t, sp))
    return out


def read_wav(path: str):
    with wave.open(path, "rb") as f:
        if f.getnchannels() != 1:
            sys.exit(f"需要单声道，实际 {f.getnchannels()} 声道")
        if f.getsampwidth() != 2:
            sys.exit("需要 16-bit PCM")
        rate = f.getframerate()
        if rate != 16000:
            sys.exit(f"需要 16kHz，实际 {rate}Hz")
        raw = f.readframes(f.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def segments_from_labels(labeled, audio_sec: float, max_seg_sec: float = 20.0):
    """标注只有起点 → 终点取下一段起点，并截断到 max_seg_sec。"""
    segs = []
    for i, (start, sp) in enumerate(labeled):
        if start >= audio_sec:
            break
        if i + 1 < len(labeled):
            end = min(labeled[i + 1][0], start + max_seg_sec, audio_sec)
        else:
            end = min(start + 5.0, audio_sec)
        if end - start < MIN_SEG_SEC:
            continue
        segs.append((start, end, sp))
    return segs


def embed_segments(extractor, samples, segs, threads_desc=""):
    vectors = []
    meta = []
    elapsed = []
    for start, end, sp in segs:
        chunk = samples[int(start * 16000): int(end * 16000)]
        if len(chunk) < int(MIN_SEG_SEC * 16000):
            continue
        t0 = time.time()
        stream = extractor.create_stream()
        stream.accept_waveform(sample_rate=16000, waveform=chunk)
        stream.input_finished()
        vec = np.array(extractor.compute(stream), dtype=np.float32)
        elapsed.append(time.time() - t0)
        vectors.append(vec / (np.linalg.norm(vec) + 1e-9))
        meta.append((start, end, sp))
    return np.array(vectors), meta, elapsed


def is_me(speaker: str, me_names: set[str]) -> bool:
    if speaker in me_names:
        return True
    # 兼容「宸（我）」这类写法
    if "（我）" in speaker or "(我)" in speaker:
        return True
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wav", required=True)
    ap.add_argument("--md", required=True)
    ap.add_argument("--me", default="宸（我）",
                    help="「我」的说话人名字（md 标题里的原文）")
    ap.add_argument("--model", default=_DEFAULT_SPK)
    ap.add_argument("--threads", type=int, default=2)
    ap.add_argument("--enroll-ratio", type=float, default=0.5)
    ap.add_argument("--max-seg-sec", type=float, default=20.0)
    # 长录音可选：只取前 N 分钟的标注（加速试跑）
    ap.add_argument("--until-min", type=float, default=0,
                    help="只使用前 N 分钟标注（0=全部）")
    args = ap.parse_args()

    for p in (args.wav, args.md, args.model):
        if not os.path.exists(p):
            sys.exit(f"找不到：{p}")

    labeled = parse_md(args.md)
    if args.until_min and args.until_min > 0:
        cut = args.until_min * 60
        labeled = [(t, sp) for t, sp in labeled if t <= cut]
    if len(labeled) < 10:
        sys.exit("标注段太少")

    samples = read_wav(args.wav)
    audio_sec = len(samples) / 16000
    print(f"录音：{os.path.basename(args.wav)}  {audio_sec/60:.1f} 分钟")
    print(f"标注：{os.path.basename(args.md)}  {len(labeled)} 条 "
          f"（{labeled[0][0]:.0f}s → {labeled[-1][0]:.0f}s）")
    print(f"「我」：{args.me}")

    segs = segments_from_labels(labeled, audio_sec, max_seg_sec=args.max_seg_sec)
    me_names = {args.me}
    me_segs = [s for s in segs if is_me(s[2], me_names)]
    other_segs = [s for s in segs if not is_me(s[2], me_names)]
    print(f"有效段（≥{MIN_SEG_SEC}s）：我 {len(me_segs)} / 别人 {len(other_segs)}")
    if len(me_segs) < 6:
        sys.exit("「我」的有效段不足 6，无法留出")

    print("\n提取声纹…")
    extractor = so.SpeakerEmbeddingExtractor(
        so.SpeakerEmbeddingExtractorConfig(
            model=args.model, num_threads=args.threads))
    # 合并提取，避免两套循环
    all_segs = me_segs + other_segs
    vectors, meta, elapsed = embed_segments(extractor, samples, all_segs)
    if elapsed:
        print(f"  {len(vectors)} 段，中位 {np.median(elapsed)*1000:.0f}ms/段，"
              f"相对实时率 {sum(elapsed)/sum(e-s for s,e,_ in meta):.4f}")

    me_idx = [i for i, m in enumerate(meta) if is_me(m[2], me_names)]
    other_idx = [i for i, m in enumerate(meta) if not is_me(m[2], me_names)]
    # 按时间排序后前半注册
    me_idx_sorted = sorted(me_idx, key=lambda i: meta[i][0])
    n_enroll = max(2, int(len(me_idx_sorted) * args.enroll_ratio))
    if n_enroll >= len(me_idx_sorted) - 1:
        n_enroll = len(me_idx_sorted) // 2
    enroll_idx = me_idx_sorted[:n_enroll]
    test_idx = me_idx_sorted[n_enroll:]
    enroll_sec = sum(meta[i][1] - meta[i][0] for i in enroll_idx)
    test_sec = sum(meta[i][1] - meta[i][0] for i in test_idx)

    manager = so.SpeakerEmbeddingManager(vectors.shape[1])
    manager.add("我", [vectors[i].tolist() for i in enroll_idx])
    same = np.array([manager.score("我", vectors[i].tolist()) for i in test_idx])
    other = np.array(
        [manager.score("我", vectors[i].tolist()) for i in other_idx]
    ) if other_idx else np.array([])

    print("\n======== 真注册留出（按标注「我」）========")
    print(f"  注册 {len(enroll_idx)} 段 / {enroll_sec:.0f}s  "
          f"（{meta[enroll_idx[0]][0]:.0f}s → {meta[enroll_idx[-1]][1]:.0f}s）")
    print(f"  测试同人 {len(test_idx)} 段 / {test_sec:.0f}s  "
          f"（{meta[test_idx[0]][0]:.0f}s → {meta[test_idx[-1]][1]:.0f}s）")
    print(f"  别人 {len(other_idx)} 段")
    print(f"  同人相似度：均值 {same.mean():.3f}  中位 {np.median(same):.3f}  "
          f"5%分位 {np.percentile(same, 5):.3f}")
    if len(other):
        print(f"  别人相似度：均值 {other.mean():.3f}  中位 {np.median(other):.3f}  "
              f"95%分位 {np.percentile(other, 95):.3f}")
        gap = float(same.mean() - other.mean())
        print(f"  ★ 留出间隔：{gap:.3f}  ", end="")
        if gap > 0.20:
            print("→ 良好")
        elif gap > 0.10:
            print("→ 偏弱但可用")
        else:
            print("→ 偏弱/不可用")

        rows, best = [], None
        for th in np.arange(0.30, 0.90, 0.05):
            fr = float((same < th).mean())
            fa = float((other >= th).mean())
            total = (fr + fa) / 2
            rows.append((th, fr, fa, total))
            if best is None or total < best[1]:
                best = (th, total)
        print("\n  阈值    误拒(同人)  误纳(别人)  总错误")
        for th, fr, fa, total in rows:
            mark = "  ← 建议" if best and th == best[0] else ""
            print(f"  {th:.2f}    {fr:>7.1%}     {fa:>7.1%}    {total:>6.1%}{mark}")
        if best:
            print(f"  建议工作阈值 ≈ {best[0]:.2f}")

        # 按别人名字拆开看谁最容易被误认
        by_sp = {}
        for i in other_idx:
            sp = meta[i][2]
            sc = manager.score("我", vectors[i].tolist())
            by_sp.setdefault(sp, []).append(sc)
        print("\n  各对方说话人 vs「我」相似度：")
        for sp, scores in sorted(by_sp.items(),
                                 key=lambda x: -np.mean(x[1])):
            a = np.array(scores)
            print(f"    {sp}: n={len(a)}  均值 {a.mean():.3f}  "
                  f"95% {np.percentile(a, 95):.3f}")
    else:
        print("  没有「别人」段")

    # 短注册压力测试：只用前 ~20s / 前 5 段
    short_n = 0
    short_sec = 0.0
    short_idx = []
    for i in me_idx_sorted:
        short_idx.append(i)
        short_n += 1
        short_sec += meta[i][1] - meta[i][0]
        if short_sec >= 20 and short_n >= 3:
            break
    if len(short_idx) >= 3 and len(me_idx_sorted) > len(short_idx) + 2:
        rest = me_idx_sorted[len(short_idx):]
        mgr2 = so.SpeakerEmbeddingManager(vectors.shape[1])
        mgr2.add("我", [vectors[i].tolist() for i in short_idx])
        same2 = np.array([mgr2.score("我", vectors[i].tolist()) for i in rest])
        other2 = np.array(
            [mgr2.score("我", vectors[i].tolist()) for i in other_idx])
        print(f"\n-------- 短注册压力（约 {short_sec:.0f}s / {len(short_idx)} 段，"
              f"模拟会前 20 秒）--------")
        print(f"  同人：均值 {same2.mean():.3f}  5% {np.percentile(same2, 5):.3f}")
        print(f"  别人：均值 {other2.mean():.3f}  95% {np.percentile(other2, 95):.3f}")
        print(f"  间隔：{same2.mean() - other2.mean():.3f}")
        best2 = None
        for th in np.arange(0.30, 0.90, 0.05):
            fr = float((same2 < th).mean())
            fa = float((other2 >= th).mean())
            total = (fr + fa) / 2
            if best2 is None or total < best2[1]:
                best2 = (th, total, fr, fa)
        if best2:
            print(f"  建议阈值 ≈ {best2[0]:.2f}  "
                  f"（误拒 {best2[2]:.1%}  误纳 {best2[3]:.1%}）")

    print("\n⚠️ 标注时间只有起点，终点用「下一段起点」近似；"
          "若转写延迟大，个别段可能掺进邻人尾音。")
    print("   原始 md/mp3 未改写；工作 wav 可删。")


if __name__ == "__main__":
    main()
