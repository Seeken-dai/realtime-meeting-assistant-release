"""
G6 热词真实召回对照 CLI（本地、无云端）。

示例：
  # 用示例目标词 + 某场导出的转写 JSON/MD
  python eval_hotword_recall.py ^
    --terms eval/hotword_targets.example.json ^
    --transcript path/to/meeting-transcript.json

  # 直接喂纯文本
  python eval_hotword_recall.py --terms eval/hotword_targets.example.json --text "我们在 EKP 和 MK 上做蓝凌三快"

默认把 JSON/MD 报告写到 eval/reports/（已 gitignore）。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from hotword_recall import (
    extract_transcript_text,
    load_terms,
    pass_criteria,
    render_markdown_report,
    score_hotword_recall,
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="G6 Aliyun hotword real-recall scorer")
    parser.add_argument(
        "--terms",
        required=True,
        help="目标专名 JSON（见 eval/hotword_targets.example.json）",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--transcript", help="转写文件：.json / .md / .txt")
    source.add_argument("--text", help="直接传入转写正文")
    parser.add_argument(
        "--min-term-recall",
        type=float,
        default=1.0,
        help="通过阈值，默认 1.0（核心专名全中）",
    )
    parser.add_argument(
        "--required",
        nargs="*",
        default=None,
        help="额外点名必须命中的专名（默认用 terms 里 required=true 的项）",
    )
    parser.add_argument(
        "--out-dir",
        default=None,
        help="报告目录，默认 poc/eval/reports",
    )
    parser.add_argument(
        "--label",
        default="",
        help="报告标签（如 meeting-id 或 日期-场景），勿写入敏感正文",
    )
    parser.add_argument(
        "--no-write",
        action="store_true",
        help="只打印摘要，不写报告文件",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    terms_raw = json.loads(Path(args.terms).read_text(encoding="utf-8"))
    terms = load_terms(terms_raw)

    if args.transcript:
        transcript = extract_transcript_text(args.transcript)
        transcript_source = str(args.transcript)
    else:
        transcript = args.text or ""
        transcript_source = "<inline-text>"

    report = score_hotword_recall(transcript, terms)

    required = list(args.required or [])
    if not required and isinstance(terms_raw, dict):
        for item in terms_raw.get("terms") or terms_raw.get("targets") or []:
            if isinstance(item, dict) and item.get("required"):
                required.append(str(item.get("text") or item.get("term") or ""))
        required = [r for r in required if r]

    ok, reasons = pass_criteria(
        report,
        min_term_recall=float(args.min_term_recall),
        required_terms=required or None,
    )

    meta = {
        "label": args.label or None,
        "terms_file": str(args.terms),
        "transcript_source": transcript_source,
        "asr_provider_hint": (
            terms_raw.get("asr_provider") if isinstance(terms_raw, dict) else None
        ),
        "pass": ok,
        "fail_reasons": "; ".join(reasons) if reasons else None,
        "scored_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    print(
        f"G6 hotword recall: term_recall={report.term_recall:.0%} "
        f"mention_recall={report.mention_recall:.0%} "
        f"hits={report.hit_count}/{report.term_count} "
        f"{'PASS' if ok else 'FAIL'}"
    )
    if report.misses:
        print("misses:", ", ".join(report.misses))
    if reasons:
        for reason in reasons:
            print("fail:", reason)
    for note in report.notes:
        print("note:", note)

    if not args.no_write:
        out_dir = Path(args.out_dir or Path(__file__).resolve().parent / "eval" / "reports")
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        label = re_slug(args.label or "hotword-recall")
        base = out_dir / f"hotword_recall_{label}_{stamp}"
        payload = {
            "meta": meta,
            "summary": {
                "term_recall": report.term_recall,
                "mention_recall": report.mention_recall,
                "hit_count": report.hit_count,
                "term_count": report.term_count,
                "misses": report.misses,
                "pass": ok,
                "fail_reasons": reasons,
            },
            "terms": [
                {
                    "id": t.id,
                    "text": t.text,
                    "expected_mentions": t.expected_mentions,
                    "found_mentions": t.found_mentions,
                    "hit": t.hit,
                    "contexts": t.contexts,
                }
                for t in report.terms
            ],
        }
        base.with_suffix(".json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        base.with_suffix(".md").write_text(
            render_markdown_report(report, meta=meta),
            encoding="utf-8",
        )
        print(f"wrote: {base.with_suffix('.json')}")
        print(f"wrote: {base.with_suffix('.md')}")

    return 0 if ok else 2


def re_slug(value: str) -> str:
    text = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in value.strip())
    text = "-".join(part for part in text.split("-") if part)
    return (text or "run")[:60]


if __name__ == "__main__":
    sys.exit(main())
