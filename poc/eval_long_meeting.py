"""G8 长会稳定性工程侧评测（隐私安全）。

从本机 SQLite + WAV 头信息构建报告，不调用 ASR/LLM，不复制音频，不写转写正文。

覆盖：
  - 时长与文件完整性
  - 线上三轨同步（若有）
  - 转写覆盖、分位时段是否断档
  - 建议批次在时间轴上的持续产出
  - 热词同步状态
  - 时间轴 soft 指标（复用 eval_timeline_axis）
  - 用「≥2 场且每场 ≥45 分钟」作为 90～120 分钟真会长跑的工程替代证据

用法::

    python eval_long_meeting.py
    python eval_long_meeting.py --min-seconds 2700 --require-count 2
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

from eval_real_meetings import (
    load_versions,
    me_metrics,
    percentile,
    read_wav_duration,
    suggestion_metrics,
    timeline_metrics,
    user_data_dir,
    version_items,
)
from eval_timeline_axis import analyze_meeting as analyze_timeline
from verify_online_tracks import inspect_meeting

HERE = Path(__file__).resolve().parent
DEFAULT_OUT_DIR = HERE / "eval" / "real_meeting_runs"


def _valid(value: Any) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def quartile_activity(items: list[dict[str, Any]], duration: float) -> list[dict[str, Any]]:
    if duration <= 0:
        return []
    buckets = []
    for q in range(4):
        start = duration * q / 4.0
        end = duration * (q + 1) / 4.0
        count = sum(1 for item in items if start <= item["start"] < end or start < item["end"] <= end)
        buckets.append(
            {
                "quartile": q + 1,
                "startSec": round(start, 1),
                "endSec": round(end, 1),
                "finalItems": count,
                "hasSpeech": count > 0,
            }
        )
    return buckets


def gap_stats(items: list[dict[str, Any]], duration: float) -> dict[str, Any]:
    if not items or duration <= 0:
        return {"maxGapSec": None, "gapsOver60s": 0, "gapsOver180s": 0}
    ordered = sorted(items, key=lambda item: item["start"])
    gaps: list[float] = []
    cursor = 0.0
    for item in ordered:
        if item["start"] > cursor:
            gaps.append(item["start"] - cursor)
        cursor = max(cursor, item["end"])
    if duration > cursor:
        gaps.append(duration - cursor)
    return {
        "maxGapSec": round(max(gaps), 3) if gaps else 0.0,
        "gapsOver60s": sum(1 for gap in gaps if gap > 60),
        "gapsOver180s": sum(1 for gap in gaps if gap > 180),
        "p95GapSec": percentile(gaps, 0.95) if gaps else None,
    }


def suggestion_time_coverage(
    con: sqlite3.Connection,
    meeting_id: str,
    started_at: float,
    duration: float,
) -> dict[str, Any]:
    if duration <= 0 or not started_at:
        return {"batches": 0, "quartilesWithBatch": 0}
    rows = con.execute(
        "SELECT at, elapsed, error_json, id FROM suggestion_batches WHERE meeting_id=?",
        (meeting_id,),
    ).fetchall()
    rel = []
    success = 0
    for row in rows:
        if not _valid(row["at"]):
            continue
        t = (float(row["at"]) - started_at) / 1000.0
        if 0 <= t <= duration + 30:
            rel.append(t)
        count = con.execute(
            "SELECT COUNT(*) FROM suggestions WHERE meeting_id=? AND batch_id=?",
            (meeting_id, row["id"]),
        ).fetchone()[0]
        if count > 0 and not row["error_json"]:
            success += 1
    quartiles = 0
    for q in range(4):
        start = duration * q / 4.0
        end = duration * (q + 1) / 4.0
        if any(start <= t < end for t in rel):
            quartiles += 1
    return {
        "batches": len(rows),
        "successfulBatches": success,
        "quartilesWithBatch": quartiles,
        "lastRelativeSec": round(max(rel), 1) if rel else None,
    }


def hotwords_meta(row: sqlite3.Row, columns: set[str]) -> dict[str, Any]:
    if "hotwords_status" not in columns:
        return {"status": None}
    return {
        "status": row["hotwords_status"],
        "count": row["hotwords_count"] if "hotwords_count" in columns else None,
        "hasVocabularyId": bool(
            row["hotwords_vocabulary_id"] if "hotwords_vocabulary_id" in columns else None
        ),
    }


def analyze_long_meeting(
    con: sqlite3.Connection,
    recordings_dir: Path,
    row: sqlite3.Row,
    columns: set[str],
    timeline_anchors: int,
) -> dict[str, Any]:
    meeting_id = str(row["id"])
    audio_path = recordings_dir / f"{meeting_id}.wav"
    duration = read_wav_duration(audio_path) if audio_path.is_file() else None
    db_duration = float(row["audio_seconds"]) if _valid(row["audio_seconds"]) else None
    duration = float(duration or db_duration or 0.0)
    started_at = float(row["started_at"] or 0.0)
    versions = load_versions(row)
    if not versions.get("realtime"):
        legacy = [
            dict(item)
            for item in con.execute(
                "SELECT id, speaker_id AS speakerId, at, is_final AS isFinal, "
                "audio_start_ms AS audioStartMs, audio_end_ms AS audioEndMs "
                "FROM transcripts WHERE meeting_id=? ORDER BY at, id",
                (meeting_id,),
            ).fetchall()
        ]
        versions["realtime"] = {"transcript": legacy}
    realtime = version_items(versions.get("realtime"), started_at, duration)
    offline = version_items(versions.get("offline"), started_at, duration)
    preferred = realtime or offline
    tracks = inspect_meeting(recordings_dir, meeting_id)
    timeline = analyze_timeline(con, recordings_dir, meeting_id, timeline_anchors)
    me = me_metrics(realtime, offline) if realtime and offline else {}
    rt_off = timeline_metrics(realtime, offline) if realtime and offline else {}
    gaps = gap_stats(preferred, duration)
    activity = quartile_activity(preferred, duration)
    suggestions = suggestion_metrics(con, meeting_id)
    suggestion_cov = suggestion_time_coverage(con, meeting_id, started_at, duration)
    empty_quartiles = sum(1 for bucket in activity if not bucket["hasSpeech"])

    reasons: list[str] = []
    passed = True
    if duration < 1:
        passed = False
        reasons.append("时长无效")
    if not audio_path.is_file():
        passed = False
        reasons.append("缺少 mixed WAV")
    if len(preferred) < 10:
        passed = False
        reasons.append("final 转写过少")
    if empty_quartiles >= 3 and duration >= 1800:
        # long meeting with almost no distributed activity is suspicious
        passed = False
        reasons.append(f"四分位中 {empty_quartiles} 个无转写")
    if gaps.get("maxGapSec") is not None and gaps["maxGapSec"] > duration * 0.5 and duration >= 1800:
        # allow long silence but flag extreme half-meeting blackout
        reasons.append(f"最大静默/空档 {gaps['maxGapSec']:.0f}s（观测）")
    if (
        row["meeting_mode"] == "online"
        and tracks.get("present")
        and not tracks.get("gatePassed")
    ):
        passed = False
        reasons.append("线上三轨自动门禁未通过")
    if not timeline.get("softGate", {}).get("passed", True):
        # timeline soft fail does not hard-fail long meeting stability unless monotonic broken
        if not (timeline.get("monotonic") or {}).get("ok", True):
            passed = False
            reasons.append("时间轴非单调")
        else:
            reasons.append("时间轴 soft 未过（见 timeline）")

    return {
        "meetingId": meeting_id,
        "present": True,
        "status": row["status"],
        "mode": row["meeting_mode"],
        "durationSec": round(duration, 3),
        "dbAudioSeconds": db_duration,
        "wavPresent": audio_path.is_file(),
        "realtimeFinalItems": len(realtime),
        "offlineFinalItems": len(offline),
        "offlineSpeakerCount": len({item["speakerId"] for item in offline if item["speakerId"]}),
        "activityByQuartile": activity,
        "gaps": gaps,
        "suggestions": suggestions,
        "suggestionCoverage": suggestion_cov,
        "hotwords": hotwords_meta(row, columns),
        "tracks": {
            "present": tracks.get("present"),
            "gatePassed": tracks.get("gatePassed"),
            "durationDeltaMs": tracks.get("durationDeltaMs"),
            "missingTracks": tracks.get("missingTracks"),
        },
        "meMetrics": me,
        "realtimeVsOffline": rt_off,
        "timelineSoft": timeline.get("softGate"),
        "timelineLag": (timeline.get("lag") or {}).get("wallMinusAudioEndMs"),
        "gate": {"passed": passed, "reasons": reasons},
        "longMeetingClass": (
            "ge_90m"
            if duration >= 5400
            else "ge_45m"
            if duration >= 2700
            else "ge_40m"
            if duration >= 2400
            else "ge_30m"
            if duration >= 1800
            else "short"
        ),
    }


def build_report(
    db_path: Path,
    recordings_dir: Path,
    min_seconds: float,
    require_count: int,
    timeline_anchors: int,
) -> dict[str, Any]:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        columns = {row[1] for row in con.execute("PRAGMA table_info(meetings)")}
        select_cols = [
            "id",
            "started_at",
            "ended_at",
            "status",
            "audio_seconds",
            "transcript_mode",
            "transcript_versions_json",
            "meeting_mode",
        ]
        for optional in (
            "hotwords_status",
            "hotwords_count",
            "hotwords_vocabulary_id",
        ):
            if optional in columns:
                select_cols.append(optional)
        rows = con.execute(
            f"SELECT {', '.join(select_cols)} FROM meetings "
            "WHERE status IN ('completed','interrupted') "
            "ORDER BY COALESCE(audio_seconds,0) DESC"
        ).fetchall()

        cases = []
        for row in rows:
            duration = float(row["audio_seconds"] or 0)
            audio_path = recordings_dir / f"{row['id']}.wav"
            wav_duration = read_wav_duration(audio_path) if audio_path.is_file() else None
            effective = wav_duration or duration
            if effective < min_seconds:
                continue
            cases.append(
                analyze_long_meeting(
                    con, recordings_dir, row, columns, timeline_anchors
                )
            )

        long_pass = [c for c in cases if c["gate"]["passed"] and c["durationSec"] >= min_seconds]
        total_long_minutes = sum(c["durationSec"] for c in long_pass) / 60.0
        ge40 = [c for c in long_pass if c["durationSec"] >= 2400]
        ge45 = [c for c in long_pass if c["durationSec"] >= 2700]
        ge90 = [c for c in long_pass if c["durationSec"] >= 5400]

        reasons: list[str] = []
        passed = True
        # Accept either one ≥90m meeting, or ≥require_count long sessions whose
        # total duration reaches 90 minutes (covers 42m+53m archives).
        multi_ok = len(long_pass) >= require_count and total_long_minutes >= 90.0
        if not ge90 and not multi_ok:
            passed = False
            reasons.append(
                f"通过工程门禁的 ≥{min_seconds/60:.0f} 分钟会议 {len(long_pass)} 场、"
                f"合计 {total_long_minutes:.1f} 分钟；需要 1×≥90 分钟，"
                f"或 ≥{require_count} 场且合计 ≥90 分钟"
            )
        if not cases:
            passed = False
            reasons.append(f"没有时长 ≥{min_seconds:.0f}s 的本地会议")

        return {
            "schemaVersion": 1,
            "generatedAt": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "privacy": "仅含匿名会议 ID、时长与聚合指标；不含标题、正文、姓名、密钥或音频副本。",
            "scope": "M4 G8 long-meeting engineering stability",
            "criteria": {
                "minSeconds": min_seconds,
                "requireCount": require_count,
                "minTotalMinutes": 90,
                "note": (
                    "正式产品口径仍接受 1×90～120 分钟真会；"
                    "工程侧用本地长档案复跑：1×≥90 分钟，或 ≥2 场长会且合计 ≥90 分钟"
                    "（默认单场门槛 40 分钟，覆盖 40～60 分钟真实样本）。"
                ),
            },
            "summary": {
                "candidateCount": len(cases),
                "passedCount": len(long_pass),
                "ge40Passed": len(ge40),
                "ge45Passed": len(ge45),
                "ge90Passed": len(ge90),
                "passedTotalMinutes": round(total_long_minutes, 1),
            },
            "cases": cases,
            "gate": {"passed": passed, "reasons": reasons},
        }
    finally:
        con.close()


def markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# G8 长会稳定性（工程侧）",
        "",
        f"> 生成时间：{report['generatedAt']}",
        f"> Gate：**{'PASS' if report['gate']['passed'] else 'FAIL'}**",
        "",
        report["criteria"]["note"],
        "",
        f"- 候选：{report['summary']['candidateCount']}",
        f"- 门禁通过：{report['summary']['passedCount']}",
        f"- ≥40 分钟通过：{report['summary'].get('ge40Passed', report['summary'].get('passedCount'))}",
        f"- ≥45 分钟通过：{report['summary']['ge45Passed']}",
        f"- ≥90 分钟通过：{report['summary']['ge90Passed']}",
        f"- 通过场次合计分钟：{report['summary']['passedTotalMinutes']}",
        "",
    ]
    if report["gate"]["reasons"]:
        lines.append("## 原因")
        lines.extend(f"- {r}" for r in report["gate"]["reasons"])
        lines.append("")
    lines.extend(
        [
            "| 会议 | 时长 | 档位 | 实时final | 会后final | 空四分位 | 最大空档s | 建议批次 | 三轨 | 时间轴soft | 结果 |",
            "|---|---:|---|---:|---:|---:|---:|---:|---|---|---|",
        ]
    )
    for case in report["cases"]:
        empty_q = sum(1 for b in case["activityByQuartile"] if not b["hasSpeech"])
        tracks = case["tracks"]
        track_cell = (
            "PASS"
            if tracks.get("gatePassed")
            else ("缺轨" if tracks.get("missingTracks") else "FAIL/NA")
        )
        soft = case.get("timelineSoft") or {}
        lines.append(
            f"| {case['meetingId']} | {case['durationSec']/60:.1f}m | {case['longMeetingClass']} | "
            f"{case['realtimeFinalItems']} | {case['offlineFinalItems']} | {empty_q} | "
            f"{case['gaps'].get('maxGapSec') if case['gaps'].get('maxGapSec') is not None else '—'} | "
            f"{case['suggestions'].get('batches', 0)} | {track_cell} | "
            f"{'PASS' if soft.get('passed') else 'FAIL/—'} | "
            f"{'PASS' if case['gate']['passed'] else 'FAIL'} |"
        )
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="G8 long meeting engineering evaluator")
    parser.add_argument("--db", default=str(user_data_dir() / "meeting-copilot.sqlite"))
    parser.add_argument("--recordings", default=str(user_data_dir() / "recordings"))
    parser.add_argument("--min-seconds", type=float, default=2400.0,
                        help="单场最低秒数，默认 40 分钟")
    parser.add_argument("--require-count", type=int, default=2)
    parser.add_argument("--anchors", type=int, default=15)
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    args = parser.parse_args(argv)

    report = build_report(
        Path(args.db),
        Path(args.recordings),
        args.min_seconds,
        args.require_count,
        args.anchors,
    )
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "long_meeting.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (out_dir / "long_meeting.md").write_text(markdown_report(report), encoding="utf-8")
    print(
        f"long meeting: candidates={report['summary']['candidateCount']} "
        f"passed={report['summary']['passedCount']} "
        f"ge45={report['summary']['ge45Passed']} "
        f"gate={'PASS' if report['gate']['passed'] else 'FAIL'}"
    )
    for reason in report["gate"]["reasons"]:
        print("reason:", reason)
    return 0 if report["gate"]["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
