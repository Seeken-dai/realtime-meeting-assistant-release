"""会中「我 / 对方」判定门槛的评测（无需 pytest / 联网 / 模型）：

    python -m tests.test_adaptive_cut

## 为什么需要它

会中靠 `speaker_me.find_adaptive_cut()` 每场会自己找断层来区分「我 / 对方」。
这个判据是启发式的，出错时**不会报错，只会静静地把对方标成「我」**——
而「我说话不触发建议」，于是整段时间一批建议都不出。
实测（HANDOFF §4.9 ②）最坏情况是连续两分钟标错、210 秒没有任何建议。

肉眼看不出来，只能量。基准数据来自一场真实会议，
真值用会后聚类的归属（理由见 fixture 里的 why_label_is_truth）。

## 指标口径

对每个长度 ≥ MIN_ADAPTIVE_SEGMENTS 的**前缀**求一次切点，再用这个切点给
该前缀的所有段分类，与真值比对。之所以逐前缀评估而不是只看全场：
会中是边开会边判的，只有最后一刻正确没有意义——前 20 段判错，
那 20 段对应的建议就已经错过了。
"""

from __future__ import annotations

import json
import os
import sys

from speaker_me import MIN_ADAPTIVE_SEGMENTS, find_adaptive_cut

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)
FIXTURE = os.path.join(PROJECT_ROOT, "eval", "adaptive_cut_fixture.json")

# 达标线。初版实现（argmax 最大断层）是 73% / 83.9% / 72%；
# 换成一维 2-means 后是 100% / 99.9% / 94%。
# ⚠️ 平均值别写成 1.00：只要有一个前缀不满分，均值就永远差一点点，
#    那样这条断言无论实现多好都过不了。
TARGET_FULL = 1.00
TARGET_MEAN = 0.99
TARGET_WORST = 0.94

# 切点为 None（判据不成立）时，会中退回的固定阈值
FALLBACK_THRESHOLD = 0.65


def load_fixture():
    with open(FIXTURE, encoding="utf-8") as f:
        data = json.load(f)
    segments = data["segments"]
    return [s["score"] for s in segments], [s["label"] for s in segments]


def accuracy(scores, labels, cut):
    """用切点给这批段分类，返回与真值一致的比例。"""
    effective = FALLBACK_THRESHOLD if cut is None else cut
    hit = sum((score >= effective) == (label == "me")
              for score, label in zip(scores, labels))
    return hit / len(scores)


def evaluate(scores, labels):
    rows = []
    for n in range(MIN_ADAPTIVE_SEGMENTS, len(scores) + 1):
        cut = find_adaptive_cut(scores[:n])
        rows.append((n, cut, accuracy(scores[:n], labels[:n], cut)))
    return rows


def main():
    scores, labels = load_fixture()
    me = [s for s, l in zip(scores, labels) if l == "me"]
    other = [s for s, l in zip(scores, labels) if l == "other"]
    print(f"基准：{len(scores)} 段　"
          f"me {len(me)} 段 {min(me):.3f}~{max(me):.3f}　"
          f"对方 {len(other)} 段 {min(other):.3f}~{max(other):.3f}")
    print(f"　　　能把两类完全分开的切点区间：({max(other):.3f}, {min(me):.3f}]")

    rows = evaluate(scores, labels)
    accs = [a for _, _, a in rows]
    full, mean, worst = accs[-1], sum(accs) / len(accs), min(accs)

    print("\n前缀   切点     准确率")
    for n, cut, acc in rows:
        # 只打印首尾与判错较多的行，避免刷屏
        if n <= MIN_ADAPTIVE_SEGMENTS + 2 or n >= len(scores) - 2 or acc < 0.95:
            shown = "None" if cut is None else f"{cut:.3f}"
            print(f"{n:4d}   {shown:>6s}   {acc * 100:5.1f}%"
                  + ("" if acc >= 0.95 else "   ← 判错较多"))

    print(f"\n全长 {full * 100:.1f}%　各前缀平均 {mean * 100:.1f}%　"
          f"最差前缀 {worst * 100:.1f}%")

    failures = []
    if full < TARGET_FULL:
        failures.append(f"全长准确率 {full * 100:.1f}% < {TARGET_FULL * 100:.0f}%")
    if mean < TARGET_MEAN:
        failures.append(f"前缀平均 {mean * 100:.1f}% < {TARGET_MEAN * 100:.0f}%")
    if worst < TARGET_WORST:
        failures.append(f"最差前缀 {worst * 100:.1f}% < {TARGET_WORST * 100:.0f}%")

    # 护栏：样本不足时必须拒绝判断，而不是硬切
    if find_adaptive_cut(scores[:MIN_ADAPTIVE_SEGMENTS - 1]) is not None:
        failures.append("段数不足 MIN_ADAPTIVE_SEGMENTS 时应返回 None")
    # 护栏：全场只有一个人（分数都很接近）时不该硬分成两拨
    if find_adaptive_cut([0.70, 0.71, 0.69, 0.72, 0.70, 0.71, 0.68]) is not None:
        failures.append("分数高度集中（只有一个人）时应返回 None")
    # 护栏：整体分数都很低（本场没有「我」在说话）时不该强行切
    if find_adaptive_cut([0.20, 0.25, 0.18, 0.30, 0.22, 0.27, 0.19]) is not None:
        failures.append("最高分低于 ADAPTIVE_FLOOR 时应返回 None")

    if failures:
        print("\n❌ 未达标：")
        for item in failures:
            print(f"  · {item}")
        sys.exit(1)
    print("\n✅ 全部达标")


if __name__ == "__main__":
    main()
