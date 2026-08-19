"""Local unit tests for G6 hotword recall scoring (no network)."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from hotword_recall import (
    extract_transcript_text,
    load_terms,
    pass_criteria,
    score_hotword_recall,
)


def test_load_terms_and_exact_hits():
    terms = load_terms(
        {
            "terms": [
                {"text": "三快", "expected_mentions": 2, "weight": 5},
                {"text": "EKP", "aliases": ["ekp"], "expected_mentions": 1},
                {"text": "MK", "aliases": ["mk"], "expected_mentions": 1},
            ]
        }
    )
    assert len(terms) == 3
    transcript = "今天对齐蓝凌三快；ekp 环境就绪，MK 合同也要看。"
    report = score_hotword_recall(transcript, terms)
    assert report.hit_count == 3
    assert report.miss_count == 0
    assert report.term_recall == 1.0
    by_text = {t.text: t for t in report.terms}
    assert by_text["三快"].found_mentions >= 1
    assert by_text["EKP"].found_mentions >= 1
    assert by_text["MK"].found_mentions >= 1


def test_misses_and_pass_criteria():
    terms = load_terms(["三快", "蓝凌", "幽灵词"])
    report = score_hotword_recall("蓝凌这边三快进度正常", terms)
    assert report.hit_count == 2
    assert "幽灵词" in report.misses
    ok, reasons = pass_criteria(report, min_term_recall=1.0)
    assert ok is False
    assert reasons
    ok2, _ = pass_criteria(report, min_term_recall=0.5, required_terms=["三快", "蓝凌"])
    assert ok2 is True


def test_extract_from_meeting_record_and_markdown():
    record = {
        "transcriptVersion": "offline",
        "transcriptVersions": {
            "offline": {
                "transcript": [
                    {"id": "1", "text": "EKP 基座要升级"},
                    {"id": "2", "text": "MK 报价对齐蓝凌"},
                ]
            }
        },
    }
    text = extract_transcript_text(record)
    assert "EKP" in text and "MK" in text

    md = """# demo\n\n[00:12] 我：请确认三快范围\n对方：好的\n"""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "t.md"
        path.write_text(md, encoding="utf-8")
        assert "三快" in extract_transcript_text(path)


def test_example_targets_file_scores_script():
    root = Path(__file__).resolve().parents[1]
    example = root / "eval" / "hotword_targets.example.json"
    payload = json.loads(example.read_text(encoding="utf-8"))
    terms = load_terms(payload)
    script = " ".join(payload.get("spoken_script_zh") or [])
    report = score_hotword_recall(script, terms)
    # 口播金标自身应对全部 required 命中
    assert report.term_recall == 1.0
    assert report.miss_count == 0


if __name__ == "__main__":
    test_load_terms_and_exact_hits()
    test_misses_and_pass_criteria()
    test_extract_from_meeting_record_and_markdown()
    test_example_targets_file_scores_script()
    print("ok: hotword recall scoring + example targets")
