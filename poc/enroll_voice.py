"""会前声纹注册（非交互）。Electron / CLI 均可调用。

用法：
  python enroll_voice.py --out enroll_me.wav --seconds 20 --device 1
  python enroll_voice.py --status --out enroll_me.wav
"""

import argparse
import json
import os
import sys
import time
import wave

import numpy as np

from mic_stream import MicStream

try:
    import config
except ImportError:
    class config:
        SAMPLE_RATE = 16000
        CHANNELS = 1
        FRAME_MS = 40


PROMPT = """请用【平时开会的音量和语速】连续说话 20 秒，中途不要停。

可以照着念（念不完会自动截断，不影响）：

  今天这场评审，主要把需求范围和排期确认下来。
  审批流程还得再细化，特别是金额分档和多级审批的规则。
  涉及定制开发的部分，工作量评估要重新过一遍，
  不要直接参考上个项目的口径。
  另外，接口对接的时间点，也要跟对方团队再沟通一次。
  内部资源如果排不开，可以先做第一期，剩下的放到后面迭代。

也可以不念稿，直接聊一个真实议题（比如"明天这场会我准备问客户哪几个问题"）——
自然说话的语调更接近开会状态，注册效果通常更好。内容随意，**别停顿**就行。"""


def emit(obj):
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--seconds", type=float, default=20)
    ap.add_argument("--device", type=int, default=None)
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()

    if args.status:
        ok = os.path.isfile(args.out)
        size = os.path.getsize(args.out) if ok else 0
        secs = 0.0
        if ok and size > 44:
            try:
                with wave.open(args.out, "rb") as f:
                    secs = f.getnframes() / float(f.getframerate() or 16000)
            except Exception:
                secs = 0.0
        emit({"ok": ok, "path": args.out, "bytes": size, "seconds": round(secs, 1)})
        return

    parent = os.path.dirname(os.path.abspath(args.out))
    if parent:
        os.makedirs(parent, exist_ok=True)

    emit({"type": "enroll_status", "status": "starting", "prompt": PROMPT})
    frames = []
    started = time.time()
    try:
        with MicStream(
            sample_rate=getattr(config, "SAMPLE_RATE", 16000),
            channels=getattr(config, "CHANNELS", 1),
            frame_ms=getattr(config, "FRAME_MS", 40),
            device=args.device,
        ) as mic:
            emit({"type": "enroll_status", "status": "recording"})
            for pcm in mic.frames():
                frames.append(pcm)
                passed = time.time() - started
                level = int(np.abs(np.frombuffer(pcm, dtype=np.int16)).mean())
                emit({
                    "type": "enroll_level",
                    "elapsed": round(passed, 2),
                    "total": args.seconds,
                    "level": min(level / 4000, 1),
                })
                if passed >= args.seconds:
                    break
    except Exception as exc:
        emit({"type": "enroll_status", "status": "error", "message": str(exc)})
        sys.exit(1)

    audio = b"".join(frames)
    sr = getattr(config, "SAMPLE_RATE", 16000)
    ch = getattr(config, "CHANNELS", 1)
    with wave.open(args.out, "wb") as f:
        f.setnchannels(ch)
        f.setsampwidth(2)
        f.setframerate(sr)
        f.writeframes(audio)

    samples = np.frombuffer(audio, dtype=np.int16)
    peak = int(np.abs(samples).max()) if len(samples) else 0
    mean = int(np.abs(samples).mean()) if len(samples) else 0
    warn = None
    if mean < 150:
        warn = "音量偏低，建议靠近麦克风重录"
    elif peak > 32000:
        warn = "出现削波，建议离远一点重录"

    # 快速校验能否提出有效嵌入
    try:
        from speaker_me import enroll_from_wav
        _, _, n = enroll_from_wav(args.out)
        emit({
            "type": "enroll_status",
            "status": "completed",
            "path": args.out,
            "seconds": round(len(samples) / sr, 1),
            "peak": peak,
            "mean": mean,
            "enrollSegments": n,
            "warning": warn,
        })
    except Exception as exc:
        emit({
            "type": "enroll_status",
            "status": "error",
            "message": f"录音已保存但声纹注册失败：{exc}",
            "path": args.out,
        })
        sys.exit(2)


if __name__ == "__main__":
    main()
