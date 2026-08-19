"""可重复的话术建议质量基准。

与 ``run_suggest.py`` 的人工阅读不同，本脚本会：
1. 对同一组场景重复运行，观察模型随机性；
2. 用确定性规则检查结构、依据、风险、长度和场景关键点；
3. 把每轮原始结果与得分保存成 JSON，便于横向比较模型。

示例：
    python suggestion_benchmark.py --runs 3
    python suggestion_benchmark.py --provider gemini --model gemini-3.5-flash-lite
    python suggestion_benchmark.py --case 4 --runs 5
"""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

import providers
from suggest import SUGGESTION_SCRIPT_MAX_CHARS


ME = "我"
ROOT = Path(__file__).resolve().parent


CASES = [
    {
        "id": "custom-boundary",
        "name": "定制边界",
        "transcript": [
            {"speaker": ME, "text": "这次主要确认审批需求。"},
            {
                "speaker": "客户",
                "text": "能不能支持自定义审批流？就是按金额分几档走不同的人。",
            },
        ],
        "expect_any": ["定制", "分支", "场景", "边界"],
        "expect_reference_any": ["产品功能清单.md", "需求边界与报价规则.md"],
    },
    {
        "id": "free-integration",
        "name": "免费对接压力",
        "transcript": [
            {
                "speaker": "客户",
                "text": "OA 对接就是调个接口，你们顺便免费做了吧，别单独收费。",
            },
        ],
        "expect_any": ["定制", "评估", "范围", "接口", "联调", "商务"],
        "forbid_patterns": [
            r"(?<!不)(?:可以|同意|答应|我们会|我方会).{0,6}免费.{0,6}(?:做|提供|交付|实施)",
            r"(?<!不)(?:我们|我方).{0,4}能.{0,3}免费.{0,6}(?:做|提供|交付|实施)",
            r"免费.{0,6}(?:没有问题|没问题|可以安排)",
        ],
    },
    {
        "id": "price-deadline",
        "name": "追问工期报价",
        "transcript": [
            {
                "speaker": "客户",
                "text": "这些需求要做多久、多少钱？我们下周上会，现在给个准数。",
            },
        ],
        "expect_any": ["评估", "正式", "报价", "确认", "需求"],
        "forbid_patterns": [r"\d+(?:\.\d+)?\s*(元|万元|人天|折)"],
    },
    {
        "id": "unsupported-blockchain",
        "name": "知识库无依据能力",
        "transcript": [
            {
                "speaker": "客户",
                "text": "你们系统支持区块链存证吗？审计要求审批记录全部上链。",
            },
        ],
        "expect_any": ["确认", "审计", "要求", "场景", "存证", "上链"],
        "forbid_grounded": True,
        "forbid_patterns": [
            r"(我们|系统|产品).{0,8}(支持|具备|可以实现).{0,8}(区块链|上链)",
            r"(区块链|上链).{0,8}(已经支持|可以实现|没有问题)",
            # 断言式归属才拦；「是否要评估定制」类追问不拦
            r"(?<![是否要需评估会])(属于|归入).{0,4}定制开发",
            r"定制开发范畴",
        ],
    },
    {
        "id": "known-api",
        "name": "已有 API 能力",
        "transcript": [
            {
                "speaker": "客户",
                "text": "你们有开放接口吗？订单状态变化能不能主动通知我们的系统？",
            },
        ],
        "expect_any": ["REST", "Webhook", "接口", "事件"],
        "expect_reference_any": ["产品功能清单.md"],
    },
    {
        "id": "known-approval-limit",
        "name": "标准审批能力边界",
        "transcript": [
            {
                "speaker": "客户",
                "text": "标准版审批到底支持什么？能按条件增加任意节点吗？",
            },
        ],
        "expect_any": ["三级", "固定", "条件分支", "定制", "节点"],
        "expect_reference_any": ["产品功能清单.md"],
    },
    {
        "id": "communication-strategy",
        "name": "纯沟通策略",
        "transcript": [
            {
                "speaker": "客户",
                "text": "你们讲了半天我还是不满意，今天必须给我一个明确说法。",
            },
        ],
        "expect_any": ["理解", "具体", "问题", "优先", "确认", "不满意"],
        "expect_type_any": ["advisory", "clarify"],
    },
    {
        "id": "customer-confidentiality",
        "name": "客户名称保密",
        "transcript": [
            {
                "speaker": "客户",
                "text": "你们以前给哪些客户做过审批流和私有化？把名字告诉我。",
            },
        ],
        "expect_any": ["案例", "授权", "行业", "客户", "匿名"],
        "forbid_patterns": [r"西南零售连锁", r"北方能源集团"],
    },
]


def _visible_len(text):
    return len(re.sub(r"\s+", "", str(text or "")))


def _add_check(checks, name, passed, detail="", weight=1):
    checks.append(
        {
            "name": name,
            "passed": bool(passed),
            "detail": str(detail or ""),
            "weight": weight,
        }
    )


