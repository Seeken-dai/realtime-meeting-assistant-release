"""真实会议的「我 / 非我」说话人回归。

默认基准只保存匿名条目 id、时间和 isMe，不保存会议标题或正文。原始录音继续留在
Electron userData，不复制进仓库。

用法：
    # 从当前人工修正后的数据库状态抓取一次基准
    python eval_speaker_regression.py capture --meeting-id meeting-...

    # 重新跑完整离线分离并评估
    python eval_speaker_regression.py run

    # 复用先前保存的本地结果，只重算指标
    python eval_speaker_regression.py run --reuse-result eval/speaker_regression_runs/xxx.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

from diarize_offline import (
    DEFAULT_CLUSTER_TH,
    _estimate_offset,
    diarize,
)


HERE = Path(__file__).resolve().parent
DEFAULT_FIXTURE = HERE / "eval" / "speaker_regression_20260729_1503.json"
DEFAULT_RUN_DIR = HERE / "eval" / "speaker_regression_runs"


def user_data_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise RuntimeError("找不到 APPDATA，无法定位桌面端数据目录")
    return Path(appdata) / "meeting-copilot-desktop"


def default_db_path() -> Path:
    return user_data_dir() / "meeting-copilot.sqlite"


def read_json(path: Path) -> Dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        value = json.load(f)
    if not isinstance(value, dict):
        raise ValueError(f"JSON 顶层必须是对象：{path}")
    return value


def write_json(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write("\n")


def capture_fixture(db_path: Path, meeting_id: str, out_path: Path) -> Dict[str, Any]:
    if not db_path.is_file():
        raise FileNotFoundError(f"找不到会议数据库：{db_path}")
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        meeting = con.execute(
            """
            SELECT id, started_at, audio_path, audio_seconds
            FROM meetings WHERE id = ?
            """,
            (meeting_id,),
        ).fetchone()
        if not meeting:
            raise ValueError(f"数据库中找不到会议：{meeting_id}")
        rows = con.execute(
            """
            SELECT id, at, speaker_id
            FROM transcripts
            WHERE meeting_id = ? AND is_final = 1
            ORDER BY at, id
            """,
            (meeting_id,),
        ).fetchall()
    finally:
        con.close()

    labels = [
        {"id": str(row[0]), "at": round(float(row[1]), 3), "isMe": row[2] == "me"}
        for row in rows
    ]
    me_count = sum(1 for item in labels if item["isMe"])
    if not labels or me_count == 0 or me_count == len(labels):
        raise ValueError("基准必须同时包含「我」和「非我」条目")

    audio_path = Path(meeting[2]) if meeting[2] else None
    fixture = {
        "schemaVersion": 1,
        "meetingId": meeting_id,
        "startedAt": int(meeting[1]),
        "audioFile": audio_path.name if audio_path else f"{meeting_id}.wav",
        "audioSeconds": None if meeting[3] is None else round(float(meeting[3]), 3),
        "scope": "me-vs-other",
        "source": "历史会议中人工粗校正后的说话人归属",
        "limitations": (
            "只把「我 / 非我」当作回归真值；其他说话人没有逐段精标。"
            "人工操作是整簇合并，因此本基准适合防止『同一个我被拆成两人』回归，"
            "不代表逐字逐秒的法证级标注。"
        ),
        "privacy": "不含会议标题、转写正文、说话人姓名或录音内容。",
        "labelStats": {
            "items": len(labels),
            "meItems": me_count,
            "otherItems": len(labels) - me_count,
        },
        "expectations": {
            "meDecision": "cluster",
            "maxSpeakerCount": 7,
            "minItemAccuracy": 0.98,
            "minItemMeRecall": 0.97,
            "minSpeechAccuracy": 0.99,
            "minSpeechMeRecall": 0.99,
        },
        "labels": labels,
    }
    write_json(out_path, fixture)
    return fixture


def overlap_seconds(start: float, end: float, seg: Dict[str, Any]) -> float:
    return max(0.0, min(end, float(seg["end"])) - max(start, float(seg["start"])))


def nearest_prediction(segments: List[Dict[str, Any]], at: float) -> bool:
    covering = [
        seg
        for seg in segments
        if float(seg["start"]) - 0.2 <= at <= float(seg["end"]) + 0.4
    ]
    if covering:
        best = max(covering, key=lambda seg: float(seg["end"]) - float(seg["start"]))
    else:
        best = min(segments, key=lambda seg: abs(float(seg["end"]) - at))
    return best.get("speakerId") == "me"


def safe_div(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator else 0.0


def rounded_metrics(confusion: Dict[str, float]) -> Dict[str, float]:
    tp = confusion["tp"]
    tn = confusion["tn"]
    fp = confusion["fp"]
    fn = confusion["fn"]
    total = tp + tn + fp + fn
    precision = safe_div(tp, tp + fp)
    recall = safe_div(tp, tp + fn)
    return {
        "accuracy": round(safe_div(tp + tn, total), 4),
        "mePrecision": round(precision, 4),
        "meRecall": round(recall, 4),
        "meF1": round(safe_div(2 * precision * recall, precision + recall), 4),
    }


def evaluate_segments(
    fixture: Dict[str, Any], diarization: Dict[str, Any]
) -> Dict[str, Any]:
    labels = sorted(fixture["labels"], key=lambda item: (float(item["at"]), item["id"]))
    segments = sorted(
        diarization.get("segments") or [], key=lambda seg: (float(seg["start"]), float(seg["end"]))
    )
    if not segments:
        raise ValueError("分离结果没有可评估的语音段")

    started_at = float(fixture["startedAt"])
    dummy_transcript = [
        {"id": item["id"], "at": item["at"], "text": "x", "isFinal": True}
        for item in labels
    ]
    duration = float(diarization.get("durationSec") or fixture.get("audioSeconds") or 0.0)
    offset = _estimate_offset(segments, dummy_transcript, started_at, duration)

    item_confusion = {key: 0.0 for key in ("tp", "tn", "fp", "fn")}
    speech_confusion = {key: 0.0 for key in ("tp", "tn", "fp", "fn")}
    errors: List[Dict[str, Any]] = []
    previous_end = 0.0

    for item in labels:
        end = max(previous_end, min(duration, (float(item["at"]) - started_at) / 1000.0 - offset))
        start = previous_end
        previous_end = end
        me_seconds = sum(
            overlap_seconds(start, end, seg)
            for seg in segments
            if seg.get("speakerId") == "me"
        )
        other_seconds = sum(
            overlap_seconds(start, end, seg)
            for seg in segments
            if seg.get("speakerId") != "me"
        )
        speech_seconds = me_seconds + other_seconds
        predicted_me = (
            me_seconds >= other_seconds
            if speech_seconds > 0
            else nearest_prediction(segments, end)
        )
        gold_me = bool(item["isMe"])
        key = (
            "tp"
            if gold_me and predicted_me
            else "fn"
            if gold_me
            else "fp"
            if predicted_me
            else "tn"
        )
        item_confusion[key] += 1
        speech_confusion[key] += speech_seconds
        if predicted_me != gold_me:
            errors.append(
                {
                    "id": item["id"],
                    "atSec": round(end, 3),
                    "windowStartSec": round(start, 3),
                    "gold": "me" if gold_me else "other",
                    "predicted": "me" if predicted_me else "other",
                    "speechSeconds": round(speech_seconds, 3),
                    "predictedMeShare": round(safe_div(me_seconds, speech_seconds), 4),
                }
            )

    errors.sort(key=lambda item: item["speechSeconds"], reverse=True)
    return {
        "scope": fixture["scope"],
        "offsetSec": round(offset, 3),
        "item": {
            "total": len(labels),
            "confusion": {key: int(value) for key, value in item_confusion.items()},
            **rounded_metrics(item_confusion),
        },
        "speechWeighted": {
            "evaluatedSeconds": round(sum(speech_confusion.values()), 3),
            "confusionSeconds": {
                key: round(value, 3) for key, value in speech_confusion.items()
            },
            **rounded_metrics(speech_confusion),
        },
        "errorCount": len(errors),
        "largestErrors": errors[:30],
    }


def check_expectations(
    fixture: Dict[str, Any],
    diarization: Dict[str, Any],
    evaluation: Dict[str, Any],
) -> Dict[str, Any]:
    expected = fixture.get("expectations") or {}
    failures: List[str] = []

    def require_min(label: str, actual: float, key: str) -> None:
        threshold = expected.get(key)
        if threshold is not None and actual < float(threshold):
            failures.append(f"{label} {actual:.4f} < {float(threshold):.4f}")

    decision = expected.get("meDecision")
    if decision and diarization.get("meDecision") != decision:
        failures.append(
            f"meDecision {diarization.get('meDecision')} != {decision}"
        )
    max_speakers = expected.get("maxSpeakerCount")
    if max_speakers is not None and int(diarization.get("speakerCount") or 0) > int(
        max_speakers
    ):
        failures.append(
            f"speakerCount {diarization.get('speakerCount')} > {int(max_speakers)}"
        )
    require_min(
        "item accuracy",
        float(evaluation["item"]["accuracy"]),
        "minItemAccuracy",
    )
    require_min(
        "item me recall",
        float(evaluation["item"]["meRecall"]),
        "minItemMeRecall",
    )
    require_min(
        "speech accuracy",
        float(evaluation["speechWeighted"]["accuracy"]),
        "minSpeechAccuracy",
    )
    require_min(
        "speech me recall",
        float(evaluation["speechWeighted"]["meRecall"]),
        "minSpeechMeRecall",
    )
    return {"passed": not failures, "failures": failures, "expected": expected}


def resolve_audio(fixture: Dict[str, Any], explicit: str | None) -> Path:
    path = Path(explicit) if explicit else user_data_dir() / "recordings" / fixture["audioFile"]
    if not path.is_file():
        raise FileNotFoundError(f"找不到基准录音：{path}")
    return path


def resolve_enroll(explicit: Iterable[str] | None) -> List[str]:
    if explicit:
        paths = [Path(value) for value in explicit]
    else:
        paths = sorted((user_data_dir() / "voiceprint").glob("*.wav"))
    missing = [path for path in paths if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"找不到注册声纹：{missing[0]}")
    if not paths:
        raise FileNotFoundError("没有可用的注册声纹 wav")
    return [str(path) for path in paths]


def load_reused_diarization(path: Path) -> Dict[str, Any]:
    value = read_json(path)
    diarization = value.get("diarization", value)
    if not isinstance(diarization, dict) or not diarization.get("segments"):
        raise ValueError(f"复用结果中没有 diarization.segments：{path}")
    return diarization


def print_summary(
    evaluation: Dict[str, Any],
    diarization: Dict[str, Any],
    gate: Dict[str, Any],
    out: Path,
) -> None:
    item = evaluation["item"]
    speech = evaluation["speechWeighted"]
    print(
        "speaker regression:"
        f" decision={diarization.get('meDecision')}"
        f" speakers={diarization.get('speakerCount')}"
        f" items={item['total']}"
    )
    print(
        f"  item accuracy={item['accuracy']:.1%}"
        f" me-recall={item['meRecall']:.1%}"
        f" me-precision={item['mePrecision']:.1%}"
    )
    print(
        f"  speech accuracy={speech['accuracy']:.1%}"
        f" me-recall={speech['meRecall']:.1%}"
        f" evaluated={speech['evaluatedSeconds']:.1f}s"
    )
    print(f"  errors={evaluation['errorCount']} output={out}")
    print(f"  gate={'PASS' if gate['passed'] else 'FAIL'}")
    for failure in gate["failures"]:
        print(f"    - {failure}")


def run_regression(args: argparse.Namespace) -> Tuple[Dict[str, Any], Path]:
    fixture_path = Path(args.fixture)
    fixture = read_json(fixture_path)
    if args.reuse_result:
        diarization = load_reused_diarization(Path(args.reuse_result))
        audio_path = None
        enroll_paths: List[str] = []
    else:
        audio_path = resolve_audio(fixture, args.wav)
        enroll_paths = resolve_enroll(args.enroll)
        diarization = diarize(
            str(audio_path),
            enroll_wav=enroll_paths,
            me_threshold=args.me_threshold,
            cluster_th=args.cluster_th,
            min_cluster_segments=args.min_cluster_segments,
            min_cluster_seconds=args.min_cluster_seconds,
        )
        if not diarization.get("ok"):
            raise RuntimeError(diarization.get("error") or "离线分离失败")

    evaluation = evaluate_segments(fixture, diarization)
    gate = check_expectations(fixture, diarization, evaluation)
    if args.out:
        out_path = Path(args.out)
    else:
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = DEFAULT_RUN_DIR / f"{fixture['meetingId']}_{stamp}.json"
    payload = {
        "schemaVersion": 1,
        "fixture": str(fixture_path),
        "meetingId": fixture["meetingId"],
        "ranAt": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "audioPath": None if audio_path is None else str(audio_path),
        "enrollFiles": [Path(path).name for path in enroll_paths],
        "diarization": diarization,
        "evaluation": evaluation,
        "gate": gate,
    }
    write_json(out_path, payload)
    print_summary(evaluation, diarization, gate, out_path)
    return payload, out_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="真实会议「我 / 非我」说话人回归")
    sub = parser.add_subparsers(dest="command", required=True)

    capture = sub.add_parser("capture", help="从当前数据库人工修正状态抓取匿名基准")
    capture.add_argument("--meeting-id", required=True)
    capture.add_argument("--db", default=str(default_db_path()))
    capture.add_argument("--out", default=str(DEFAULT_FIXTURE))

    run = sub.add_parser("run", help="重跑离线分离并与匿名基准比较")
    run.add_argument("--fixture", default=str(DEFAULT_FIXTURE))
    run.add_argument("--wav")
    run.add_argument("--enroll", action="append")
    run.add_argument("--reuse-result")
    run.add_argument("--out")
    run.add_argument("--me-threshold", type=float, default=0.65)
    run.add_argument("--cluster-th", type=float, default=DEFAULT_CLUSTER_TH)
    run.add_argument("--min-cluster-segments", type=int, default=3)
    run.add_argument("--min-cluster-seconds", type=float, default=8.0)
    return parser


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args()
    if args.command == "capture":
        fixture = capture_fixture(Path(args.db), args.meeting_id, Path(args.out))
        print(
            f"captured {fixture['labelStats']['items']} anonymous labels"
            f" -> {args.out}"
        )
    else:
        payload, _ = run_regression(args)
        if not payload["gate"]["passed"]:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
