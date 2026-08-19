"""
本地声纹方案（方案 D1）可行性验证 —— 在【真实会议录音】上跑，不看论文数字。

回答三个问题：
  Q1 这条音频链路上，声纹嵌入到底能不能把人分开？
     （单麦远场 + 16k 混音，和论文的近场干净语料差很远，必须实测）
  Q2 讯飞的实时盲分，错在哪、错多少？
  Q3 「注册我 → 判断这句是不是我」的相似度阈值该定在多少？

⚠️ 为什么不能只看 CN-Celeb 的 EER：那是开集验证在困难配对上的指标，
   和我们「闭集、少人数、固定信道」的任务不是一回事，高估或低估都有可能。
   唯一可信的是在自己的音频上测。

用法：
    # Q1+Q2：无需任何标注，直接跑真实录音
    python verify_speaker.py --wav <会议录音.wav>

    # 叠加 Q2 的对照：把讯飞当时的说话人标签拉进来比对
    python verify_speaker.py --wav <录音.wav> --meeting-id <会议ID>

    # Q3：注册一段自己的声音后，测阈值
    python verify_speaker.py --wav <录音.wav> --enroll <我的声音.wav>

    # Q3 代理：本场没有「我」的发言时，用最大说话人簇做留出验证
    # （前半注册、后半测试，不依赖讯飞标签）
    python verify_speaker.py --wav <录音.wav> --proxy-me

    # 换模型对比
    python verify_speaker.py --wav <录音.wav> --model models/3dspeaker_speech_eres2netv2_sv_zh-cn_16k-common.onnx
"""

import argparse
import os
import sqlite3
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
_DEFAULT_VAD = os.path.join(_HERE, "models", "silero_vad_v5.onnx")

# 短片段的嵌入不可靠（信息量不足），统计时丢掉。
# 0.7s 是经验下限：比这更短的「嗯」「对」提取出来的向量噪声占主导。
MIN_SEG_SEC = 0.7


