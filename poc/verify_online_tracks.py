"""Privacy-safe verifier for a newly recorded online three-track meeting.

The verifier reads WAV headers and aggregate PCM energy only.  It never copies,
prints, or persists audio samples.  It is intentionally a separate gate from
the historical mixed-only evaluator: an old ``.wav`` file can never satisfy
the new online track requirement.
"""

from __future__ import annotations

import argparse
import array
import datetime as dt
import json
import math
import os
import re
import sqlite3
import sys
import wave
from pathlib import Path
from typing import Any


SAFE_ID = re.compile(r"^[a-zA-Z0-9_-]{8,80}$")
HERE = Path(__file__).resolve().parent
DEFAULT_OUT_DIR = HERE / "eval" / "real_meeting_runs"
TRACK_NAMES = ("mixed", "mic", "system")


def user_data_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise RuntimeError("找不到 APPDATA，无法定位桌面端数据目录")
    return Path(appdata) / "meeting-copilot-desktop"


def _samples(raw: bytes) -> array.array:
    values = array.array("h")
    values.frombytes(raw)
    if sys.byteorder != "little":
        values.byteswap()
    return values


def _track_stats(path: Path) -> dict[str, Any]:
    with wave.open(str(path), "rb") as wav:
        channels = wav.getnchannels()
        width = wav.getsampwidth()
        rate = wav.getframerate()
        frames = wav.getnframes()
        if channels != 1 or width != 2:
            # Still return format metadata; the caller will fail the gate.
            return {
                "present": True,
                "sampleRate": rate,
                "channels": channels,
                "sampleWidth": width,
                "frames": frames,
                "durationSec": round(frames / max(rate, 1), 3),
                "rms": None,
                "activeFraction": None,
            }
        total_sq = 0.0
        total = 0
        active_chunks = 0
        chunks = 0
        while True:
            values = _samples(wav.readframes(16_000))
            if not values:
                break
            chunks += 1
            chunk_sq = sum(float(value) * float(value) for value in values)
            total_sq += chunk_sq
            total += len(values)
            chunk_rms = math.sqrt(chunk_sq / len(values)) / 32768.0
            if chunk_rms > 0.01:
                active_chunks += 1
        return {
            "present": True,
            "sampleRate": rate,
            "channels": channels,
            "sampleWidth": width,
            "frames": frames,
            "durationSec": round(frames / max(rate, 1), 3),
            "rms": round(math.sqrt(total_sq / total) / 32768.0, 6)
            if total
            else 0.0,
            "activeFraction": round(active_chunks / chunks, 4) if chunks else 0.0,
        }


def _cross_track_stats(mic_path: Path, system_path: Path) -> dict[str, Any]:
    """Return only aggregate aligned energy/correlation, never PCM samples."""
    with wave.open(str(mic_path), "rb") as mic, wave.open(str(system_path), "rb") as system:
        if (
            mic.getnchannels() != 1
            or system.getnchannels() != 1
            or mic.getsampwidth() != 2
            or system.getsampwidth() != 2
            or mic.getframerate() != system.getframerate()
        ):
            return {"alignedFrames": 0, "absoluteCorrelation": None}
        sum_mic_sq = 0.0
        sum_system_sq = 0.0
        sum_product = 0.0
        count = 0
        while True:
            mic_values = _samples(mic.readframes(16_000))
            system_values = _samples(system.readframes(16_000))
            if not mic_values or not system_values:
                break
            length = min(len(mic_values), len(system_values))
            for left, right in zip(mic_values[:length], system_values[:length]):
                left_value = float(left)
                right_value = float(right)
                sum_mic_sq += left_value * left_value
                sum_system_sq += right_value * right_value
                sum_product += left_value * right_value
            count += length
        denominator = math.sqrt(sum_mic_sq * sum_system_sq)
        correlation = abs(sum_product / denominator) if denominator else None
        return {
            "alignedFrames": count,
            "absoluteCorrelation": None if correlation is None else round(correlation, 4),
        }


def _recording_paths(recordings_dir: Path, meeting_id: str) -> dict[str, Path]:
    if not SAFE_ID.fullmatch(meeting_id):
        raise ValueError("会议 ID 格式无效")
    return {
        "mixed": recordings_dir / f"{meeting_id}.wav",
        "mic": recordings_dir / f"{meeting_id}.mic.wav",
        "system": recordings_dir / f"{meeting_id}.system.wav",
    }


def _candidate_ids(db_path: Path, recordings_dir: Path, requested: str | None) -> list[str]:
    if requested:
        return [requested]
    if not db_path.is_file():
        return []
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        columns = {row[1] for row in con.execute("PRAGMA table_info(meetings)")}
        if "meeting_mode" not in columns:
            return []
        rows = con.execute(
            "SELECT id FROM meetings WHERE meeting_mode='online' ORDER BY started_at DESC"
        ).fetchall()
        return [str(row[0]) for row in rows if SAFE_ID.fullmatch(str(row[0]))]
    finally:
        con.close()


