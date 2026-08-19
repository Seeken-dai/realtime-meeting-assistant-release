"""turn_split 的自检（无需 pytest）：python -m tests.test_turn_split

覆盖的都是真机上出过问题的情况，不是为了凑覆盖率：
  - 一条 final 跨两个说话人 → 必须切开
  - 附近没有标点 → 宁可不切，也不能切在词中间
  - 首尾渗进来的短碎片 → 不能挤出「那」「做实」这种孤儿段
"""

from turn_split import Span, group_spans, split_text_by_spans

FAILED = []


def check(name, condition, detail=""):
    if condition:
        print(f"  ✅ {name}")
    else:
        print(f"  ❌ {name} {detail}")
        FAILED.append(name)


def test_two_speakers():
    print("跨说话人的长句按边界切开")
    text = "这个厉害，可以实时转写的。你那个不能实时转写。我那个是录完之后统一转。"
    spans = [
        Span(0.0, 4.0, "other"),
        Span(4.2, 8.0, "me"),
        Span(8.4, 14.0, "other"),
    ]
    chunks = split_text_by_spans(text, spans)
    check("切成 3 段", len(chunks) == 3, [c.text for c in chunks])
    check(
        "说话人依次是 other/me/other",
        [c.label for c in chunks] == ["other", "me", "other"],
        [c.label for c in chunks],
    )
    check("文本无损", "".join(c.text for c in chunks) == text)
    check(
        "都切在标点后面",
        all(c.text[-1] in "。！？，" for c in chunks[:-1]),
        [c.text for c in chunks],
    )


def test_no_punctuation_no_cut():
    print("附近没有标点就不切（不能切在词中间）")
    text = "还有的上就是我现在的是我现在自己做那个是呃那这种的话大概可以区分出具体谁谁谁"
    spans = [Span(0.0, 6.0, "other"), Span(6.1, 12.0, "me")]
    chunks = split_text_by_spans(text, spans)
    check("整条不切", len(chunks) == 1, [c.text for c in chunks])
    check("归给说话更久的一方", chunks[0].label in ("me", "other"))
    check("文本无损", chunks[0].text == text)


def test_same_speaker_pause_groups():
    print("同一个人中间停久了要分段")
    spans = [Span(0.0, 5.0, "me"), Span(9.0, 14.0, "me")]
    check("停顿 4s → 两组", len(group_spans(spans)) == 2)
    check(
        "停顿 0.5s → 一组",
        len(group_spans([Span(0.0, 5.0, "me"), Span(5.5, 9.0, "me")])) == 1,
    )


def test_tiny_fragment_merged():
    print("同说话人的碎片并回相邻段，不同说话人的短插话保留")
    text = "对。我这边先说一下背景，然后我们再看方案。"
    spans = [Span(0.0, 0.6, "me"), Span(0.7, 8.0, "me")]
    chunks = split_text_by_spans(text, spans)
    check("同一个人不产生碎片", len(chunks) == 1, [c.text for c in chunks])

    text2 = "嗯。那我们下周再对一次这个方案的细节吧。"
    spans2 = [Span(0.0, 1.2, "other"), Span(1.4, 8.0, "me")]
    chunks2 = split_text_by_spans(text2, spans2)
    check("不同人的短插话保留", len(chunks2) == 2, [c.text for c in chunks2])
    check("文本无损", "".join(c.text for c in chunks2) == text2)


def test_long_monologue_split():
    print("一个人说太久也要分段（可读 / 可改派）")
    text = "。".join([f"这是第{i}句话，内容大概是这样的" for i in range(12)]) + "。"
    spans = [Span(0.0, 90.0, "me")]
    chunks = split_text_by_spans(text, spans)
    check("被分成多段", len(chunks) > 1, len(chunks))
    check("每段都不超上限", all(len(c.text) <= 160 for c in chunks))
    check("说话人不变", all(c.label == "me" for c in chunks))
    check("文本无损", "".join(c.text for c in chunks) == text)


def test_no_spans():
    print("没有语音段时原样返回（宁可不切）")
    chunks = split_text_by_spans("随便一句话。", [])
    check("单段", len(chunks) == 1)
    check("没有说话人", chunks[0].label is None)


def test_word_timestamps():
    print("有词级时间戳时按时间切")
    text = "你好呀。我知道了。"
    # 前 4 个字在 0-2s，后面在 3-6s
    char_times = [0.5, 1.0, 1.5, 2.0, 3.5, 4.0, 4.5, 5.0, 6.0]
    spans = [Span(0.0, 2.2, "other"), Span(3.0, 6.0, "me")]
    chunks = split_text_by_spans(text, spans, char_times=char_times)
    check("切成 2 段", len(chunks) == 2, [c.text for c in chunks])
    check("按时间切在句号后", chunks[0].text == "你好呀。", chunks[0].text)


if __name__ == "__main__":
    for fn in (
        test_two_speakers,
        test_no_punctuation_no_cut,
        test_same_speaker_pause_groups,
        test_tiny_fragment_merged,
        test_long_monologue_split,
        test_no_spans,
        test_word_timestamps,
    ):
        fn()
    print()
    if FAILED:
        raise SystemExit(f"{len(FAILED)} 项失败：{FAILED}")
    print("全部通过")