def evaluate_result(case, result):
    """对单次模型结果做确定性评分，不再调用另一个 LLM 当裁判。"""
    checks = []
    suggestions = result.get("suggestions") or []
    hits = result.get("hits") or []
    available_refs = {str(hit.get("source") or "") for hit in hits}

    _add_check(
        checks,
        "可解析",
        not result.get("error"),
        (result.get("error") or {}).get("message", ""),
        weight=3,
    )
    _add_check(
        checks,
        "建议条数",
        2 <= len(suggestions) <= 3,
        f"实际 {len(suggestions)} 条",
    )
    _add_check(
        checks,
        "字段完整",
        bool(suggestions)
        and all(item.get("intent") and item.get("script") for item in suggestions),
        "intent/script 均须非空",
        weight=2,
    )

    lengths = [_visible_len(item.get("script")) for item in suggestions]
    _add_check(
        checks,
        "话术长度",
        bool(lengths) and max(lengths) <= SUGGESTION_SCRIPT_MAX_CHARS,
        f"长度 {lengths}，上限 {SUGGESTION_SCRIPT_MAX_CHARS}",
        weight=3,
    )

    levels = [item.get("type") for item in suggestions]
    _add_check(
        checks,
        "分级合法",
        bool(levels)
        and all(level in {"grounded", "advisory", "clarify"} for level in levels),
        f"实际 {levels}",
        weight=2,
    )

    references = [
        ref for item in suggestions for ref in (item.get("references") or [])
    ]
    bogus_refs = [ref for ref in references if ref not in available_refs]
    grounded_without_ref = [
        index + 1
        for index, item in enumerate(suggestions)
        if item.get("type") == "grounded" and not item.get("references")
    ]
    _add_check(
        checks,
        "引用一致",
        not bogus_refs and not grounded_without_ref,
        f"无效引用 {bogus_refs}；无引用 grounded {grounded_without_ref}",
        weight=2,
    )
    grounded_without_evidence = [
        index + 1
        for index, item in enumerate(suggestions)
        if item.get("type") == "grounded" and not item.get("evidence")
    ]
    malformed_evidence = [
        index + 1
        for index, item in enumerate(suggestions)
        if any(
            not isinstance(evidence, dict)
            or not evidence.get("source")
            or not evidence.get("quote")
            for evidence in (item.get("evidence") or [])
        )
    ]
    _add_check(
        checks,
        "原文证据完整",
        not grounded_without_evidence and not malformed_evidence,
        (
            f"无原文 grounded {grounded_without_evidence}；"
            f"格式异常 {malformed_evidence}"
        ),
        weight=3,
    )

    sensitive_items = [
        item.get("_sensitive")
        for item in suggestions
        if item.get("_sensitive")
    ]
    _add_check(
        checks,
        "可直接说出口",
        not sensitive_items,
        "；".join(sensitive_items),
        weight=3,
    )

    shortened = [
        item.get("_original_script_length")
        for item in suggestions
        if item.get("_shortened")
    ]
    _add_check(
        checks,
        "模型主动遵守长度",
        not shortened,
        f"程序兜底前长度 {shortened}",
    )

    combined = "\n".join(
        f"{item.get('intent', '')}\n{item.get('script', '')}"
        for item in suggestions
    )
    expected = case.get("expect_any") or []
    if expected:
        matched = [word for word in expected if word.lower() in combined.lower()]
        _add_check(
            checks,
            "场景关键点",
            bool(matched),
            f"命中 {matched or '无'}；候选 {expected}",
            weight=2,
        )

    expected_refs = case.get("expect_reference_any") or []
    if expected_refs:
        matched_refs = [ref for ref in expected_refs if ref in references]
        _add_check(
            checks,
            "使用相关依据",
            bool(matched_refs),
            f"实际引用 {references or '无'}",
            weight=2,
        )

    expected_types = case.get("expect_type_any") or []
    if expected_types:
        _add_check(
            checks,
            "建议类型符合场景",
            any(level in expected_types for level in levels),
            f"实际 {levels}；期望至少一个 {expected_types}",
            weight=2,
        )

    if case.get("forbid_grounded"):
        _add_check(
            checks,
            "无依据时不冒充有依据",
            "grounded" not in levels,
            f"实际 {levels}",
            weight=3,
        )

    # 红线只看【会说出口的 script】，不看 intent 标题。
    # 否则「提示是否属于定制」这类内部意图也会被 (属于).{0,6}定制 误杀。
    spoken = "\n".join(str(item.get("script") or "") for item in suggestions)
    forbidden_matches = []
    for pattern in case.get("forbid_patterns") or []:
        forbidden_matches.extend(re.findall(pattern, spoken, flags=re.I))
    if case.get("forbid_patterns"):
        _add_check(
            checks,
            "未命中场景红线",
            not forbidden_matches,
            f"命中 {forbidden_matches}",
            weight=3,
        )

    earned = sum(check["weight"] for check in checks if check["passed"])
    total = sum(check["weight"] for check in checks)
    return {
        "passed": all(check["passed"] for check in checks),
        "score": round(earned / total * 100, 1) if total else 0.0,
        "scriptLengths": lengths,
        "checks": checks,
    }


def _select_cases(case_number):
    if case_number is None:
        return CASES
    if not 1 <= case_number <= len(CASES):
        raise ValueError(f"--case 应在 1～{len(CASES)} 之间")
    return [CASES[case_number - 1]]


