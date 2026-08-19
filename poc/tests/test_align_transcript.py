"""会后分离回写转写的回归测试（无需 pytest / 联网 / 模型）：

    python -m tests.test_align_transcript

## 为什么需要它

`diarize_offline.align_transcript()` 是纯逻辑——给定语音段和转写就能算，
不碰模型也不碰网络，却是**用户档案的最终形态**：它决定每句话归谁、
一条过长的 final 从哪里切开。2026-07-27 一次性改了三处
（时间偏移估计、窗口裁剪、按说话人切分），当时一行测试都没有。

这里覆盖的都是**已经踩过或差点踩到的坑**，不是为了凑覆盖率：
  - 切分不能丢字、不能重排（用户档案不许被改写）
  - 切点必须落在标点后（否则界面上出现半句，见 HANDOFF §4.6 坑 1）
  - 时间偏移不能被"最后一条 final"带偏（停止录音会 flush 出一条晚到的，见坑 3）
  - 重跑要收敛（用户可能连点两次「会后分离」）
"""

from __future__ import annotations

import json
import os
import sys

from diarize_offline import align_transcript
from turn_split import STRONG_PUNCT, WEAK_PUNCT

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)
FIXTURE = os.path.join(PROJECT_ROOT, "eval", "align_transcript_fixture.json")

FAILED = []


def check(name, condition, detail=""):
    if condition:
        print(f"  ✅ {name}")
    else:
        print(f"  ❌ {name}   {detail}")
        FAILED.append(name)


def load():
    with open(FIXTURE, encoding="utf-8") as f:
        data = json.load(f)
    result = {
        "ok": True,
        "durationSec": data["durationSec"],
        "segments": data["segments"],
        "speakers": data["speakers"],
    }
    return result, data["transcript"], float(data["startedAt"])


def finals(items):
    return [i for i in items if i.get("isFinal", True)]


def test_lossless():
    print("切分不丢字、不改字、不重排")
    result, transcript, started = load()
    out = align_transcript(result, [dict(t) for t in transcript], started)
    before = "".join(t["text"].strip() for t in finals(transcript))
    after = "".join(t["text"].strip() for t in finals(out))
    check("文本完全一致", before == after,
          f"{len(before)} 字 vs {len(after)} 字")
    check("条数只增不减", len(finals(out)) >= len(finals(transcript)),
          f"{len(finals(transcript))} → {len(finals(out))}")
    check("确实切开了长条", len(finals(out)) > len(finals(transcript)),
          "一条都没切开，说明切分逻辑没生效")


def test_ids_and_order():
    print("id 唯一、时间严格递增")
    result, transcript, started = load()
    out = align_transcript(result, [dict(t) for t in transcript], started)
    ids = [t["id"] for t in out]
    check("id 唯一", len(set(ids)) == len(ids),
          f"{len(ids) - len(set(ids))} 个重复")
    ats = [t["at"] for t in finals(out)]
    check("at 严格递增", all(b > a for a, b in zip(ats, ats[1:])),
          "排序按 at，相等或倒序会让界面上的段落乱序")
    check("每条都有说话人", all(t.get("speakerId") for t in finals(out)))
    timed = [
        t
        for t in finals(out)
        if t.get("audioStartMs") is not None and t.get("audioEndMs") is not None
    ]
    check(
        "每条都有录音轴起止时间",
        len(timed) == len(finals(out)),
        f"{len(timed)} / {len(finals(out))}",
    )
    check(
        "录音轴区间均有效",
        all(
            0 <= float(t["audioStartMs"]) < float(t["audioEndMs"])
            <= float(result["durationSec"]) * 1000 + 1
            for t in timed
        ),
    )
    known = {s["id"] for s in result["speakers"]}
    unknown = {t["speakerId"] for t in finals(out)} - known
    # ⚠️ 曾经在对不上时返回过 "other" —— 那个 id 不在 speakers 表里，
    #    界面上既改不了名也归不了类
    check("说话人都在 speakers 表里", not unknown, f"多出来的：{unknown}")


def test_cut_at_punctuation():
    print("切点落在标点后面（不切半句）")
    result, transcript, started = load()
    out = align_transcript(result, [dict(t) for t in transcript], started)
    by_base = {}
    for item in finals(out):
        base = str(item["id"]).split("#p")[0]
        by_base.setdefault(base, []).append(item["text"])
    bad = []
    for base, pieces in by_base.items():
        if len(pieces) < 2:
            continue
        for piece in pieces[:-1]:          # 最后一片的结尾是原文结尾，不算切点
            if piece and piece.rstrip()[-1] not in STRONG_PUNCT + WEAK_PUNCT:
                bad.append(piece[-12:])
    check("没有从词中间切开", not bad, f"{len(bad)} 处，例如 {bad[:3]}")


