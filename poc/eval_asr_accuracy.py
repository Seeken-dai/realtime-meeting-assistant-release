"""
方案 B 关键验证 #1：同一段真实会议 wav 上，比各家实时 ASR 与对照转写的字错率。

对照文本 = 转写 md（本身可能已有错，结论是【相对一致性】不是绝对真理 CER）。
音频从本地 wav 按帧推流，模拟会中实时。

用法：
  python eval_asr_accuracy.py --wav eval/_work_16k.wav \\
      --md "eval/07-21 工作交接与职责梳理.md" \\
      --vendors aliyun,xfyun --minutes 8
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import wave
from datetime import datetime

import numpy as np

import providers

_HERE = os.path.dirname(os.path.abspath(__file__))
HEADER = re.compile(r"^(.+?)\s+(\d{2}:\d{2}:\d{2})\s*$")
# 评测时去掉的标点/空白（中英常见）
_PUNCT = re.compile(
    r"[\s\u3000，。！？、；：,.!?;:\"'“”‘’（）()\[\]【】《》<>…—\-·`~@#\$%\^&\*\+=\|\\/]+"
)


def parse_ts(ts: str) -> float:
    h, m, s = ts.split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)


def load_gold(md_path: str, t0: float, t1: float) -> str:
    """取 [t0, t1) 时间窗内的对照正文（按说话人块拼接）。"""
    lines = open(md_path, encoding="utf-8").read().splitlines()
    blocks = []  # (start, text)
    i = 0
    while i < len(lines):
        m = HEADER.match(lines[i].strip())
        if not m:
            i += 1
            continue
        start = parse_ts(m.group(2))
        i += 1
        body = []
        while i < len(lines) and not HEADER.match(lines[i].strip()):
            body.append(lines[i])
            i += 1
        text = "".join(body).strip()
        if text:
            blocks.append((start, text))
    parts = [t for s, t in blocks if t0 <= s < t1]
    return "".join(parts)


def normalize(text: str) -> str:
    text = text.lower()
    text = _PUNCT.sub("", text)
    # 全角数字/字母 → 半角
    out = []
    for ch in text:
        code = ord(ch)
        if 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    return "".join(out)


def edit_distance(ref: str, hyp: str) -> int:
    """标准 Levenshtein，字符级。"""
    if ref == hyp:
        return 0
    n, m = len(ref), len(hyp)
    if n == 0:
        return m
    if m == 0:
        return n
    # 两行滚动，省内存
    prev = list(range(m + 1))
    cur = [0] * (m + 1)
    for i in range(1, n + 1):
        cur[0] = i
        rc = ref[i - 1]
        for j in range(1, m + 1):
            cost = 0 if rc == hyp[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
        prev, cur = cur, prev
    return prev[m]


def cer(ref: str, hyp: str) -> dict:
    r, h = normalize(ref), normalize(hyp)
    dist = edit_distance(r, h)
    return {
        "cer": dist / max(len(r), 1),
        "edits": dist,
        "ref_chars": len(r),
        "hyp_chars": len(h),
        "ref_raw_chars": len(ref),
        "hyp_raw_chars": len(hyp),
    }


def read_wav_slice(path: str, start_sec: float, minutes: float):
    with wave.open(path, "rb") as f:
        assert f.getnchannels() == 1 and f.getsampwidth() == 2
        rate = f.getframerate()
        assert rate == 16000
        start = int(start_sec * rate)
        n = int(minutes * 60 * rate)
        f.setpos(start)
        raw = f.readframes(n)
    samples = np.frombuffer(raw, dtype=np.int16)
    return samples, rate


def run_vendor(vendor: str, pcm: bytes, frame_bytes: int, speed: float,
               model: str | None = None, realtime_pace: bool = True):
    """把 PCM 按帧推给 ASR，收集 is_final 句子。"""
    asr = providers.build_asr(provider=vendor, debug=False, model=model)
    finals: list[str] = []
    partials = 0
    errors: list[str] = []

    def on_result(text, speaker, is_final, end_ms=0, **_extra):
        nonlocal partials
        if not text:
            return
        if is_final:
            finals.append(text.strip())
        else:
            partials += 1

    print(f"\n=== {vendor} ({asr.name}) 开始推流 "
          f"{len(pcm)/32000:.1f}s 音频，speed={speed}x ===")
    t0 = time.time()
    asr.start(on_result)
    try:
        # 等连接稳一下
        time.sleep(0.4)
        frame_sec = frame_bytes / 32000.0  # 16k * 2 bytes
        sleep_per = frame_sec / max(speed, 0.1) if realtime_pace else 0
        sent = 0
        n = len(pcm)
        while sent < n:
            chunk = pcm[sent: sent + frame_bytes]
            if len(chunk) < frame_bytes:
                chunk = chunk + b"\x00" * (frame_bytes - len(chunk))
            try:
                asr.send(chunk)
            except Exception as exc:  # noqa: BLE001 — 厂商 SDK 异常种类不一
                errors.append(str(exc))
                print(f"  [send error] {exc}")
                break
            sent += frame_bytes
            if sleep_per > 0:
                # 粗略 pacing：按块 sleep，略扣处理开销
                time.sleep(sleep_per)
            if sent % (frame_bytes * 250) == 0:  # ~10s 音频
                print(f"  …已送 {sent/32000:.0f}s  收到 final {len(finals)} 句",
                      flush=True)
        # 尾部静音，促发最后一句
        silence = b"\x00" * frame_bytes
        for _ in range(25):
            asr.send(silence)
            if sleep_per:
                time.sleep(sleep_per)
        time.sleep(1.5)
    finally:
        try:
            asr.stop()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"stop: {exc}")
        # 收尾缓冲
        time.sleep(1.0)

    wall = time.time() - t0
    hyp = "".join(finals)
    print(f"=== {vendor} 结束：final {len(finals)} 句，"
          f"partial 事件 {partials}，墙钟 {wall:.1f}s ===")
    return {
        "vendor": vendor,
        "model": model,
        "finals": finals,
        "hyp": hyp,
        "wall_sec": wall,
        "errors": errors,
        "partial_events": partials,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wav", required=True)
    ap.add_argument("--md", required=True)
    ap.add_argument("--vendors", default="aliyun,xfyun",
                    help="逗号分隔：aliyun,xfyun,...")
    ap.add_argument("--minutes", type=float, default=8.0,
                    help="只评测前 N 分钟（控制耗时与费用）")
    ap.add_argument("--start", type=float, default=0.0,
                    help="起始秒（可跳过开场）")
    ap.add_argument("--speed", type=float, default=1.2,
                    help="推流倍速（过大可能被服务端限流/丢识别）")
    ap.add_argument("--aliyun-model", default="paraformer-realtime-v2")
    ap.add_argument("--out-dir", default=os.path.join(_HERE, "eval"))
    args = ap.parse_args()

    samples, rate = read_wav_slice(args.wav, args.start, args.minutes)
    pcm = samples.tobytes()
    t0, t1 = args.start, args.start + args.minutes * 60
    gold = load_gold(args.md, t0, t1)
    if not gold.strip():
        sys.exit(f"时间窗 [{t0},{t1}) 内对照文本为空，检查 md 时间戳")

    print(f"音频切片：{args.start:.0f}s + {args.minutes:.1f} min，"
          f"{len(samples)/rate:.1f}s，{len(pcm)} bytes")
    print(f"对照原文（规范化前）{len(gold)} 字；"
          f"规范化后 {len(normalize(gold))} 字")
    print(f"对照预览：{normalize(gold)[:80]}…")

    frame_bytes = int(rate * 0.04) * 2  # 40ms
    vendors = [v.strip() for v in args.vendors.split(",") if v.strip()]
    results = []
    for v in vendors:
        model = args.aliyun_model if v == "aliyun" else None
        try:
            r = run_vendor(v, pcm, frame_bytes, speed=args.speed, model=model)
        except SystemExit as e:
            print(f"跳过 {v}: {e}")
            continue
        except Exception as exc:  # noqa: BLE001
            print(f"失败 {v}: {exc}")
            results.append({"vendor": v, "error": str(exc)})
            continue
        metrics = cer(gold, r["hyp"])
        r["metrics"] = metrics
        results.append(r)
        print(f"\n[{v}] CER={metrics['cer']:.1%}  "
              f"edits={metrics['edits']}  "
              f"ref={metrics['ref_chars']} hyp={metrics['hyp_chars']}")
        print(f"[{v}] hyp 预览：{normalize(r['hyp'])[:100]}…")

    # 汇总
    print("\n======== 汇总 ========")
    print(f"窗口：{t0:.0f}–{t1:.0f}s（{args.minutes} min）  speed={args.speed}x")
    print(f"对照规范化字数：{len(normalize(gold))}")
    rows = []
    for r in results:
        if "metrics" not in r:
            print(f"  {r.get('vendor')}: ERROR {r.get('error')}")
            continue
        m = r["metrics"]
        print(f"  {r['vendor']:10s}  CER {m['cer']:6.1%}  "
              f"edits {m['edits']:4d}  hyp_chars {m['hyp_chars']:4d}  "
              f"wall {r['wall_sec']:.0f}s")
        rows.append({
            "vendor": r["vendor"],
            "model": r.get("model"),
            "cer": m["cer"],
            "edits": m["edits"],
            "ref_chars": m["ref_chars"],
            "hyp_chars": m["hyp_chars"],
            "wall_sec": r["wall_sec"],
            "n_finals": len(r.get("finals") or []),
            "errors": r.get("errors") or [],
        })

    os.makedirs(args.out_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = {
        "wav": os.path.basename(args.wav),
        "md": os.path.basename(args.md),
        "window_sec": [t0, t1],
        "minutes": args.minutes,
        "speed": args.speed,
        "gold_preview": normalize(gold)[:200],
        "gold_norm_chars": len(normalize(gold)),
        "results": rows,
        "hyps": {
            r["vendor"]: {
                "text": r.get("hyp", ""),
                "finals": r.get("finals", []),
            }
            for r in results if "hyp" in r
        },
        "note": "对照 md 本身可能含识别错误；CER 为相对一致性，非绝对真值。",
    }
    out_path = os.path.join(args.out_dir, f"asr_cer_{stamp}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n结果已写：{out_path}")


if __name__ == "__main__":
    main()