def run_benchmark(engine, cases, runs):
    records = []
    for round_number in range(1, runs + 1):
        for case in cases:
            started = time.perf_counter()
            try:
                result = engine.suggest(case["transcript"])
            except Exception as exc:  # 保留失败样本，不能让整轮数据消失
                result = {
                    "suggestions": [],
                    "hits": [],
                    "error": {"message": f"{type(exc).__name__}: {exc}"},
                }
            elapsed = time.perf_counter() - started
            evaluation = evaluate_result(case, result)
            records.append(
                {
                    "round": round_number,
                    "caseId": case["id"],
                    "caseName": case["name"],
                    "elapsedSeconds": round(elapsed, 3),
                    "evaluation": evaluation,
                    "result": result,
                }
            )
            status = "PASS" if evaluation["passed"] else "FAIL"
            print(
                f"[{status}] 第 {round_number} 轮 / {case['name']}: "
                f"{evaluation['score']:.1f} 分，{elapsed:.2f}s，"
                f"长度 {evaluation['scriptLengths']}"
            )
            for check in evaluation["checks"]:
                if not check["passed"]:
                    print(f"       - {check['name']}: {check['detail']}")
    return records


def summarize(records):
    scores = [item["evaluation"]["score"] for item in records]
    elapsed = [item["elapsedSeconds"] for item in records]
    lengths = [
        length
        for item in records
        for length in item["evaluation"]["scriptLengths"]
    ]
    passed = sum(1 for item in records if item["evaluation"]["passed"])
    case_stats = {}
    for item in records:
        case = case_stats.setdefault(
            item["caseId"],
            {"name": item["caseName"], "runs": 0, "passed": 0, "scores": []},
        )
        case["runs"] += 1
        case["passed"] += int(item["evaluation"]["passed"])
        case["scores"].append(item["evaluation"]["score"])
    for case in case_stats.values():
        case["averageScore"] = round(statistics.mean(case.pop("scores")), 1)

    return {
        "samples": len(records),
        "passed": passed,
        "passRate": round(passed / len(records), 4) if records else 0,
        "averageScore": round(statistics.mean(scores), 1) if scores else 0,
        "latencyMedianSeconds": round(statistics.median(elapsed), 3) if elapsed else 0,
        "latencyP95Seconds": round(
            sorted(elapsed)[max(0, math.ceil(len(elapsed) * 0.95) - 1)], 3
        )
        if elapsed
        else 0,
        "latencyMaxSeconds": round(max(elapsed), 3) if elapsed else 0,
        "scriptLengthMedian": round(statistics.median(lengths), 1) if lengths else 0,
        "scriptLengthMax": max(lengths) if lengths else 0,
        "cases": case_stats,
    }


def main():
    parser = argparse.ArgumentParser(description="话术建议质量基准")
    parser.add_argument("--runs", type=int, default=3, help="每个场景重复次数，默认 3")
    parser.add_argument("--case", type=int, help="只跑指定场景（从 1 开始）")
    parser.add_argument("--provider", help="临时指定 LLM 供应商")
    parser.add_argument("--model", help="临时指定模型名")
    parser.add_argument("--output", help="JSON 报告路径；默认写入 poc/eval")
    parser.add_argument("--no-save", action="store_true", help="不保存 JSON 报告")
    parser.add_argument(
        "--min-pass-rate",
        type=float,
        default=0.85,
        help="最低整场通过率，默认 0.85",
    )
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs 至少为 1")

    cases = _select_cases(args.case)
    kb = providers.build_kb(docs_dir=str(ROOT / "docs"), verbose=False)
    engine = providers.build_llm(
        kb, me_name=ME, provider=args.provider, model=args.model
    )
    print(
        f"模型：{engine.label} / {engine.model}；"
        f"场景 {len(cases)} 个 × {args.runs} 轮"
    )
    records = run_benchmark(engine, cases, args.runs)
    summary = summarize(records)
    report = {
        "schemaVersion": 1,
        "createdAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "provider": engine.provider,
        "model": engine.model,
        "runs": args.runs,
        "scriptMaxChars": SUGGESTION_SCRIPT_MAX_CHARS,
        "summary": summary,
        "records": records,
    }

    print(
        "\n汇总："
        f"通过 {summary['passed']}/{summary['samples']} "
        f"({summary['passRate']:.0%})；平均 {summary['averageScore']:.1f} 分；"
        f"延迟 P50/P95 {summary['latencyMedianSeconds']:.2f}/"
        f"{summary['latencyP95Seconds']:.2f}s；"
        f"话术长度 P50/Max {summary['scriptLengthMedian']}/"
        f"{summary['scriptLengthMax']} 字"
    )

    if not args.no_save:
        output = Path(args.output) if args.output else (
            ROOT
            / "eval"
            / f"suggestion_benchmark_{datetime.now():%Y%m%d_%H%M%S}.json"
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"报告：{output}")

    return 0 if summary["passRate"] >= args.min_pass_rate else 1


if __name__ == "__main__":
    sys.exit(main())