def inspect_meeting(recordings_dir: Path, meeting_id: str) -> dict[str, Any]:
    paths = _recording_paths(recordings_dir, meeting_id)
    missing = [name for name, path in paths.items() if not path.is_file()]
    if missing:
        return {
            "meetingId": meeting_id,
            "present": False,
            "missingTracks": missing,
            "gatePassed": False,
        }
    try:
        stats = {name: _track_stats(path) for name, path in paths.items()}
        durations = [float(stats[name]["durationSec"]) for name in TRACK_NAMES]
        formats_match = len(
            {
                (
                    stats[name]["sampleRate"],
                    stats[name]["channels"],
                    stats[name]["sampleWidth"],
                )
                for name in TRACK_NAMES
            }
        ) == 1 and stats["mixed"]["channels"] == 1 and stats["mixed"]["sampleWidth"] == 2
        duration_delta_ms = round((max(durations) - min(durations)) * 1000.0, 3)
        nonempty = all(stats[name]["frames"] > 0 for name in TRACK_NAMES)
        cross = _cross_track_stats(paths["mic"], paths["system"])
        gate_passed = formats_match and nonempty and duration_delta_ms <= 100.0
        return {
            "meetingId": meeting_id,
            "present": True,
            "tracks": stats,
            "durationDeltaMs": duration_delta_ms,
            "formatsMatch": formats_match,
            "nonempty": nonempty,
            "alignedEnergy": cross,
            "manualIsolationReviewRequired": True,
            "manualIsolationNote": (
                "需结合实际发言回放确认麦克风无明显远端主声、系统音轨无明显本地主声；"
                "本报告只保存聚合能量与相关性，不保存音频。"
            ),
            "gatePassed": gate_passed,
        }
    except (OSError, wave.Error, ValueError) as exc:
        return {
            "meetingId": meeting_id,
            "present": True,
            "gatePassed": False,
            "error": str(exc)[:300],
        }


def build_report(
    db_path: Path,
    recordings_dir: Path,
    meeting_id: str | None = None,
    require: bool = False,
) -> dict[str, Any]:
    candidates = _candidate_ids(db_path, recordings_dir, meeting_id)
    inspections = [inspect_meeting(recordings_dir, item) for item in candidates]
    passing = [item for item in inspections if item.get("gatePassed")]
    passed = bool(passing) if require else True
    reasons: list[str] = []
    if not candidates:
        reasons.append("未找到线上会议的 mixed/mic/system 三份 WAV")
    elif not passing:
        reasons.append("找到线上候选，但没有一组通过格式、非空和 100ms 时长差门槛")
    return {
        "schemaVersion": 1,
        "generatedAt": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "privacy": "仅含匿名会议 ID、WAV 参数和聚合声学指标；不含音频副本、转写正文、标题或密钥。",
        "scope": "M4 G2 online three-track acceptance",
        "required": require,
        "gate": {"passed": passed, "reasons": reasons},
        "candidates": inspections,
        "manualReview": "三轨隔离质量仍需人工听感/设备场景确认；自动门禁只验证文件生命周期和同步性。",
    }


def markdown_report(report: dict[str, Any]) -> str:
    gate = "PASS" if report["gate"]["passed"] else "FAIL"
    lines = [
        "# 线上三音轨匿名验收",
        "",
        f"> 生成时间：{report['generatedAt']}",
        f"> Gate：**{gate}**",
        "> 不含音频副本、转写正文、标题或密钥。",
        "",
    ]
    for reason in report["gate"]["reasons"]:
        lines.append(f"- {reason}")
    lines.extend(["", "| 会议 | 文件 | 时长差 | 格式 | 非空 | 自动结果 |", "|---|---|---:|---|---|---|"])
    for item in report["candidates"]:
        if not item.get("present"):
            lines.append(
                f"| {item['meetingId']} | 缺少 {','.join(item.get('missingTracks', []))} | — | — | — | FAIL |"
            )
            continue
        lines.append(
            f"| {item['meetingId']} | mixed/mic/system | "
            f"{item.get('durationDeltaMs', '—')}ms | "
            f"{item.get('formatsMatch', False)} | {item.get('nonempty', False)} | "
            f"{'PASS' if item.get('gatePassed') else 'FAIL'} |"
        )
    lines.extend(["", "## 人工复核", "", report["manualReview"], ""])
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="线上三音轨隐私安全验收")
    parser.add_argument("--db", default=str(user_data_dir() / "meeting-copilot.sqlite"))
    parser.add_argument("--recordings", default=str(user_data_dir() / "recordings"))
    parser.add_argument("--meeting-id")
    parser.add_argument("--require-triple-track", action="store_true")
    parser.add_argument("--out-json", default=str(DEFAULT_OUT_DIR / "online_tracks.json"))
    parser.add_argument("--out-md", default=str(DEFAULT_OUT_DIR / "online_tracks.md"))
    args = parser.parse_args()

    report = build_report(
        Path(args.db),
        Path(args.recordings),
        meeting_id=args.meeting_id,
        require=args.require_triple_track,
    )
    json_path = Path(args.out_json)
    md_path = Path(args.out_md)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(markdown_report(report), encoding="utf-8")
    print(
        f"online track verification: candidates={len(report['candidates'])} "
        f"gate={'PASS' if report['gate']['passed'] else 'FAIL'} json={json_path}"
    )
    for reason in report["gate"]["reasons"]:
        print(f"  - {reason}")
    if not report["gate"]["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
