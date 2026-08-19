"""G4 时间轴自动量化（隐私安全，不写转写正文）。

对本地 SQLite 中的 final 行计算：
  - 墙上到达时间相对录音起点 vs 显式 PCM 轴（audioStartMs/audioEndMs）的滞后
  - 实时 vs 会后同源 ID 的起点/终点误差
  - 均匀抽取最多 N 个锚点，便于对照人工跳播

用法::

    python eval_timeline_axis.py
    python eval_timeline_axis.py --meeting-id meeting-xxx --anchors 15
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import sqlite3
from pathlib import Path
from typing import Any, Sequence

from eval_real_meetings import (
    load_versions,
    percentile,
    read_wav_duration,
    timeline_metrics,
    user_data_dir,
    version_items,
)

HERE = Path(__file__).resolve().parent
DEFAULT_OUT = HERE / "eval" / "real_meeting_runs" / "timeline_axis.json"


def _valid(value: Any) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def lag_stats(
    items: list[dict[str, Any]],
    started_at: float,
) -> dict[str, Any]:
    """lag_ms ≈ (at - started_at) - audioEndMs，正值表示转写晚于录音轴。"""
    start_lags: list[float] = []
    end_lags: list[float] = []
    explicit = 0
    for item in items:
        raw = item.get("_raw") or {}
        if not _valid(raw.get("audioStartMs")) or not _valid(raw.get("audioEndMs")):
            continue
        if not _valid(raw.get("at")) or not started_at:
            continue
        explicit += 1
        wall = float(raw["at"]) - float(started_at)
        start_lags.append(wall - float(raw["audioStartMs"]))
        end_lags.append(wall - float(raw["audioEndMs"]))

    def pack(values: list[float]) -> dict[str, Any]:
        if not values:
            return {
                "count": 0,
                "median": None,
                "p90": None,
                "p95": None,
                "max": None,
                "within1s": None,
                "within2s": None,
            }
        abs_values = [abs(v) for v in values]
        return {
            "count": len(values),
            "median": percentile(abs_values, 0.5),
            "p90": percentile(abs_values, 0.9),
            "p95": percentile(abs_values, 0.95),
            "max": round(max(abs_values), 3),
            "signedMedian": percentile(values, 0.5),
            "within1s": round(sum(1 for v in abs_values if v <= 1000) / len(abs_values), 4),
            "within2s": round(sum(1 for v in abs_values if v <= 2000) / len(abs_values), 4),
        }

    return {
        "explicitRanges": explicit,
        "wallMinusAudioStartMs": pack(start_lags),
        "wallMinusAudioEndMs": pack(end_lags),
        "note": (
            "wallMinusAudioEndMs 近似识别延迟；播放高亮若未校正，"
            "人耳会感觉高亮偏早约该滞后的量级。"
        ),
    }


def monotonic_checks(items: list[dict[str, Any]]) -> dict[str, Any]:
    starts = [item["start"] for item in items]
    ends = [item["end"] for item in items]
    start_regressions = sum(
        1 for i in range(1, len(starts)) if starts[i] + 1e-6 < starts[i - 1]
    )
    negative_duration = sum(1 for item in items if item["end"] + 1e-6 < item["start"])
    return {
        "items": len(items),
        "startRegressions": start_regressions,
        "negativeDuration": negative_duration,
        "ok": start_regressions == 0 and negative_duration == 0,
    }


def sample_anchors(items: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    if not items or count <= 0:
        return []
    count = min(count, len(items))
    if count == 1:
        indexes = [0]
    else:
        indexes = [
            round(i * (len(items) - 1) / (count - 1)) for i in range(count)
        ]
    anchors = []
    for position, index in enumerate(indexes, 1):
        item = items[index]
        anchors.append(
            {
                "anchorIndex": position,
                "itemIdHash": abs(hash(item["id"])) % 10_000_000,
                "relativeStartMs": round(item["start"] * 1000, 1),
                "relativeEndMs": round(item["end"] * 1000, 1),
                "speaker": item["speaker"],
                "durationMs": round((item["end"] - item["start"]) * 1000, 1),
            }
        )
    return anchors


def enrich_version_items(
    version: dict[str, Any] | None,
    started_at: float,
    duration: float,
) -> list[dict[str, Any]]:
    """Like version_items but keeps raw fields for lag calculation."""
    base = version_items(version, started_at, duration)
    if not isinstance(version, dict):
        return base
    by_id = {
        str(raw.get("id")): raw
        for raw in (version.get("transcript") or [])
        if isinstance(raw, dict) and raw.get("id")
    }
    for item in base:
        item["_raw"] = by_id.get(item["id"], {})
    return base


def analyze_meeting(
    con: sqlite3.Connection,
    recordings_dir: Path,
    meeting_id: str,
    anchors: int,
) -> dict[str, Any]:
    row = con.execute(
        "SELECT id, started_at, ended_at, status, audio_seconds, "
        "transcript_mode, transcript_versions_json, meeting_mode "
        "FROM meetings WHERE id=?",
        (meeting_id,),
    ).fetchone()
    if row is None:
        return {"meetingId": meeting_id, "present": False}

    audio_path = recordings_dir / f"{meeting_id}.wav"
    duration = read_wav_duration(audio_path) if audio_path.is_file() else None
    if duration is None and row["audio_seconds"] is not None:
        duration = float(row["audio_seconds"])
    duration = float(duration or 0.0)
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

    realtime = enrich_version_items(versions.get("realtime"), started_at, duration)
    offline = enrich_version_items(versions.get("offline"), started_at, duration)
    preferred = realtime if realtime else offline
    lag = lag_stats(preferred, started_at)
    mono = monotonic_checks(preferred)
    rt_offline = timeline_metrics(realtime, offline) if realtime and offline else {
        "matchedItems": 0,
        "combinedMs": {},
    }
    sample = sample_anchors(preferred, anchors)

    end_lag = lag["wallMinusAudioEndMs"]
    # Soft gate focuses on axis integrity, not ASR delivery latency.
    # wallMinusAudioEnd is typically multi-second (model RTF) and is reported
    # only as an observation for playback offset calibration.
    soft_pass = True
    reasons: list[str] = []
    if not mono["ok"]:
        soft_pass = False
        reasons.append(
            f"非单调：startRegressions={mono['startRegressions']} "
            f"negativeDuration={mono['negativeDuration']}"
        )
    explicit_ratio = (
        end_lag["count"] / max(len(preferred), 1) if preferred else 0.0
    )
    if preferred and end_lag["count"] < 10:
        reasons.append("显式录音轴样本不足 10 条，仅输出观测值")
    elif preferred and explicit_ratio < 0.5:
        soft_pass = False
        reasons.append(f"显式轴覆盖 {explicit_ratio:.0%} < 50%")

    combined = (rt_offline.get("combinedMs") or {}) if isinstance(rt_offline, dict) else {}
    matched = int(rt_offline.get("matchedItems") or 0) if isinstance(rt_offline, dict) else 0
    if matched >= 20:
        # realtime vs offline inheritance/split consistency
        p90 = combined.get("p90")
        if p90 is not None and float(p90) > 2000:
            soft_pass = False
            reasons.append(f"实时/会后轴 combined P90={p90}ms > 2000ms")
    elif offline and realtime:
        reasons.append(f"实时/会后可配对仅 {matched} 条，跳过 P90 门槛")

    if end_lag["count"] >= 10:
        reasons.append(
            f"观测：ASR 送达滞后(wall-audioEnd) P50={end_lag['median']}ms "
            f"P90={end_lag['p90']}ms（用于偏移校准，不单独作为 G4 失败条件）"
        )

    return {
        "meetingId": meeting_id,
        "present": True,
        "status": row["status"],
        "mode": row["meeting_mode"],
        "durationSec": round(duration, 3),
        "realtimeItems": len(realtime),
        "offlineItems": len(offline),
        "explicitCoverage": round(explicit_ratio, 4),
        "lag": lag,
        "monotonic": mono,
        "realtimeVsOffline": rt_offline,
        "anchors": sample,
        "softGate": {"passed": soft_pass, "reasons": reasons},
    }

def list_candidate_ids(con: sqlite3.Connection, min_seconds: float) -> list[str]:
    rows = con.execute(
        "SELECT id, audio_seconds FROM meetings "
        "WHERE status IN ('completed','interrupted') "
        "ORDER BY COALESCE(started_at,0) DESC"
    ).fetchall()
    out = []
    for row in rows:
        if row["audio_seconds"] is not None and float(row["audio_seconds"]) >= min_seconds:
            out.append(str(row["id"]))
    return out


def build_report(
    db_path: Path,
    recordings_dir: Path,
    meeting_ids: Sequence[str] | None,
    anchors: int,
    min_seconds: float,
) -> dict[str, Any]:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        ids = list(meeting_ids) if meeting_ids else list_candidate_ids(con, min_seconds)
        cases = [analyze_meeting(con, recordings_dir, mid, anchors) for mid in ids]
        soft_fail = [c for c in cases if c.get("present") and not c.get("softGate", {}).get("passed")]
        mono_fail = [
            c
            for c in cases
            if c.get("present") and not (c.get("monotonic") or {}).get("ok", True)
        ]
        long_pass = [
            c
            for c in cases
            if c.get("present")
            and c.get("softGate", {}).get("passed")
            and float(c.get("durationSec") or 0) >= 2400
            and float(c.get("explicitCoverage") or 0) >= 0.5
        ]
        # Overall engineering gate: no monotonic corruption, and at least one
        # long meeting with explicit PCM axis passes. Legacy offline rewrites
        # may still fail realtime/offline P90 and are listed as residuals.
        overall_pass = bool(cases) and not mono_fail and bool(long_pass)
        reasons: list[str] = []
        if not cases:
            reasons.append("没有可量化会议")
        if mono_fail:
            reasons.append(
                "存在非单调时间轴：" + ", ".join(c["meetingId"] for c in mono_fail)
            )
        if not long_pass:
            reasons.append("没有 ≥40 分钟且 soft PASS 的显式轴长会")
        for case in soft_fail:
            reasons.append(
                f"{case['meetingId']}: {', '.join(case.get('softGate', {}).get('reasons') or [])}"
            )
        return {
            "schemaVersion": 1,
            "generatedAt": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "privacy": "仅含匿名会议 ID、相对时间和聚合误差；不含转写正文、标题或音频。",
            "scope": "M4 G4 timeline axis quantification",
            "cases": cases,
            "gate": {
                "passed": overall_pass,
                "reasons": reasons,
                "longMeetingsSoftPass": [c["meetingId"] for c in long_pass],
                "note": (
                    "工程门禁：无非单调轴 + 至少一场 ≥40 分钟显式轴 soft PASS；"
                    "旧混音会后重写造成的实时/会后 P90 偏差记为残留。"
                    "人工点击跳播听感仍是 G4 关闭前的必要补充。"
                ),
            },
        }
    finally:
        con.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="G4 timeline axis quantifier")
    parser.add_argument("--db", default=str(user_data_dir() / "meeting-copilot.sqlite"))
    parser.add_argument("--recordings", default=str(user_data_dir() / "recordings"))
    parser.add_argument("--meeting-id", action="append", dest="meeting_ids")
    parser.add_argument("--anchors", type=int, default=15)
    parser.add_argument("--min-seconds", type=float, default=600.0)
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--out-md", default="")
    args = parser.parse_args(argv)

    report = build_report(
        Path(args.db),
        Path(args.recordings),
        args.meeting_ids,
        args.anchors,
        args.min_seconds,
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    lines = [
        "# G4 时间轴自动量化",
        "",
        f"> 生成时间：{report['generatedAt']}",
        f"> Gate：**{'PASS' if report['gate']['passed'] else 'OBSERVE/FAIL'}**",
        "",
        report["gate"].get("note", ""),
        "",
        f"- 长会 soft PASS：{', '.join(report['gate'].get('longMeetingsSoftPass') or []) or '无'}",
        "",
        "| 会议 | 时长 | 显式覆盖 | 实时/会后P90ms | ASR滞后P50ms | 单调 | soft |",
        "|---|---:|---:|---:|---:|---|---|",
    ]
    for case in report["cases"]:
        if not case.get("present"):
            lines.append(f"| {case['meetingId']} | 缺失 | — | — | — | — | — |")
            continue
        lag = case["lag"]["wallMinusAudioEndMs"]
        p50 = "—" if lag["median"] is None else f"{lag['median']:.0f}"
        rt_p90 = (case.get("realtimeVsOffline") or {}).get("combinedMs", {}).get("p90")
        rt_cell = "—" if rt_p90 is None else f"{float(rt_p90):.0f}"
        cov = case.get("explicitCoverage")
        cov_cell = "—" if cov is None else f"{float(cov)*100:.0f}%"
        lines.append(
            f"| {case['meetingId']} | {case['durationSec']:.0f}s | {cov_cell} | "
            f"{rt_cell} | {p50} | "
            f"{'✓' if case['monotonic']['ok'] else '✗'} | "
            f"{'PASS' if case['softGate']['passed'] else 'FAIL'} |"
        )
    if report["gate"]["reasons"]:
        lines.extend(["", "## 原因"])
        lines.extend(f"- {reason}" for reason in report["gate"]["reasons"])
    md = "\n".join(lines) + "\n"
    md_path = Path(args.out_md) if args.out_md else out.with_suffix(".md")
    md_path.write_text(md, encoding="utf-8")
    print(
        f"timeline axis: cases={len(report['cases'])} "
        f"gate={'PASS' if report['gate']['passed'] else 'FAIL'} "
        f"out={out}"
    )
    return 0 if report["gate"]["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
