"""会后转写整理的安全性回归测试（无需联网或模型）。"""

from clean_transcript import (
    apply_cleaned_items,
    chunk_transcript,
    parse_cleaned_items,
)


def test_chunk_keeps_items_and_order():
    items = [
        {"id": "a", "at": 1000, "speaker": "我", "text": "第一段"},
        {"id": "b", "at": 2000, "speaker": "对方", "text": "第二段"},
        {"id": "empty", "at": 3000, "speaker": "对方", "text": ""},
        {"id": "c", "at": 4000, "speaker": "我", "text": "第三段"},
    ]
    chunks = chunk_transcript(items, 0, max_chars=45)
    assert [item["id"] for chunk in chunks for item in chunk] == ["a", "b", "c"]


def test_parse_accepts_json_fence_but_requires_exact_rows():
    source = [
        {"id": "a", "text": "这个是初稿"},
        {"id": "b", "text": "我们下周验证"},
    ]
    parsed = parse_cleaned_items(
        '```json\n[{"id":"a","text":"这是初稿。"},{"id":"b","text":"我们下周验证。"}]\n```',
        source,
    )
    assert parsed == [
        {"id": "a", "text": "这是初稿。"},
        {"id": "b", "text": "我们下周验证。"},
    ]

    for invalid in (
        '[{"id":"a","text":"只返回一行"}]',
        '[{"id":"b","text":"顺序错了"},{"id":"a","text":"另一行"}]',
        '[{"id":"a","text":"未知内容"},{"id":"x","text":"越界"}]',
    ):
        try:
            parse_cleaned_items(invalid, source)
        except ValueError:
            pass
        else:
            raise AssertionError("不安全的模型输出没有被拒绝")


def test_parse_rejects_semantic_drift():
    source = [{"id": "a", "text": "818 之后再看 K 跟 K。"}]
    for invalid in (
        '[{"id":"a","text":"818 之后再看 820。"}]',
        '[{"id":"a","text":"818 之后再看 K 跟 K..."}]',
        '[{"id":"a","text":"818 之后再看 MK 跟 EKP。"}]',
    ):
        try:
            parse_cleaned_items(invalid, source, trusted_terms=["EKP"])
        except ValueError:
            pass
        else:
            raise AssertionError("语义漂移没有被拒绝")

    parsed = parse_cleaned_items(
        '[{"id":"a","text":"818 之后再看 EKP。"}]',
        source,
        trusted_terms=["EKP"],
    )
    assert parsed[0]["text"] == "818 之后再看 EKP。"


def test_apply_only_changes_text_and_preserves_non_final_rows():
    source = [
        {"id": "a", "text": "原始文字", "isFinal": True, "speakerId": "spk1"},
        {"id": "interim", "text": "临时内容", "isFinal": False},
    ]
    output = apply_cleaned_items(source, [{"id": "a", "text": "整理后的文字。"}])
    assert output == [
        {"id": "a", "text": "整理后的文字。", "isFinal": True, "speakerId": "spk1"},
        {"id": "interim", "text": "临时内容", "isFinal": False},
    ]


if __name__ == "__main__":
    test_chunk_keeps_items_and_order()
    test_parse_accepts_json_fence_but_requires_exact_rows()
    test_parse_rejects_semantic_drift()
    test_apply_only_changes_text_and_preserves_non_final_rows()
    print("ok: transcript cleanup preserves ids, order, and untouched rows")
