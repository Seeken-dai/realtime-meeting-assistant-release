"""合并多份话术基准报告，并按当前评分规则重新计算模型横评。

旧报告中的 evaluation 可能由旧版规则产生；本脚本始终读取原始 result，
调用当前 ``evaluate_result`` 重算，保证不同日期、不同模型使用同一把尺。
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from knowledge_base import extract_forbidden_terms, extract_internal_numbers
from suggest import _apply_length_limits, _validate
from suggestion_benchmark import CASES, evaluate_result, summarize


ROOT = Path(__file__).resolve().parent
CASE_BY_ID = {case["id"]: case for case in CASES}


def _benchmark_safety_terms():
    forbidden = set()
    internal_numbers = set()
    for path in (ROOT / "docs").glob("*.md"):
        text = path.read_text(encoding="utf-8")
        forbidden.update(extract_forbidden_terms(text))
        internal_numbers.update(extract_internal_numbers(text))
    return forbidden, internal_numbers


def compare_reports(paths):
    groups = defaultdict(list)
    sources = defaultdict(list)
    forbidden, internal_numbers = _benchmark_safety_terms()
    for path in paths:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        key = (data.get("provider") or "unknown", data.get("model") or "unknown")
        sources[key].append(str(path))
        for record in data.get("records") or []:
            case = CASE_BY_ID.get(record.get("caseId"))
            if not case:
                continue
            stored_result = record.get("result") or {}
            clean_suggestions = [
                {
                    key: value
                    for key, value in suggestion.items()
                    if not key.startswith("_")
                }
                for suggestion in (stored_result.get("suggestions") or [])
            ]
            refreshed_result = {
                **stored_result,
                "suggestions": _apply_length_limits(
                    _validate(
                        clean_suggestions,
                        stored_result.get("hits") or [],
                        forbidden,
                        internal_numbers,
                    )
                ),
            }
            refreshed = {
                **record,
                "result": refreshed_result,
                "evaluation": evaluate_result(case, refreshed_result),
                "sourceReport": str(path),
            }
            groups[key].append(refreshed)

    models = []
    for (provider, model), records in groups.items():
        summary = summarize(records)
        failures = defaultdict(lambda: {"name": "", "failed": 0, "runs": 0})
        sensitive_failures = 0
        shortened = 0
        for record in records:
            case = failures[record["caseId"]]
            case["name"] = record["caseName"]
            case["runs"] += 1
            case["failed"] += int(not record["evaluation"]["passed"])
            sensitive_failures += int(
                any(
                    check["name"] == "可直接说出口" and not check["passed"]
                    for check in record["evaluation"]["checks"]
                )
            )
            shortened += int(
                any(
                    check["name"] == "模型主动遵守长度" and not check["passed"]
                    for check in record["evaluation"]["checks"]
                )
            )
        models.append(
            {
                "provider": provider,
                "model": model,
                "summary": summary,
                "sensitiveFailureSamples": sensitive_failures,
                "lengthFallbackSamples": shortened,
                "failureCases": {
                    case_id: value
                    for case_id, value in failures.items()
                    if value["failed"]
                },
                "sourceReports": sources[(provider, model)],
            }
        )

    models.sort(
        key=lambda item: (
            -item["summary"]["passRate"],
            item["summary"]["latencyP95Seconds"],
            -item["summary"]["averageScore"],
        )
    )
    return {"models": models, "recommended": models[0] if models else None}


def main():
    parser = argparse.ArgumentParser(description="汇总并重算话术模型横评")
    parser.add_argument("reports", nargs="*", help="报告 JSON；空则读取 eval/*_r*.json")
    parser.add_argument("--output", help="汇总 JSON 输出路径")
    args = parser.parse_args()
    paths = [Path(path) for path in args.reports]
    if not paths:
        paths = sorted((ROOT / "eval").glob("*_r*.json"))
    if not paths:
        parser.error("没有找到可比较的报告")

    comparison = compare_reports(paths)
    print("模型横评（按当前规则重算）")
    for index, item in enumerate(comparison["models"], 1):
        summary = item["summary"]
        print(
            f"{index}. {item['provider']} / {item['model']}："
            f"{summary['passed']}/{summary['samples']} "
            f"({summary['passRate']:.0%})，平均 {summary['averageScore']:.1f}，"
            f"延迟 P50/P95 {summary['latencyMedianSeconds']:.2f}/"
            f"{summary['latencyP95Seconds']:.2f}s（Max "
            f"{summary['latencyMaxSeconds']:.2f}s），"
            f"话术 P50/Max {summary['scriptLengthMedian']}/"
            f"{summary['scriptLengthMax']} 字，"
            f"敏感失败 {item['sensitiveFailureSamples']} 次"
        )
        for failure in item["failureCases"].values():
            print(
                f"   - {failure['name']}：失败 "
                f"{failure['failed']}/{failure['runs']} 轮"
            )

    output = Path(args.output) if args.output else (
        ROOT / "eval" / f"suggestion_model_comparison_{datetime.now():%Y%m%d}.json"
    )
    output.write_text(
        json.dumps(comparison, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"报告：{output}")


if __name__ == "__main__":
    main()
