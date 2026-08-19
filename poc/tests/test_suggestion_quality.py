"""话术长度控制与质量评分的无网络回归测试。"""

from suggest import (
    SUGGESTION_SCRIPT_MAX_CHARS,
    _apply_length_limits,
    _compact_script,
    SAFE_CLARIFY_SCRIPT,
    _validate,
)
from suggestion_benchmark import CASES, evaluate_result, summarize


def test_compact_script():
    short = "我先确认一下具体边界，再给您准确答复。"
    assert _compact_script(short) == (short, False, len(short))

    long = "先确认审批节点和金额条件；" + "再确认例外流程以及联调范围，" * 8
    compacted, shortened, original_length = _compact_script(long)
    assert shortened is True
    assert original_length > SUGGESTION_SCRIPT_MAX_CHARS
    assert len(compacted) <= SUGGESTION_SCRIPT_MAX_CHARS
    assert compacted.endswith("…")

    limited = _apply_length_limits([{"script": long, "intent": "确认边界"}])
    assert limited[0]["_shortened"] is True
    assert limited[0]["_original_script_length"] == original_length

    placeholder = _apply_length_limits(
        [{"script": "我们会在（负责人）前完成 XX 模块。", "intent": "交付"}]
    )
    assert placeholder[0]["script"] == SAFE_CLARIFY_SCRIPT
    assert placeholder[0]["type"] == "clarify"
    assert placeholder[0]["references"] == []


def test_quality_evaluator():
    case = CASES[3]  # 知识库无依据能力
    safe = {
        "suggestions": [
            {
                "intent": "先确认审计要求",
                "script": "这个能力我先核实，您方便说明存证范围和验收标准吗？",
                "type": "clarify",
                "references": [],
            },
            {
                "intent": "拆清具体场景",
                "script": "咱们先确认哪些审批记录需要上链，以及是否有指定平台。",
                "type": "advisory",
                "references": [],
            },
        ],
        "hits": [],
    }
    good = evaluate_result(case, safe)
    assert good["passed"] is True

    unsafe = {
        "suggestions": [
            {
                "intent": "直接承诺",
                "script": "我们系统已经支持区块链存证，可以实现审批记录全部上链。",
                "type": "grounded",
                "references": ["不存在.md"],
            }
        ],
        "hits": [],
    }
    bad = evaluate_result(case, unsafe)
    assert bad["passed"] is False
    assert any(
        not check["passed"] and check["name"] == "未命中场景红线"
        for check in bad["checks"]
    )

    sensitive = {
        **safe,
        "suggestions": [
            {
                **safe["suggestions"][0],
                "_sensitive": "内部成本数据：15-25人天",
            },
            safe["suggestions"][1],
        ],
    }
    sensitive_result = evaluate_result(case, sensitive)
    assert sensitive_result["passed"] is False
    assert any(
        not check["passed"] and check["name"] == "可直接说出口"
        for check in sensitive_result["checks"]
    )


def test_summary():
    records = [
        {
            "caseId": "a",
            "caseName": "A",
            "elapsedSeconds": 1.0,
            "evaluation": {"passed": True, "score": 100, "scriptLengths": [40]},
        },
        {
            "caseId": "a",
            "caseName": "A",
            "elapsedSeconds": 2.0,
            "evaluation": {"passed": False, "score": 80, "scriptLengths": [80]},
        },
    ]
    result = summarize(records)
    assert result["passRate"] == 0.5
    assert result["averageScore"] == 90
    assert result["scriptLengthMedian"] == 60
    assert result["latencyP95Seconds"] == 2
    assert result["latencyMaxSeconds"] == 2


def test_grounded_capability_needs_domain_evidence():
    """grounded 若断言能力，evidence 必须覆盖领域词，不能用无关原文充数。"""
    bad = _validate(
        [
            {
                "intent": "承诺区块链",
                "script": "我们系统支持区块链存证，审批记录可以全部上链。",
                "type": "grounded",
                "references": ["产品功能清单.md"],
                "evidence": [
                    {
                        "source": "产品功能清单.md",
                        "quote": "标准版支持三级审批，固定节点不可按条件任意增减。",
                    }
                ],
            }
        ],
        [
            {
                "source": "产品功能清单.md",
                "text": "标准版支持三级审批，固定节点不可按条件任意增减。",
            }
        ],
    )
    assert bad[0]["type"] == "clarify"
    assert bad[0].get("_downgraded")

    good = _validate(
        [
            {
                "intent": "说明接口",
                "script": "我们支持 REST 接口，订单状态可用 Webhook 主动通知。",
                "type": "grounded",
                "references": ["产品功能清单.md"],
                "evidence": [
                    {
                        "source": "产品功能清单.md",
                        "quote": "开放 REST API，并支持订单状态变更的 Webhook 回调。",
                    }
                ],
            }
        ],
        [
            {
                "source": "产品功能清单.md",
                "text": "开放 REST API，并支持订单状态变更的 Webhook 回调。",
            }
        ],
    )
    assert good[0]["type"] == "grounded"
    assert not good[0].get("_downgraded")