def read_wav(path):
    """读 16k 单声道 wav，返回 float32 [-1,1]。采样率不符直接报错而不是静默重采样。"""
    with wave.open(path, "rb") as f:
        if f.getnchannels() != 1:
            sys.exit(f"需要单声道 wav，实际 {f.getnchannels()} 声道")
        if f.getsampwidth() != 2:
            sys.exit("需要 16-bit PCM")
        rate = f.getframerate()
        if rate != 16000:
            sys.exit(f"需要 16kHz，实际 {rate}Hz（模型按 16k 训练，重采样会影响结论）")
        raw = f.readframes(f.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def segment_speech(samples, vad_model):
    """VAD 切出语音片段，返回 [(start_sec, end_sec, samples), ...]"""
    cfg = so.VadModelConfig(
        silero_vad=so.SileroVadModelConfig(
            model=vad_model,
            threshold=0.5,
            min_silence_duration=0.5,
            min_speech_duration=0.25,
            max_speech_duration=20,
        ),
        sample_rate=16000,
    )
    vad = so.VoiceActivityDetector(cfg, buffer_size_in_seconds=60)
    window = cfg.silero_vad.window_size
    segments = []
    for i in range(0, len(samples), window):
        chunk = samples[i:i + window]
        if len(chunk) < window:
            break
        vad.accept_waveform(chunk)
        while not vad.empty():
            seg = vad.front
            start = seg.start / 16000
            segments.append((start, start + len(seg.samples) / 16000,
                             np.array(seg.samples, dtype=np.float32)))
            vad.pop()
    vad.flush()
    while not vad.empty():
        seg = vad.front
        start = seg.start / 16000
        segments.append((start, start + len(seg.samples) / 16000,
                         np.array(seg.samples, dtype=np.float32)))
        vad.pop()
    return segments


def embed_all(extractor, segments):
    """逐段提取声纹向量，同时统计单段耗时（关系到会中会不会卡）。"""
    vectors = []
    kept = []
    elapsed = []
    for start, end, samples in segments:
        if end - start < MIN_SEG_SEC:
            continue
        t0 = time.time()
        stream = extractor.create_stream()
        stream.accept_waveform(sample_rate=16000, waveform=samples)
        stream.input_finished()
        vec = np.array(extractor.compute(stream), dtype=np.float32)
        elapsed.append(time.time() - t0)
        vectors.append(vec / (np.linalg.norm(vec) + 1e-9))
        kept.append((start, end))
    return np.array(vectors), kept, elapsed


def cluster(vectors, threshold):
    """余弦相似度上的凝聚聚类（average linkage），返回每段的簇编号。

    自己实现而不引 scipy：依赖越少，越容易搬进桌面端。
    段数是百级，O(n^3) 完全够用。
    """
    n = len(vectors)
    if n == 0:
        return np.empty(0, dtype=int)
    # 簇间相似度增量维护（Lance-Williams average linkage）：
    # 每次合并后按大小加权更新一行一列，整体 O(n^2)。
    # 朴素做法每轮重扫所有点对是 O(n^3)，20 分钟的会有几百段，会卡到不可用。
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
        # b 并入 a：新相似度 = 两簇按大小加权平均
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


def load_transcript_labels(db_path, meeting_id):
    """取讯飞当时给出的说话人标签（相对会议开始的秒数 + 名字）。"""
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    meeting = con.execute(
        "SELECT started_at FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
    if not meeting:
        sys.exit(f"库里没有会议 {meeting_id}")
    rows = con.execute(
        "SELECT speaker, at FROM transcripts WHERE meeting_id = ? AND is_final = 1"
        " ORDER BY at", (meeting_id,)).fetchall()
    started = meeting["started_at"]
    return [((r["at"] - started) / 1000.0, r["speaker"]) for r in rows]


def align_labels(kept, labeled, audio_seconds):
    """把讯飞标签对齐到 VAD 片段。

    ⚠️ 时间基准不同，必须先估偏移：转写记的是【识别结果送达】的墙上时刻，
       录音文件 t=0 则是 AudioRecorder 建好的时刻（在 ASR 连接之后），
       两者差了初始化时间 + ASR 识别延迟，实测在数秒量级。
       这里用「最后一条转写对齐到最后一段语音」来估，再对每段取最近的标签。
       这是近似 —— 因此本函数的输出只用于【参考对照】，不作为判定依据。
    """
    if not labeled or not kept:
        return None
    offset = labeled[-1][0] - kept[-1][1]
    out = []
    for start, end in kept:
        mid = (start + end) / 2 + offset
        nearest = min(labeled, key=lambda x: abs(x[0] - mid))
        out.append(nearest[1] if abs(nearest[0] - mid) < 15 else None)
    return out, offset


def self_consistency(extractor, segments):
    """无标注的可分性下界测量 —— 本脚本最可信的一项。

    ⚠️ 为什么必须有这个：用讯飞的标签当基准去评价声纹，是拿【已知有错的尺】
       量新方案。讯飞把几个男声并成一个 ID 正是 bug ⑩ 本身；那些"同一说话人"
       的段落其实来自不同的人，会机械地压低类内相似度，得出"声纹不可分"的
       假结论。时间对齐也只是单锚点估计，会进一步污染。

    这里改用一个不依赖任何标注的事实：**一个连续语音段内部几乎必然是同一个人**
    （VAD 以 0.5s 静音切分，人很少在不停顿的情况下换人接话）。
    把每个较长段一分为二，两半就是一对【确定同源】的样本。
    它们的相似度分布 = 这条音频链路上"同一人应该长什么样"的可信基准。

    对照组用不同段之间的随机配对（多说话人会议里大多是异源）。
    两个分布拉不开 → 声纹在这条链路上确实不可用；
    拉得开 → 可用，之前的坏结论来自坏基准。
    """
    same_pairs = []
    halves = []
    for start, end, samples in segments:
        if end - start < 2 * MIN_SEG_SEC + 0.6:
            continue
        mid = len(samples) // 2
        pair = []
        for part in (samples[:mid], samples[mid:]):
            stream = extractor.create_stream()
            stream.accept_waveform(sample_rate=16000, waveform=part)
            stream.input_finished()
            v = np.array(extractor.compute(stream), dtype=np.float32)
            pair.append(v / (np.linalg.norm(v) + 1e-9))
        same_pairs.append(float(pair[0] @ pair[1]))
        halves.append(pair[0])
    if len(same_pairs) < 10:
        return None
    halves = np.array(halves)
    cross = halves @ halves.T
    iu = np.triu_indices(len(halves), k=1)
    return np.array(same_pairs), cross[iu]


def report_separability(vectors, labels_by_speaker):
    """类内 vs 类间相似度分布 —— 判断这条音频链路上声纹到底可不可分。"""
    names = sorted({n for n in labels_by_speaker if n})
    idx = {n: [i for i, l in enumerate(labels_by_speaker) if l == n] for n in names}
    idx = {n: v for n, v in idx.items() if len(v) >= 3}
    if len(idx) < 2:
        return None
    sim = vectors @ vectors.T
    within, between = [], []
    for n, ii in idx.items():
        for a in range(len(ii)):
            for b in range(a + 1, len(ii)):
                within.append(sim[ii[a], ii[b]])
    ns = list(idx)
    for a in range(len(ns)):
        for b in range(a + 1, len(ns)):
            for i in idx[ns[a]]:
                for j in idx[ns[b]]:
                    between.append(sim[i, j])
    return np.array(within), np.array(between), idx


def sweep_threshold(within, between):
    """扫阈值，给出「判成同一人」的最佳工作点。

    这直接就是 D1 里 verify() 的 threshold —— 定高了漏认自己，定低了把别人当成我。
    """
    print("\n  阈值    误拒(同一人被判为不同)  误纳(不同人被判为同一人)  总错误")
    best = None
    for th in np.arange(0.30, 0.85, 0.05):
        fr = float((within < th).mean())      # False Reject
        fa = float((between >= th).mean())    # False Accept
        total = (fr + fa) / 2
        flag = ""
        if best is None or total < best[1]:
            best, flag = (th, total), ""
        print(f"  {th:.2f}    {fr:>8.1%}                 {fa:>8.1%}              {total:>6.1%}")
    return best


def proxy_leave_out(vectors, kept, cluster_th=0.60, enroll_ratio=0.5):
    """用最大说话人簇模拟「注册我 → 认出我」的留出验证。

    为什么需要：本场录音里用户没有发言，没法真注册「我」；
    但「注册 → 1:1 验证」这条链路仍要在真实远场混音上证一次。

    方法：
      1. 声纹聚类定义身份（不依赖讯飞标签 —— bug ⑩ 会把多人并成一个 ID）
      2. 取最大簇当代理「我」
      3. 按时间排序后前半注册、后半测试（真留出，不是自欺）
      4. 用与产品相同的 SpeakerEmbeddingManager 打分
      5. 其余簇当「别人」，扫阈值看误拒/误纳

    局限（必须写进报告，别当铁板钉钉）：
      - 最大簇不一定是同一个人（聚类阈值选错时会并簇）
      - 代理身份 ≠ 真实「我」的声道/音色，只能证明链路可行
      - enroll/test 来自同一场会、同一信道，比「会前注册、会中验证」略乐观
      - 注册时长往往 > 真会前 20 秒，略乐观
    """
    labels = cluster(vectors, cluster_th)
    sizes = np.bincount(labels)
    if len(sizes) == 0 or sizes.max() < 6:
        return None
    proxy_id = int(np.argmax(sizes))
    proxy_idx = np.where(labels == proxy_id)[0]
    # 按时间排，避免「随机对半分」把相邻段分到两侧还互相泄露上下文
    order = sorted(proxy_idx, key=lambda i: kept[i][0])
    n_enroll = max(2, int(len(order) * enroll_ratio))
    if n_enroll >= len(order) - 1:
        n_enroll = len(order) // 2
    if n_enroll < 2 or len(order) - n_enroll < 2:
        return None
    enroll_idx = order[:n_enroll]
    test_idx = order[n_enroll:]
    other_idx = np.where(labels != proxy_id)[0]
    # 与 --enroll 路径一致：走 sherpa 的 SpeakerEmbeddingManager
    manager = so.SpeakerEmbeddingManager(vectors.shape[1])
    manager.add("我", [vectors[i].tolist() for i in enroll_idx])
    same_scores = np.array(
        [manager.score("我", vectors[i].tolist()) for i in test_idx])
    other_scores = np.array(
        [manager.score("我", vectors[i].tolist()) for i in other_idx]
    ) if len(other_idx) else np.array([])
    return {
        "cluster_th": cluster_th,
        "n_clusters": int(len(sizes)),
        "proxy_size": int(sizes[proxy_id]),
        "sizes_top": sorted(sizes.tolist(), reverse=True)[:8],
        "n_enroll": len(enroll_idx),
        "n_test": len(test_idx),
        "n_other": len(other_idx),
        "enroll_sec": sum(kept[i][1] - kept[i][0] for i in enroll_idx),
        "same_scores": same_scores,
        "other_scores": other_scores,
    }


def report_proxy_leave_out(result):
    """打印留出验证结果，并扫阈值。"""
    same = result["same_scores"]
    other = result["other_scores"]
    print(f"  聚类阈值 {result['cluster_th']:.2f} → {result['n_clusters']} 个簇，"
          f"前几簇段数 {result['sizes_top']}")
    print(f"  代理「我」= 最大簇（{result['proxy_size']} 段）")
    print(f"  注册 {result['n_enroll']} 段（约 {result['enroll_sec']:.0f}s） / "
          f"测试同人 {result['n_test']} 段 / 别人 {result['n_other']} 段")
    print(f"  同人测试相似度：均值 {same.mean():.3f}  "
          f"中位 {np.median(same):.3f}  "
          f"5% 分位 {np.percentile(same, 5):.3f}")
    if len(other):
        print(f"  别人相似度：    均值 {other.mean():.3f}  "
              f"中位 {np.median(other):.3f}  "
              f"95% 分位 {np.percentile(other, 95):.3f}")
        gap = same.mean() - other.mean()
        print(f"  ★ 留出间隔：{gap:.3f}  ", end="")
        if gap > 0.20:
            print("→ 良好，「注册→认出」链路在这条音频上可用")
        elif gap > 0.10:
            print("→ 偏弱，阈值要细调，或注册样本再加长")
        else:
            print("→ 不可用，链路在这条音频上拉不开")
        rows = []
        best = None
        for th in np.arange(0.30, 0.85, 0.05):
            fr = float((same < th).mean())
            fa = float((other >= th).mean())
            total = (fr + fa) / 2
            rows.append((th, fr, fa, total))
            if best is None or total < best[1]:
                best = (th, total)
        print("\n  阈值    误拒(同人未认出)  误纳(别人认成我)  总错误")
        for th, fr, fa, total in rows:
            mark = "  ← 建议" if best and th == best[0] else ""
            print(f"  {th:.2f}    {fr:>8.1%}            {fa:>8.1%}         "
                  f"{total:>6.1%}{mark}")
        if best:
            print(f"  建议工作阈值 ≈ {best[0]:.2f}")
    else:
        print("  没有「别人」簇可做误纳估计（整场几乎只有一个人？）")
    print("  ⚠️ 代理身份 ≠ 真实的你；同一场会内留出比会前注册略乐观。"
          "真正确认仍需你用同一支麦录 20 秒。")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wav", required=True, help="真实会议录音（16k 单声道）")
    ap.add_argument("--model", default=_DEFAULT_SPK, help="声纹模型 onnx")
    ap.add_argument("--vad", default=_DEFAULT_VAD, help="Silero VAD onnx")
    ap.add_argument("--meeting-id", help="拉取讯飞当时的说话人标签做对照")
    ap.add_argument("--db", help="meeting-copilot.sqlite 路径")
    ap.add_argument("--enroll", help="我的注册音频 wav（用于 1:1 验证）")
    ap.add_argument(
        "--proxy-me", action="store_true",
        help="本场无本人发言时：用最大簇做前半注册/后半测试的留出验证")
    ap.add_argument(
        "--proxy-cluster-th", type=float, default=0.65,
        help="--proxy-me 用的聚类阈值（默认 0.65；过低会并簇，污染代理身份）")
    ap.add_argument("--threads", type=int, default=2)
    args = ap.parse_args()

    for path in (args.wav, args.model, args.vad):
        if not os.path.exists(path):
            sys.exit(f"找不到文件：{path}\n（模型见 poc/models/README.md）")

    samples = read_wav(args.wav)
    duration = len(samples) / 16000
    print(f"录音：{os.path.basename(args.wav)}  时长 {duration/60:.1f} 分钟")
    print(f"声纹模型：{os.path.basename(args.model)}")

    print("\n[1/4] VAD 切分语音段…")
    segments = segment_speech(samples, args.vad)
    speech = sum(e - s for s, e, _ in segments)
    print(f"  切出 {len(segments)} 段，语音总时长 {speech/60:.1f} 分钟"
          f"（占 {speech/duration:.0%}）")

    print("\n[2/4] 提取声纹向量…")
    extractor = so.SpeakerEmbeddingExtractor(
        so.SpeakerEmbeddingExtractorConfig(model=args.model,
                                           num_threads=args.threads))
    vectors, kept, elapsed = embed_all(extractor, segments)
    print(f"  有效段（≥{MIN_SEG_SEC}s）{len(kept)} 段，向量维度 {vectors.shape[1]}")
    if elapsed:
        print(f"  单段耗时：中位 {np.median(elapsed)*1000:.0f}ms  "
              f"最大 {max(elapsed)*1000:.0f}ms  "
              f"→ 相对实时率 {np.sum(elapsed)/speech:.4f}"
              f"（远小于 1 才不会拖累会中）")

    print("\n[3/4] 无监督聚类：这条音频上到底能分出几个人？")
    for th in (0.45, 0.50, 0.55, 0.60, 0.65):
        labels = cluster(vectors, th)
        sizes = sorted(np.bincount(labels).tolist(), reverse=True)
        print(f"  相似度阈值 {th:.2f} → {len(sizes)} 个簇，各簇段数 {sizes[:8]}")

    print("\n[4/5] 可分性下界（无标注，本脚本最可信的一项）")
    consistency = self_consistency(extractor, segments)
    if consistency is None:
        print("  长段不足，跳过")
    else:
        same, mixed = consistency
        print(f"  确定同源（同一段的前后半）{len(same)} 对："
              f"相似度均值 {same.mean():.3f}，5% 分位 {np.percentile(same, 5):.3f}")
        print(f"  随机配对（跨段）：均值 {mixed.mean():.3f}，"
              f"95% 分位 {np.percentile(mixed, 95):.3f}")

        # ⚠️ 随机配对【不等于】异源配对：会议里通常有人占了大半发言，
        #    随机取两段，有相当比例其实是同一个人。直接拿它当"异源"基准会
        #    抬高对照均值、压缩间隔，把可用的方案误判成不可用。
        #    这里按簇大小估同源比例 p，再从混合均值里把同源那部分剥掉：
        #        μ_mixed = p·μ_same + (1-p)·μ_diff
        #    聚类给标签不够准，但给【比例】足够 —— 要求低得多。
        sizes = np.bincount(cluster(vectors, 0.60))
        frac = sizes / sizes.sum()
        p_same = float((frac ** 2).sum())
        mu_diff = (mixed.mean() - p_same * same.mean()) / max(1 - p_same, 1e-6)
        print(f"  估计随机配对里约 {p_same:.0%} 其实是同一个人 → "
              f"剥离后异源均值 ≈ {mu_diff:.3f}")
        gap = same.mean() - mu_diff
        print(f"  ★ 可分性间隔：{gap:.3f}  ", end="")
        if gap > 0.25:
            print("→ 良好，声纹在这条音频链路上可用")
        elif gap > 0.12:
            print("→ 偏弱，需换模型或调参后再判断")
        else:
            print("→ 不可分，声纹方案在这条链路上不成立")
        # 阈值取同源分布的低分位：宁可多问一次，也别把别人认成我
        print(f"  建议起始阈值 ≈ {np.percentile(same, 10):.2f}"
              f"（同源分布 10% 分位，可按误拒/误纳偏好再调）")

    print("\n[5/5] 与讯飞当时的标签对照（仅供参考，非判据）")
    if not (args.meeting_id and args.db):
        print("  （未提供 --meeting-id / --db，跳过）")
    else:
        labeled = load_transcript_labels(args.db, args.meeting_id)
        aligned, offset = align_labels(kept, labeled, duration)
        print(f"  讯飞给出 {len({n for _, n in labeled})} 个说话人，"
              f"估计时间偏移 {offset:.1f}s")
        stats = report_separability(vectors, aligned)
        if stats is None:
            print("  可对照的说话人不足，跳过")
        else:
            within, between, idx = stats
            print(f"\n  参与统计的说话人："
                  f"{ {n: len(v) for n, v in idx.items()} }")
            print(f"  同一说话人内部相似度：均值 {within.mean():.3f} "
                  f"（越高越好，理想 >0.6）")
            print(f"  不同说话人之间相似度：均值 {between.mean():.3f} "
                  f"（越低越好，理想 <0.4）")
            gap = within.mean() - between.mean()
            print(f"  参考间隔：{gap:.3f}")
            print("  ⚠️ 此项【不可作为判据】：它拿讯飞的标签当基准，而"
                  "「讯飞把不同人并成一个 ID」正是我们要解决的 bug ⑩。")
            print("     基准本身有错，会把好方案误判成不可用。判据看上一节。")
            sweep_threshold(within, between)

    if args.enroll:
        print("\n[附加] 1:1「这是不是我」验证")
        if not os.path.exists(args.enroll):
            sys.exit(f"找不到注册音频：{args.enroll}")
        enroll_samples = read_wav(args.enroll)
        enroll_segs = segment_speech(enroll_samples, args.vad)
        enroll_vecs, enroll_kept, _ = embed_all(extractor, enroll_segs)
        if len(enroll_vecs) == 0:
            sys.exit("注册音频里没检出有效语音段（至少说满 2-3 句）")
        manager = so.SpeakerEmbeddingManager(vectors.shape[1])
        manager.add("我", [v.tolist() for v in enroll_vecs])
        print(f"  用 {len(enroll_vecs)} 段注册完成")
        scores = np.array([manager.score("我", v.tolist()) for v in vectors])
        print(f"  会议各段与「我」的相似度：中位 {np.median(scores):.3f}  "
              f"最高 {scores.max():.3f}  最低 {scores.min():.3f}")
        for th in (0.4, 0.5, 0.6, 0.7):
            hit = int((scores >= th).sum())
            print(f"    阈值 {th:.1f} → 判为「我」的段数 {hit}/{len(scores)} "
                  f"（占 {hit/len(scores):.0%}）")
        print("  ⚠️ 请人工核对：判为「我」的比例是否接近你在这场会里的实际发言占比")

    if args.proxy_me:
        print("\n[附加] 代理「我」留出验证（最大簇 · 前半注册 / 后半测试）")
        print("  背景：本场无本人发言；用聚类最大簇模拟注册→认出，"
              "不依赖讯飞标签。")
        result = proxy_leave_out(
            vectors, kept, cluster_th=args.proxy_cluster_th)
        if result is None:
            print("  最大簇段数不足，无法做留出（试调低 --proxy-cluster-th）")
        else:
            report_proxy_leave_out(result)


if __name__ == "__main__":
    main()
