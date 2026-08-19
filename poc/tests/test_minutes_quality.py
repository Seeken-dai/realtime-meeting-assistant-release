"""Pure structure checks for generated minutes post-processing."""

from generate_minutes import (
    FINAL_SYSTEM,
    PART_SYSTEM,
    build_evidence_catalog,
    build_final_prompt,
    ensure_minutes_sections,
    evidence_marker_stats,
    normalize_evidence_markers,
)


def main() -> None:
    content = ensure_minutes_sections("## 一句话摘要\n- 讨论需求边界")
    assert content.startswith("# 会议纪要")
    assert "## 一句话摘要" in content
    assert "## 已确认结论" in content
    assert "## 待办事项" in content
    assert "## 需求与约束" in content
    assert "## 风险与待确认" in content
    assert "待确认" in content
    assert "```" not in content
    ret_content = ensure_minutes_sections(
        "会议纪要<ret>## 一句话摘要<ret>本次会议已确认范围。"
        "<ret><ret>## 已确认结论<ret>1. 采用方案 A。"
    )
    assert ret_content.startswith("# 会议纪要\n## 一句话摘要")
    assert "<ret>" not in ret_content
    assert "## 已确认结论\n1. 采用方案 A。" in ret_content
    normalized = normalize_evidence_markers(
        "结论 [证据 id=live-48 t=待确认]，其他 [证据 id=missing t=00:01]",
        [
            {
                "id": "live-48",
                "at": 1_012_000,
                "text": "已确认的事实",
                "isFinal": True,
            }
        ],
        1_000_000,
    )
    assert "[证据 id=live-48 t=00:12]" in normalized
    assert "[证据 id=missing" not in normalized
    assert normalized.endswith("[证据 待确认]")
    assert evidence_marker_stats(normalized) == {
        "evidenceMarkerCount": 1,
        "pendingEvidenceCount": 1,
    }

    evidence_lines = [
        "[证据 id=line-1 t=00:01] [我] 确认采用方案",
        "[证据 id=line-2 t=00:02] [对方] 下周提交接口说明",
    ]
    assert build_evidence_catalog(evidence_lines) == "\n".join(evidence_lines)
    final_prompt = build_final_prompt(
        "长会",
        "- 暂无已确认记忆",
        "【部分 1】事实摘要",
        "\n\n【完整证据目录】\n" + evidence_lines[0],
    )
    assert "[证据 id=line-1 t=00:01]" in final_prompt
    assert "原样复制" in PART_SYSTEM
    assert "完整证据目录" in FINAL_SYSTEM
    print("ok: minutes structure and safe missing sections")


if __name__ == "__main__":
    main()