def test_no_orphan_head():
    """切出来的第一片不能是"那""做实"这种一两个字的碎片。

    ⚠️ 这是窗口首尾"渗漏"的症状：时间窗的起点来自 ASR 送达时刻（带延迟），
       不会正好落在语音间隙上，上一条 final 的尾音会渗进这一条的窗口，
       让它在开头分走一两个字。实测出现过「那」「做实」「亲子」「对对，」。
       只查文本无损和标点位置**抓不到它**——碎片也是标点结尾、也不丢字。
    """
    print("切出来的第一片不是孤儿碎片")
    result, transcript, started = load()
    out = align_transcript(result, [dict(t) for t in transcript], started)
    heads = {}
    for item in finals(out):
        base = str(item["id"]).split("#p")[0]
        heads.setdefault(base, []).append(item)
    orphans = [
        pieces[0]["text"]
        for pieces in heads.values()
        if len(pieces) > 1 and len(pieces[0]["text"].strip()) < 4
    ]
    check("没有一两个字的开头碎片", not orphans, f"{len(orphans)} 处：{orphans[:5]}")


def test_idempotent():
    print("重跑收敛（用户可能连点两次「会后分离」）")
    result, transcript, started = load()
    once = align_transcript(result, [dict(t) for t in transcript], started)
    result2, _, _ = load()
    twice = align_transcript(result2, [dict(t) for t in once], started)
    check("第二次基本不再切",
          len(finals(twice)) - len(finals(once)) <= 1,
          f"{len(finals(once))} → {len(finals(twice))}")
    check("第二次文本仍无损",
          "".join(t["text"].strip() for t in finals(once))
          == "".join(t["text"].strip() for t in finals(twice)))
    check("id 仍唯一",
          len({t["id"] for t in twice}) == len(twice))


def test_offset_robust_to_late_tail():
    print("时间偏移不被「最后一条晚到的 final」带偏")
    result, transcript, started = load()
    normal = align_transcript(result, [dict(t) for t in transcript], started)

    # 模拟停止录音时 flush 出来的那一条：文本很短，送达时刻远晚于任何语音段
    result2, transcript2, _ = load()
    tail = dict(transcript2[-1])
    tail["id"] = "flushed-tail"
    tail["at"] = transcript2[-1]["at"] + 12_000     # 晚 12 秒
    tail["text"] = "好的。"
    skewed = align_transcript(result2, [dict(t) for t in transcript2] + [tail], started)

    # 除了新增的尾巴，其余段落的归属不应该被这一条改变
    def labels(items):
        return [(t["text"].strip(), t["speakerId"]) for t in finals(items)
                if not str(t["id"]).startswith("flushed-tail")]

    same = labels(normal) == labels(skewed)
    check("加一条晚到的尾巴后，其余归属不变", same,
          "偏移估计被单条带偏了 —— 这正是改用中位数要解决的问题")


def test_inherit_precise_realtime_ranges():
    """已有 PCM 采样钟区间不能被会后全局偏移重算。"""
    result = {
        "ok": True,
        "durationSec": 2.0,
        "segments": [
            {"start": 0.0, "end": 1.0, "speakerId": "spk1"},
            {"start": 1.0, "end": 2.0, "speakerId": "spk2"},
        ],
        "speakers": [
            {"id": "spk1", "name": "说话人1"},
            {"id": "spk2", "name": "说话人2"},
        ],
    }
    started = 1_000_000
    transcript = [
        {
            "id": "precise-a",
            "speakerId": "old",
            "speaker": "旧标签",
            "text": "第一句。",
            "isFinal": True,
            # 故意把墙上 at 放到很晚；音频区间才是事实。
            "at": started + 30_000,
            "audioStartMs": 250,
            "audioEndMs": 850,
        },
        {
            "id": "precise-b",
            "speakerId": "old",
            "speaker": "旧标签",
            "text": "第二句。",
            "isFinal": True,
            "at": started + 31_000,
            "audioStartMs": 1_100,
            "audioEndMs": 1_800,
        },
    ]
    out = align_transcript(result, [dict(item) for item in transcript], started)
    assert [(item["audioStartMs"], item["audioEndMs"]) for item in out] == [
        (250, 850),
        (1100, 1800),
    ]
    assert all(item["speakerId"] in {"spk1", "spk2"} for item in out)


if __name__ == "__main__":
    if not os.path.isfile(FIXTURE):
        sys.exit(f"缺少基准文件：{FIXTURE}")
    for fn in (
        test_lossless,
        test_ids_and_order,
        test_cut_at_punctuation,
        test_no_orphan_head,
        test_idempotent,
        test_offset_robust_to_late_tail,
        test_inherit_precise_realtime_ranges,
    ):
        fn()
    print()
    if FAILED:
        raise SystemExit(f"{len(FAILED)} 项失败：{FAILED}")
    print("全部通过")
