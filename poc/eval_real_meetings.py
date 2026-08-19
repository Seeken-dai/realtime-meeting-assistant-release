"""Build a privacy-safe local regression report from real meeting archives.

The script reads only SQLite metadata/transcript timing and WAV headers from the
Electron user-data directory.  It never copies audio or writes transcript text.
Generated manifest and reports contain anonymous case ids, relative times and
aggregate metrics only.

Usage::

    python eval_real_meetings.py
    python eval_real_meetings.py --out-dir eval/real_meeting_runs

The command is intentionally separate from the fast M4 gate.  It may inspect
real archives but does not call ASR/LLM services or load speaker models.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import sqlite3
import statistics
import sys
import wave
from pathlib import Path
from typing import Any, Iterable, Sequence


HERE = Path(__file__).resolve().parent
DEFAULT_OUT_DIR = HERE / "eval" / "real_meeting_runs"

# These are the samples explicitly named in the M4 plan.  The values are
# product facts, not inferred from the erroneous offline speaker labels.
KNOWN_CASES: dict[str, dict[str, Any]] = {
    "meeting-1785740272352-mcvn6u": {
        "caseId": "online-20260803-3p",
        "scene": "online_3_person",
        "mode": "online",
        "participants": 3,
        "purpose": "实时双通道基线、时间轴、线上会后误标复现",
        "anchorCount": 20,
        "truthScope": "realtime microphone=me; remote people intentionally unlabeled",
    },
    "meeting-1785145910139-hdk38k": {
        "caseId": "in-person-20260727-short",
        "scene": "in_person_short",
        "mode": "in_person",
        "participants": 2,
        "purpose": "既有我/非我与转写切分基准",
        "truthScope": "me-vs-other only",
    },
    "meeting-1785308626850-aj9eh1": {
        "caseId": "in-person-20260729-long",
        "scene": "in_person_long",
        "mode": "in_person",
        "participants": 2,
        "purpose": "长时稳定性、声纹漂移与既有说话人回归",
        "truthScope": "me-vs-other only",
    },
    "meeting-1785481440225-vhmd6u": {
        "caseId": "in-person-20260731-long",
        "scene": "in_person_long_supplement",
        "mode": "in_person",
        "participants": None,
        "purpose": "多说话人补充样本",
        "truthScope": "粗分，无逐段多人真值",
    },
    "meeting-1785484882739-galr54": {
        "caseId": "in-person-20260731-medium",
        "scene": "in_person_medium_supplement",
        "mode": "in_person",
        "participants": None,
        "purpose": "中等时长补充样本",
        "truthScope": "粗分，无逐段多人真值",
    },
    "meeting-1785999571869-kfd2ll": {
        "caseId": "online-20260806-long-triple",
        "scene": "online_long_triple_track",
        "mode": "online",
        "participants": None,
        "purpose": "线上长会三轨对齐、长时稳定性与建议持续产出",
        "anchorCount": 15,
        "truthScope": "三轨文件 + 聚合指标；远端身份无逐段金标",
    },
    "meeting-1785909591580-h60vir": {
        "caseId": "in-person-20260805-formula",
        "scene": "in_person_medium",
        "mode": "in_person",
        "participants": None,
        "purpose": "热词 loaded 场景下的专名/产品词观测与中长会稳定性",
        "truthScope": "无逐段多人真值；可对照 glossary 命中",
    },
}

def user_data_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise RuntimeError("找不到 APPDATA，无法定位桌面端数据目录")
    return Path(appdata) / "meeting-copilot-desktop"


def percentile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return round(ordered[0], 3)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        value = ordered[lower]
    else:
        value = ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)
    return round(value, 3)


def safe_ratio(numerator: float, denominator: float) -> float | None:
    return round(numerator / denominator, 4) if denominator > 0 else None


def read_wav_duration(path: Path) -> float | None:
    try:
        with wave.open(str(path), "rb") as wav:
            return round(wav.getnframes() / max(wav.getframerate(), 1), 3)
    except (OSError, wave.Error):
        return None


def union_intervals(intervals: Iterable[tuple[float, float]]) -> list[tuple[float, float]]:
    ordered = sorted(
        (float(start), float(end))
        for start, end in intervals
        if math.isfinite(float(start))
        and math.isfinite(float(end))
        and float(end) > float(start)
    )
    merged: list[tuple[float, float]] = []
    for start, end in ordered:
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return merged


def interval_seconds(intervals: Iterable[tuple[float, float]]) -> float:
    return sum(end - start for start, end in union_intervals(intervals))


def intersection_seconds(
    left: Iterable[tuple[float, float]], right: Iterable[tuple[float, float]]
) -> float:
    a = union_intervals(left)
    b = union_intervals(right)
    i = j = 0
    total = 0.0
    while i < len(a) and j < len(b):
        start = max(a[i][0], b[j][0])
        end = min(a[i][1], b[j][1])
        if end > start:
            total += end - start
        if a[i][1] <= b[j][1]:
            i += 1
        else:
            j += 1
    return total


def speaker_class(speaker_id: Any) -> str:
    return "me" if str(speaker_id or "") == "me" else "other"


def _valid_number(value: Any) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def version_items(
    version: dict[str, Any] | None,
    started_at: float,
    duration: float,
) -> list[dict[str, Any]]:
    """Return final items with a bounded relative interval, never their text."""

    if not isinstance(version, dict):
        return []
    raw_items = version.get("transcript") or []
    items: list[dict[str, Any]] = []
    previous_end = 0.0
    for raw in raw_items:
        if not isinstance(raw, dict) or raw.get("isFinal", True) is False:
            continue
        if not str(raw.get("id") or ""):
            continue
        start = raw.get("audioStartMs")
        end = raw.get("audioEndMs")
        if _valid_number(start) and _valid_number(end) and float(end) > float(start):
            rel_start = max(0.0, float(start) / 1000.0)
            rel_end = max(rel_start, float(end) / 1000.0)
        elif _valid_number(raw.get("at")) and started_at:
            rel_end = max(0.0, (float(raw["at"]) - started_at) / 1000.0)
            rel_end = min(rel_end, duration) if duration > 0 else rel_end
            rel_start = min(previous_end, rel_end)
        else:
            continue
        if duration > 0:
            rel_start = min(max(rel_start, 0.0), duration)
            rel_end = min(max(rel_end, rel_start), duration)
        if rel_end <= rel_start:
            # A zero-width timestamp is still useful for id matching but must
            # not inflate coverage metrics.
            rel_end = rel_start
        item = {
            "id": str(raw["id"]),
            "start": rel_start,
            "end": rel_end,
            "speakerId": None if raw.get("speakerId") is None else str(raw["speakerId"]),
            "speaker": speaker_class(raw.get("speakerId")),
        }
        items.append(item)
        previous_end = max(previous_end, rel_end)
    return sorted(items, key=lambda item: (item["start"], item["end"], item["id"]))


def load_versions(row: sqlite3.Row) -> dict[str, dict[str, Any]]:
    try:
        parsed = json.loads(row["transcript_versions_json"] or "{}")
    except (TypeError, ValueError):
        parsed = {}
    versions = parsed if isinstance(parsed, dict) else {}
    return {key: value for key, value in versions.items() if isinstance(value, dict)}


def match_split_item(item: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    base = item["id"]
    exact = [candidate for candidate in candidates if candidate["id"] == base]
    pieces = [
        candidate
        for candidate in candidates
        if candidate["id"].startswith(f"{base}#p")
    ]
    pool = exact or pieces
    if not pool:
        return None
    return min(
        pool,
        key=lambda candidate: abs(candidate["start"] - item["start"])
        + abs(candidate["end"] - item["end"]),
    )


def timeline_metrics(
    realtime: list[dict[str, Any]], offline: list[dict[str, Any]]
) -> dict[str, Any]:
    start_errors: list[float] = []
    end_errors: list[float] = []
    for item in realtime:
        matched = match_split_item(item, offline)
        if not matched:
            continue
        start_errors.append(abs(matched["start"] - item["start"]) * 1000.0)
        end_errors.append(abs(matched["end"] - item["end"]) * 1000.0)
    combined = start_errors + end_errors
    return {
        "matchedItems": len(start_errors),
        "startMs": {
            "median": percentile(start_errors, 0.5),
            "p90": percentile(start_errors, 0.9),
            "p95": percentile(start_errors, 0.95),
            "max": round(max(start_errors), 3) if start_errors else None,
        },
        "endMs": {
            "median": percentile(end_errors, 0.5),
            "p90": percentile(end_errors, 0.9),
            "p95": percentile(end_errors, 0.95),
            "max": round(max(end_errors), 3) if end_errors else None,
        },
        "combinedMs": {
            "median": percentile(combined, 0.5),
            "p90": percentile(combined, 0.9),
            "p95": percentile(combined, 0.95),
            "max": round(max(combined), 3) if combined else None,
        },
    }


def me_metrics(realtime: list[dict[str, Any]], offline: list[dict[str, Any]]) -> dict[str, Any]:
    reference_me = [(item["start"], item["end"]) for item in realtime if item["speaker"] == "me"]
    reference_speech = [(item["start"], item["end"]) for item in realtime]
    candidate_me = [(item["start"], item["end"]) for item in offline if item["speaker"] == "me"]
    evaluated = interval_seconds(reference_speech)
    tp = intersection_seconds(candidate_me, reference_me)
    candidate_inside_reference = intersection_seconds(candidate_me, reference_speech)
    fp = max(0.0, candidate_inside_reference - tp)
    fn = max(0.0, interval_seconds(reference_me) - tp)
    return {
        "referenceSpeechSec": round(evaluated, 3),
        "referenceMeSec": round(interval_seconds(reference_me), 3),
        "candidateMeSec": round(intersection_seconds(candidate_me, reference_speech), 3),
        "mePrecision": safe_ratio(tp, tp + fp),
        "meRecall": safe_ratio(tp, tp + fn),
        "meF1": safe_ratio(2 * tp, 2 * tp + fp + fn),
        "confusionSec": {
            "tp": round(tp, 3),
            "fp": round(fp, 3),
            "fn": round(fn, 3),
        },
    }


def suggestion_metrics(con: sqlite3.Connection, meeting_id: str) -> dict[str, Any]:
    rows = con.execute(
        "SELECT elapsed, error_json, id FROM suggestion_batches WHERE meeting_id=?",
        (meeting_id,),
    ).fetchall()
    latencies = [float(row[0]) for row in rows if _valid_number(row[0]) and float(row[0]) >= 0]
    successful = 0
    for row in rows:
        count = con.execute(
            "SELECT COUNT(*) FROM suggestions WHERE meeting_id=? AND batch_id=?",
            (meeting_id, row[2]),
        ).fetchone()[0]
        if count > 0 and not row[1]:
            successful += 1
    return {
        "batches": len(rows),
        "successfulBatches": successful,
        "successRate": safe_ratio(successful, len(rows)),
        "latencySec": {
            "average": round(statistics.mean(latencies), 3) if latencies else None,
            "p95": percentile(latencies, 0.95),
            "max": round(max(latencies), 3) if latencies else None,
        },
    }


def minutes_metrics(minutes_text: str | None) -> dict[str, Any]:
    if not minutes_text:
        return {
            "generated": False,
            "evidenceStatus": "not_available",
            "factBoundaryMarkers": 0,
            "evidenceMarkers": 0,
        }
    # Do not persist or print the text.  These are only observability counts;
    # G7 still requires human/semantic review before calling the minutes sound.
    boundary_markers = len(re.findall(r"已确认|待确认|倾向|可能|仍需|尚未", minutes_text))
    evidence_markers = len(re.findall(r"证据|转写|时间点|\bT\d+\b", minutes_text, re.I))
    return {
        "generated": True,
        "evidenceStatus": "markers_present" if evidence_markers else "needs_review",
        "factBoundaryMarkers": boundary_markers,
        "evidenceMarkers": evidence_markers,
    }


def anchor_rows(
    case: dict[str, Any],
    realtime: list[dict[str, Any]],
    offline: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    count = int(case.get("anchorCount") or 0)
    if count <= 0 or len(realtime) < count:
        return []
    indexes = [round(index * (len(realtime) - 1) / (count - 1)) for index in range(count)]
    anchors: list[dict[str, Any]] = []
    for position, index in enumerate(indexes, 1):
        item = realtime[index]
        matched = match_split_item(item, offline)
        anchors.append(
            {
                "anchorId": f"{case['caseId']}-a{position:02d}",
                "relativeStartMs": round(item["start"] * 1000, 1),
                "relativeEndMs": round(item["end"] * 1000, 1),
                "goldIsMe": item["speaker"] == "me",
                "goldRemoteSpeaker": None if item["speaker"] == "me" else "remote_unlabeled",
                "realtimeSpeaker": item["speaker"],
                "offlineSpeaker": matched["speakerId"] if matched else None,
                "offlineIsMeCorrect": (
                    None
                    if matched is None
                    else (matched["speaker"] == item["speaker"])
                ),
                "offlineStartErrorMs": (
                    None if matched is None else round(abs(matched["start"] - item["start"]) * 1000, 1)
                ),
            }
        )
    return anchors


def build_report(db_path: Path, recordings_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    if not db_path.is_file():
        raise FileNotFoundError(f"找不到会议数据库：{db_path}")
    if not recordings_dir.is_dir():
        raise FileNotFoundError(f"找不到录音目录：{recordings_dir}")

    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        columns = {row[1] for row in con.execute("PRAGMA table_info(meetings)")}
        mode_column = "meeting_mode" if "meeting_mode" in columns else None
        rows = con.execute(
            "SELECT id, started_at, ended_at, status, audio_path, audio_seconds, "
            "transcript_mode, transcript_versions_json, minutes_text"
            + (", meeting_mode" if mode_column else "")
            + " FROM meetings ORDER BY started_at"
        ).fetchall()
        by_id = {str(row["id"]): row for row in rows}
        cases: list[dict[str, Any]] = []
        required_missing: list[str] = []
        for meeting_id, spec in KNOWN_CASES.items():
            row = by_id.get(meeting_id)
            audio_file = f"{meeting_id}.wav"
            audio_path = recordings_dir / audio_file
            if row is None or not audio_path.is_file():
                required_missing.append(spec["caseId"])
                cases.append(
                    {
                        "caseId": spec["caseId"],
                        "meetingId": meeting_id,
                        "scene": spec["scene"],
                        "mode": spec["mode"],
                        "present": False,
                        "audioFile": audio_file,
                        "purpose": spec["purpose"],
                    }
                )
                continue
            duration = read_wav_duration(audio_path)
            db_duration = float(row["audio_seconds"]) if _valid_number(row["audio_seconds"]) else None
            duration = duration or db_duration or 0.0
            versions = load_versions(row)
            if not versions.get("realtime"):
                # Legacy records store the current transcript in the table.
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
            realtime = version_items(versions.get("realtime"), float(row["started_at"]), duration)
            offline = version_items(versions.get("offline"), float(row["started_at"]), duration)
            current_speakers = sorted({item["speakerId"] for item in offline if item["speakerId"]})
            actual_mode = (
                str(row["meeting_mode"])
                if mode_column and row["meeting_mode"]
                else spec["mode"]
            )
            timeline = timeline_metrics(realtime, offline) if offline else {
                "matchedItems": 0,
                "startMs": {},
                "endMs": {},
                "combinedMs": {},
            }
            anchors = anchor_rows(spec, realtime, offline) if spec.get("anchorCount") else []
            case = {
                "caseId": spec["caseId"],
                "meetingId": meeting_id,
                "scene": spec["scene"],
                "mode": actual_mode,
                "present": True,
                "status": str(row["status"]),
                "audioFile": audio_file,
                "durationSec": round(duration, 3),
                "dbAudioSeconds": db_duration,
                "realtimeFinalItems": len(realtime),
                "offlineFinalItems": len(offline),
                "offlineSpeakerIds": current_speakers,
                "offlineSpeakerCount": len(current_speakers),
                "expectedParticipants": spec.get("participants"),
                "expectedRemoteClusters": (
                    max(int(spec["participants"]) - 1, 0)
                    if spec.get("mode") == "online" and spec.get("participants")
                    else None
                ),
                "transcriptCoverage": round(
                    safe_ratio(
                        interval_seconds((item["start"], item["end"]) for item in realtime),
                        duration,
                    )
                    or 0.0,
                    4,
                ),
                "meMetrics": me_metrics(realtime, offline) if offline else {},
                "timelineMetrics": timeline,
                "suggestions": suggestion_metrics(con, meeting_id),
                "minutes": minutes_metrics(row["minutes_text"]),
                "anchorCount": len(anchors),
                "anchors": anchors,
                "truthScope": spec["truthScope"],
                "purpose": spec["purpose"],
            }
            cases.append(case)

        # The manifest scans every local recording but keeps unknown samples
        # anonymous and does not expose database titles or transcript text.
        known_ids = set(KNOWN_CASES)
        recordings: list[dict[str, Any]] = []
        for audio_path in sorted(recordings_dir.glob("*.wav")):
            meeting_id = audio_path.stem
            row = by_id.get(meeting_id)
            duration = read_wav_duration(audio_path)
            recordings.append(
                {
                    "meetingId": meeting_id,
                    "audioFile": audio_path.name,
                    "durationSec": duration,
                    "inDatabase": row is not None,
                    "knownCase": meeting_id in known_ids,
                    "status": None if row is None else str(row["status"]),
                    "mode": (
                        str(row["meeting_mode"])
                        if mode_column and row is not None and row["meeting_mode"]
                        else KNOWN_CASES.get(meeting_id, {}).get("mode", "unknown")
                    ),
                }
            )
        report = {
            "schemaVersion": 1,
            "generatedAt": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "privacy": "仅含匿名会议 ID、录音文件名、相对时间和聚合指标；不含标题、正文、姓名、密钥或音频副本。",
            "scope": "M4 G1 real meeting evaluation",
            "requiredCases": [spec["caseId"] for spec in KNOWN_CASES.values()],
            "requiredMissing": required_missing,
            "cases": cases,
            "recordings": recordings,
            "gate": {
                "passed": not required_missing
                and next(
                    (case["anchorCount"] >= int(KNOWN_CASES[mid]["anchorCount"])
                     for mid, case in ((c["meetingId"], c) for c in cases)
                     if mid == "meeting-1785740272352-mcvn6u"),
                    False,
                ),
                "reasons": ([f"缺少核心样本：{', '.join(required_missing)}"] if required_missing else []),
            },
        }
        # Add the reason separately so a missing/short online anchor set is
        # visible without embedding any transcript content.
        online_case = next(
            (case for case in cases if case["meetingId"] == "meeting-1785740272352-mcvn6u"),
            None,
        )
        if not online_case.get("present") if online_case else True:
            report["gate"]["reasons"].append("线上 8 月 3 日核心样本不存在")
        elif online_case.get("anchorCount", 0) < 20:
            report["gate"]["passed"] = False
            report["gate"]["reasons"].append("线上核心样本未抽取至少 20 个锚点")
        return report, {
            "manifestVersion": 1,
            "generatedAt": report["generatedAt"],
            "privacy": report["privacy"],
            "cases": [
                {
                    "caseId": case["caseId"],
                    "meetingId": case["meetingId"],
                    "scene": case["scene"],
                    "mode": case["mode"],
                    "participants": case.get("expectedParticipants"),
                    "audioFile": case.get("audioFile"),
                    "durationSec": case.get("durationSec"),
                    "purpose": case.get("purpose"),
                    "truthScope": case.get("truthScope"),
                    "anchorCount": case.get("anchorCount", 0),
                }
                for case in cases
            ],
        }
    finally:
        con.close()


def markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# M4 G1 真实录音匿名评测",
        "",
        f"> 生成时间：{report['generatedAt']}",
        "> 只包含匿名 ID、时长、相对时间和指标；不含标题、正文、姓名、密钥或音频副本。",
        "",
        f"**Gate：{'PASS' if report['gate']['passed'] else 'FAIL'}**",
    ]
    if report["gate"]["reasons"]:
        lines.extend(["", "原因："])
        lines.extend(f"- {reason}" for reason in report["gate"]["reasons"])
    lines.extend(
        [
            "",
            "| 样本 | 场景 | 时长 | 实时 final | 会后 final | 会后人数 | 我 precision | 我 recall | 时间轴 P95 | 建议 P95 |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for case in report["cases"]:
        if not case.get("present"):
            lines.append(f"| {case['caseId']} | {case['scene']} | 缺失 | — | — | — | — | — | — | — |")
            continue
        me = case.get("meMetrics") or {}
        timeline = case.get("timelineMetrics", {}).get("combinedMs", {})
        latency = case.get("suggestions", {}).get("latencySec", {})
        def pct(value: Any) -> str:
            return "—" if value is None else f"{float(value):.1%}"
        def num(value: Any, suffix: str = "") -> str:
            return "—" if value is None else f"{float(value):.0f}{suffix}"
        lines.append(
            f"| {case['caseId']} | {case['scene']} | {case['durationSec']:.1f}s | "
            f"{case['realtimeFinalItems']} | {case['offlineFinalItems']} | {case['offlineSpeakerCount']} | "
            f"{pct(me.get('mePrecision'))} | {pct(me.get('meRecall'))} | "
            f"{num(timeline.get('p95'), 'ms')} | {num(latency.get('p95'), 's')} |"
        )
    lines.extend(
        [
            "",
            "## 口径",
            "",
            "- “我”的 precision/recall 以实时版本的麦克风 `me` 区间为事实基线；这是线上身份安全基线，不是远端多人真值。",
            "- 线上 8 月 3 日样本的远端两人没有逐段人工标签，锚点中的远端统一记为 `remote_unlabeled`。",
            "- 时间轴误差只比较有实时/会后对应 ID（含拆句后缀）的区间；旧记录缺精确录音轴时显示为缺失。",
            "- 纪要指标只统计事实边界/证据标记数量，`needs_review` 仍需 G7 的语义复核，不能视为已验收。",
            "",
            "## 线上锚点",
            "",
        ]
    )
    online = next(
        (case for case in report["cases"] if case.get("caseId") == "online-20260803-3p"),
        None,
    )
    if online and online.get("anchors"):
        lines.append("| 锚点 | 相对开始 | 实时 | 会后 | 会后我归属是否一致 | 起点误差 |")
        lines.append("|---|---:|---|---|---|---:|")
        for anchor in online["anchors"]:
            lines.append(
                f"| {anchor['anchorId']} | {anchor['relativeStartMs']:.0f}ms | "
                f"{anchor['realtimeSpeaker']} | {anchor.get('offlineSpeaker') or '—'} | "
                f"{anchor.get('offlineIsMeCorrect') if anchor.get('offlineIsMeCorrect') is not None else '—'} | "
                f"{anchor.get('offlineStartErrorMs') if anchor.get('offlineStartErrorMs') is not None else '—'}ms |"
            )
    else:
        lines.append("线上核心样本缺失，未生成锚点。")
    lines.append("")
    return "\n".join(lines)


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="M4 G1 匿名真实录音评测")
    parser.add_argument("--db", default=str(user_data_dir() / "meeting-copilot.sqlite"))
    parser.add_argument("--recordings", default=str(user_data_dir() / "recordings"))
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    args = parser.parse_args()

    report, manifest = build_report(Path(args.db), Path(args.recordings))
    out_dir = Path(args.out_dir)
    write_json(out_dir / "manifest.json", manifest)
    write_json(out_dir / "report.json", report)
    (out_dir / "report.md").write_text(markdown_report(report), encoding="utf-8")
    print(
        f"real meeting evaluation: cases={len(report['cases'])} "
        f"recordings={len(report['recordings'])} "
        f"gate={'PASS' if report['gate']['passed'] else 'FAIL'} "
        f"json={out_dir / 'report.json'}"
    )
    for reason in report["gate"]["reasons"]:
        print(f"  - {reason}")
    if not report["gate"]["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
