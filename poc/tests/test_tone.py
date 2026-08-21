"""
测试话术风格（Tone & Persona）配置及 System Prompt 拼装。
"""

import sys
from pathlib import Path

# 把 poc 加入 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from suggest import (
    TONE_CONFIGS,
    normalize_tone,
    tone_config,
    build_suggestion_system_prompt,
    build_answer_system_prompt,
)


def test_tone_normalization():
    assert normalize_tone("direct") == "direct"
    assert normalize_tone("BUSINESS") == "business"
    assert normalize_tone("challenger") == "challenger"
    assert normalize_tone("collaborative") == "collaborative"
    assert normalize_tone("custom") == "custom"
    # 非法输入回退到 direct
    assert normalize_tone("invalid_tone") == "direct"
    assert normalize_tone(None) == "direct"
    assert normalize_tone("") == "direct"
    print("[PASS] test_tone_normalization")


def test_suggestion_system_prompt_direct():
    prompt = build_suggestion_system_prompt(
        me_name="小李",
        scene="requirements",
        tone="direct",
        custom_tone_prompt="",
        count=3,
    )
    assert "直率务实（产研内推）" in prompt
    assert "严禁一切公关辞令" in prompt
    assert "禁止输出“您方便详细说说”" in prompt
    assert "【本场会议场景：需求评审】" in prompt
    assert "铁律一：区分【事实】与【策略】，前者绝不编造" in prompt
    print("[PASS] test_suggestion_system_prompt_direct")


def test_suggestion_system_prompt_business():
    prompt = build_suggestion_system_prompt(
        me_name="张总",
        scene="sales",
        tone="business",
        custom_tone_prompt="",
        count=3,
    )
    assert "商务稳健（对外客户）" in prompt
    assert "得体专业、温和客气、严谨稳健" in prompt
    assert "【本场会议场景：售前沟通】" in prompt
    print("[PASS] test_suggestion_system_prompt_business")


def test_suggestion_system_prompt_custom():
    custom_instruction = "我是架构师，重点考察系统扩展性与高可用方案，说话精炼直接。"
    prompt = build_suggestion_system_prompt(
        me_name="老王",
        scene="general",
        tone="custom",
        custom_tone_prompt=custom_instruction,
        count=2,
    )
    assert "【用户自定义风格补充要求】" in prompt
    assert custom_instruction in prompt
    assert "铁律一：区分【事实】与【策略】，前者绝不编造" in prompt
    print("[PASS] test_suggestion_system_prompt_custom")


def test_answer_system_prompt():
    ans_direct = build_answer_system_prompt(me_name="小李", tone="direct")
    assert "站在「小李」这一方" in ans_direct
    assert "直率务实（产研内推）" in ans_direct

    ans_custom = build_answer_system_prompt(
        me_name="小李",
        tone="custom",
        custom_tone_prompt="用两句话回答，强调数据库索引",
    )
    assert "强调数据库索引" in ans_custom
    print("[PASS] test_answer_system_prompt")


if __name__ == "__main__":
    test_tone_normalization()
    test_suggestion_system_prompt_direct()
    test_suggestion_system_prompt_business()
    test_suggestion_system_prompt_custom()
    test_answer_system_prompt()
    print("\n所有话术风格（Tone）测试全部通过！")
