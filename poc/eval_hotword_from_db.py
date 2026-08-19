"""从本机会议库抽取转写并做 G6 热词召回评分（隐私：报告不含全文）。

用法::

    python eval_hotword_from_db.py
    python eval_hotword_from_db.py --meeting-id meeting-xxx --terms eval/hotword_targets.example.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from eval_real_meetings import user_data_dir
from hotword_recall import load_terms, pass_criteria, render_markdown_report, score_hotword_recall

HERE = Path(__file__).resolve().parent


def transcript_text(con: sqlite3.Connection, meeting_id: str) -> str:
    row = con.execute(
        "SELECT transcript_mode, transcript_versions_json FROM meetings WHERE id=?",
        (meeting_id,),
    ).fetchone()
    if row and row[1]:
        try:
            versions = json.loads(row[1])
            preferred = row[0] or "offline"
            for key in (preferred, "offline", "realtime", "live"):
                version = (versions or {}).get(key)
                if isinstance(version, dict):
                    lines = version.get("transcript") or []
                elif isinstance(version, list):
                    lines = version
                else:
                    lines = []
                texts = [
                    str(item.get("text") or "")
                    for item in lines
                    if isinstance(item, dict) and item.get("text")
                ]
                if texts:
                    return "\n".join(texts)
        except Exception:
            pass
    lines = con.execute(
        """
        SELECT text FROM transcripts
        WHERE meeting_id=? AND is_final=1 AND text IS NOT NULL AND TRIM(text) != ''
        ORDER BY at ASC, id ASC
        """,
        (meeting_id,),
    ).fetchall()
    return "\n".join(str(r[0]) for r in lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score hotword recall from local meeting DB")
    parser.add_argument("--db", default=str(user_data_dir() / "meeting-copilot.sqlite"))
    parser.add_argument("--terms", default=str(HERE / "eval" / "hotword_targets.example.json"))
    parser.add_argument("--meeting-id", action="append", dest="meeting_ids")
    parser.add_argument(
        "--latest",
        action="store_true",
        help="只评最近一场满足 --min-seconds 的会议",
    )
    parser.add_argument(
        "--union-last",
        type=int,
        default=0,
        help="合并最近 N 场（≥min-seconds）的转写后再评分",
    )
    parser.add_argument("--min-seconds", type=float, default=300.0)
    parser.add_argument("--out-dir", default=str(HERE / "eval" / "reports"))
    parser.add_argument(
        "--min-term-recall",
        type=float,
        default=0.0,
        help="历史会默认 0（只观测）；协议短会可设 1.0",
    )
    parser.add_argument(
        "--require-hotwords-loaded",
        action="store_true",
        help="要求 hotwords_status=loaded，否则记 FAIL",
    )
    parser.add_argument(
        "--strict-ekp",
        action="store_true",
        help="评分时去掉 EKP 的 1KP/一KP 等诊断别名，只认精确 EKP",
    )
    args = parser.parse_args(argv)

    terms_raw = json.loads(Path(args.terms).read_text(encoding="utf-8"))
    if args.strict_ekp and isinstance(terms_raw, dict):
        for item in terms_raw.get("terms") or terms_raw.get("targets") or []:
            if isinstance(item, dict) and str(item.get("text") or "").upper() == "EKP":
                item["aliases"] = [
                    a
                    for a in (item.get("aliases") or [])
                    if str(a).casefold() == "ekp"
                ]
    terms = load_terms(terms_raw)
    required = []
    if isinstance(terms_raw, dict):
        for item in terms_raw.get("terms") or terms_raw.get("targets") or []:
            if isinstance(item, dict) and item.get("required"):
                name = str(item.get("text") or item.get("term") or "")
                if name:
                    required.append(name)

    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        if args.meeting_ids:
            ids = args.meeting_ids
        else:
            ids = [
                str(r[0])
                for r in con.execute(
                    "SELECT id FROM meetings WHERE COALESCE(audio_seconds,0) >= ? "
                    "ORDER BY started_at DESC",
                    (args.min_seconds,),
                )
            ]
            if args.latest:
                ids = ids[:1]
            elif args.union_last and args.union_last > 0:
                ids = ids[: int(args.union_last)]
        if not ids:
            print("no meetings matched", file=sys.stderr)
            return 2

        summaries = []
        out_dir = Path(args.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        any_fail = False

        def score_one(mid: str, text: str, label: str, meta_row) -> dict:
            nonlocal any_fail
            report = score_hotword_recall(text, terms)
            ok, reasons = pass_criteria(
                report,
                min_term_recall=float(args.min_term_recall),
                required_terms=(required if float(args.min_term_recall) >= 1.0 else None),
            )
            hw_status = meta_row[2] if meta_row else None
            if args.require_hotwords_loaded and hw_status != "loaded":
                ok = False
                reasons = list(reasons) + [f"hotwords_status={hw_status!r}（需要 loaded）"]
            if not ok:
                any_fail = True
            item = {
                "meetingId": mid,
                "title": meta_row[0] if meta_row else label,
                "audioSeconds": meta_row[1] if meta_row else None,
                "mode": meta_row[5] if meta_row else None,
                "status": meta_row[6] if meta_row else None,
                "hotwords": {
                    "status": hw_status,
                    "count": meta_row[3] if meta_row else None,
                    "hasVocabularyId": bool(meta_row[4]) if meta_row else None,
                },
                "termRecall": report.term_recall,
                "hitCount": report.hit_count,
                "termCount": report.term_count,
                "hits": [t.text for t in report.terms if t.hit],
                "misses": report.misses,
                "details": [
                    {
                        "text": t.text,
                        "found": t.found_mentions,
                        "contexts": t.contexts[:2],
                    }
                    for t in report.terms
                    if t.found_mentions
                ],
                "pass": ok,
                "failReasons": reasons,
            }
            safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", mid)[:80]
            stem = out_dir / f"hotword_recall_db_{safe_id}"
            stem.with_suffix(".json").write_text(
                json.dumps(item, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            stem.with_suffix(".md").write_text(
                render_markdown_report(
                    report,
                    title=f"G6 DB 召回 — {item['title'] or mid}",
                    meta={
                        "meeting_id": mid,
                        "hotwords_status": hw_status,
                        "audio_seconds": item["audioSeconds"],
                        "pass": ok,
                        "note": label,
                    },
                ),
                encoding="utf-8",
            )
            flag = "PASS" if ok else "FAIL"
            print(
                f"{mid}: {flag} hits={report.hit_count}/{report.term_count} "
                f"{item['hits']} misses={item['misses']} hw={hw_status}"
            )
            for reason in reasons:
                print(f"  - {reason}")
            return item

        if args.union_last and args.union_last > 0 and not args.meeting_ids:
            texts = []
            metas = []
            all_loaded = True
            for mid in ids:
                meta = con.execute(
                    "SELECT title, audio_seconds, hotwords_status, hotwords_count, "
                    "hotwords_vocabulary_id, meeting_mode, status FROM meetings WHERE id=?",
                    (mid,),
                ).fetchone()
                if not meta:
                    continue
                text = transcript_text(con, mid)
                texts.append(text)
                metas.append(meta)
                if meta[2] != "loaded":
                    all_loaded = False
                # observational per-meeting line (does not drive exit code)
                obs = score_hotword_recall(text, terms)
                print(
                    f"{mid}: (obs) hits={obs.hit_count}/{obs.term_count} "
                    f"{[t.text for t in obs.terms if t.hit]} "
                    f"misses={obs.misses} hw={meta[2]}"
                )
            union_text = "\n".join(texts)
            union_meta = (
                f"union:{'+'.join(ids)}",
                sum(float(m[1] or 0) for m in metas),
                "loaded" if all_loaded and metas else (metas[0][2] if metas else None),
                max((m[3] or 0) for m in metas) if metas else 0,
                metas[0][4] if metas else None,
                "union",
                "completed",
            )
            print("--- union ---")
            any_fail = False
            summaries.append(
                score_one(
                    "union-" + "-".join(i[-6:] for i in ids),
                    union_text,
                    f"union of last {len(ids)} meetings",
                    union_meta,
                )
            )
        else:
            for mid in ids:
                meta = con.execute(
                    "SELECT title, audio_seconds, hotwords_status, hotwords_count, "
                    "hotwords_vocabulary_id, meeting_mode, status FROM meetings WHERE id=?",
                    (mid,),
                ).fetchone()
                if not meta:
                    continue
                text = transcript_text(con, mid)
                summaries.append(score_one(mid, text, "single meeting", meta))
    finally:
        con.close()

    overview = {
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "termsFile": args.terms,
        "minTermRecall": args.min_term_recall,
        "meetings": [
            {
                "meetingId": s["meetingId"],
                "audioSeconds": s["audioSeconds"],
                "hotwords": s["hotwords"],
                "termRecall": s["termRecall"],
                "hits": s["hits"],
                "misses": s["misses"],
                "pass": s["pass"],
            }
            for s in summaries
        ],
    }
    overview_path = Path(args.out_dir) / "hotword_recall_db_overview.json"
    overview_path.write_text(
        json.dumps(overview, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"wrote {overview_path} meetings={len(summaries)}")
    if not summaries:
        return 2
    if float(args.min_term_recall) > 0 and any_fail:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