def test_assert_then_ask_still_blocked():
    """先断言能力再补一句「是否」，不能靠是否二字逃过校验。"""
    out = _validate(
        [
            {
                "intent": "否定区块链",
                "script": "目前系统暂未集成区块链存证功能，您是否需要我们评估该需求？",
                "type": "clarify",
                "references": [],
            }
        ],
        [],
    )
    assert out[0]["type"] == "clarify"
    assert out[0].get("_downgraded")
    assert "暂未集成" not in out[0]["script"]


def test_recover_evidence_from_hits():
    """模型漏写 quote 时，若 hit 原文能撑住话术，应自动回填 evidence。"""
    out = _validate(
        [
            {
                "intent": "说明标准审批",
                "script": "标准版支持固定三级审批流，不支持按条件任意加节点。",
                "type": "grounded",
                "references": ["产品功能清单.md"],
                "evidence": [],
            }
        ],
        [
            {
                "source": "产品功能清单.md",
                "text": "标准版支持固定三级审批流：发起人 → 部门主管 → 分管副总。不支持自定义审批节点数量、条件分支审批。",
            }
        ],
    )
    assert out[0]["type"] == "grounded"
    assert out[0].get("evidence")
    assert out[0].get("_recovered_evidence")


def test_commitment_word_context():
    safe = _validate(
        [
            {
                "intent": "确认范围",
                "script": "为了保证给领导的数字准确，咱们先把需求边界定下来。",
                "type": "advisory",
                "references": [],
            }
        ],
        [],
    )
    assert "_sensitive" not in safe[0]

    risky = _validate(
        [
            {
                "intent": "越界承诺",
                "script": "我们会出具方案，确保满足审计合规。",
                "type": "advisory",
                "references": [],
            }
        ],
        [],
    )
    assert "承诺性表述" in risky[0]["_sensitive"]


def test_grounded_evidence_validation():
    hits = [
        {
            "source": "产品能力.md",
            "text": "- **标准版支持固定三级审批流。** 如需更多节点应先评估。",
        }
    ]
    verified = _validate(
        [
            {
                "intent": "说明标准能力",
                "script": "标准版支持固定三级审批流，更多节点需要另行评估。",
                "type": "grounded",
                "references": ["《产品能力.md》"],
                "evidence": [
                    {
                        "source": "产品能力.md",
                        "quote": "标准版支持固定三级审批流。",
                    }
                ],
            }
        ],
        hits,
    )[0]
    assert verified["type"] == "grounded"
    assert verified["references"] == ["产品能力.md"]
    assert verified["evidence"] == [
        {
            "source": "产品能力.md",
            "quote": "标准版支持固定三级审批流。",
        }
    ]

    fabricated = _validate(
        [
            {
                "intent": "编造能力",
                "script": "标准版支持区块链存证。",
                "type": "grounded",
                "references": ["产品能力.md"],
                "evidence": [
                    {
                        "source": "产品能力.md",
                        "quote": "标准版支持区块链存证。",
                    }
                ],
            }
        ],
        hits,
    )[0]
    assert fabricated["type"] == "clarify"
    assert fabricated["references"] == []
    assert fabricated["evidence"] == []
    assert "再核实" in fabricated["script"]
    assert "区块链存证" not in fabricated["script"]
    assert "原文" in fabricated["_downgraded"]

    negative_claim = _validate(
        [
            {
                "intent": "直接否定能力",
                "script": "我们目前系统不支持区块链存证功能。",
                "type": "clarify",
                "references": [],
                "evidence": [],
            }
        ],
        hits,
    )[0]
    assert negative_claim["type"] == "clarify"
    assert "不支持区块链" not in negative_claim["script"]
    assert "再核实" in negative_claim["script"]
    assert "支持或不支持" in negative_claim["_downgraded"]

    unintegrated_claim = _validate(
        [
            {
                "intent": "换一种措辞否定能力",
                "script": "目前我们的系统暂未集成区块链存证功能。",
                "type": "clarify",
                "references": [],
                "evidence": [],
            }
        ],
        hits,
    )[0]
    assert "暂未集成" not in unintegrated_claim["script"]
    assert "再核实" in unintegrated_claim["script"]

    safe_question = _validate(
        [
            {
                "intent": "确认能力要求",
                "script": "我需要先核实系统是否支持这一能力，您方便说明验收要求吗？",
                "type": "clarify",
                "references": [],
                "evidence": [],
            }
        ],
        hits,
    )[0]
    assert safe_question["script"].startswith("我需要先核实")


if __name__ == "__main__":
    test_compact_script()
    test_quality_evaluator()
    test_summary()
    test_commitment_word_context()
    test_grounded_evidence_validation()
    print("ok: suggestion quality + grounded evidence validation")
