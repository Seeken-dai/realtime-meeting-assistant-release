"""用一段已有录音回放整条【会中】链路，并打印可读的验收报告。

这是「不开会也能验证会中链路」的入口：它按真实节奏把录音喂给
`desktop_bridge.py --wav-in`，走的是和真实会议**完全相同**的代码路径
（阿里实时转写 → 本地声纹 → 按说话人切分 → 建议闸门 → LLM），
然后把 JSON Lines 事件流整理成人能读的报告。

用法：
    python replay_meeting.py --wav <会议wav>
    python replay_meeting.py --wav <会议wav> --provider gemini
    python replay_meeting.py --wav <会议wav> --speed 4 --no-suggest   # 只看转写
    python replay_meeting.py --wav <会议wav> --out events.json        # 存事件流备查

⚠️ 默认按真实时长回放：5 分钟录音就跑 5 分钟。建议触发依赖墙上时间
   （debounce / 20s 冷却），加速会让这些闸门失真，`--speed` 只适合看转写。

密钥与桌面端同源：先读 `%APPDATA%/meeting-copilot-desktop/meeting-copilot-secrets.json`
注入环境变量（与 main.cjs 的 pythonProcessEnv 同口径），再由 providers 回退到 config.py。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def user_data_dir() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "meeting-copilot-desktop")


def load_secrets_env() -> dict:
    """与 main.cjs pythonProcessEnv 同口径：secrets.json 逐项注入 env。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    path = os.path.join(user_data_dir(), "meeting-copilot-secrets.json")
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                for key, value in json.load(f).items():
                    if value is not None and str(value).strip():
                        env[key] = str(value)
        except Exception as exc:
            print(f"⚠️ 读取 secrets.json 失败（将只用 config.py）：{exc}")
    return env


def enroll_samples() -> list:
    """扫目录取全部声纹样本 —— 不做递增探测，理由见 HANDOFF §2。"""
    folder = os.path.join(user_data_dir(), "voiceprint")
    if not os.path.isdir(folder):
        return []
    return sorted(
        os.path.join(folder, n)
        for n in os.listdir(folder)
        if n.lower().endswith(".wav")
    )


def fmt(seconds: float) -> str:
    return f"{int(seconds // 60):02d}:{seconds % 60:04.1f}"


def main():
    ap = argparse.ArgumentParser(description="回放录音跑通会中链路")
    ap.add_argument("--wav", required=True, help="要回放的会议录音（16k 单声道）")
    ap.add_argument("--speed", type=float, default=1.0,
                    help="回放倍速，默认 1.0（>1 会让建议闸门失真）")
    ap.add_argument("--provider", help="LLM 供应商（默认按 config/设置）")
    ap.add_argument("--model", help="LLM 模型名")
    ap.add_argument("--asr-provider", dest="asr_provider", help="ASR 供应商")
    ap.add_argument("--silence-seconds", dest="silence_seconds", type=float,
                    help="对方停顿多久后给建议")
    ap.add_argument("--suggestion-count", dest="suggestion_count", type=int)
    ap.add_argument("--no-voiceprint", action="store_true",
                    help="不加载声纹（对照用：看不认「我」时是什么样）")
    ap.add_argument("--no-suggest", action="store_true",
                    help="不生成建议，只看转写与说话人（省 LLM 调用）")
    ap.add_argument("--docs", action="append", default=None,
                    help="本场文档路径，可重复；不传 = 零文档（无项目会议的默认）")
    ap.add_argument("--out", help="把原始事件流写到这个 JSON 文件")
    args = ap.parse_args()

    if not os.path.isfile(args.wav):
        sys.exit(f"找不到录音：{args.wav}")

    cmd = [sys.executable, os.path.join(HERE, "desktop_bridge.py"),
           "--wav-in", args.wav, "--wav-speed", str(args.speed)]
    # ⚠️ documents 必须显式传（哪怕空数组），否则 Python 侧回退到全局 docs/，
    #    等于跨项目串用资料（HANDOFF §7）。
    cmd += ["--docs", json.dumps(args.docs or [])]
    if args.provider:
        cmd += ["--provider", args.provider]
    if args.model:
        cmd += ["--llm-model", args.model]
    if args.asr_provider:
        cmd += ["--asr-provider", args.asr_provider]
    if args.silence_seconds:
        cmd += ["--silence-seconds", str(args.silence_seconds)]
    if args.suggestion_count:
        cmd += ["--suggestion-count", str(args.suggestion_count)]
    if not args.no_voiceprint:
        for path in enroll_samples():
            cmd += ["--enroll-wav", path]

    print(f"回放：{os.path.basename(args.wav)}  速度 {args.speed}x")
    print(f"声纹样本：{0 if args.no_voiceprint else len(enroll_samples())} 段"
          f"　|　文档：{len(args.docs or [])} 份"
          f"　|　建议：{'关' if args.no_suggest else '开'}")
    print("-" * 70)

    started = time.time()
    events = []
    transcript = []
    batches = []
    proc = subprocess.Popen(
        cmd, cwd=HERE, env=load_secrets_env(),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )
    if args.no_suggest:
        # 复用会中的「暂停建议」命令，省掉 LLM 调用。
        # ⚠️ 字段名是 command / suggestionsPaused（见 desktop_bridge.command_loop），
        #    写错不会报错，只会静默地什么都不做。
        proc.stdin.write(json.dumps({"command": "set_controls",
                                     "suggestionsPaused": True}) + "\n")
        proc.stdin.flush()

    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except Exception:
                continue
            event["_t"] = round(time.time() - started, 2)
            events.append(event)
            kind = event.get("type")

            if kind == "status":
                print(f"[{fmt(event['_t'])}] · {event.get('message', '')}")
            elif kind == "replay_progress":
                print(f"[{fmt(event['_t'])}] · 已放 {event.get('audioSec')}s / "
                      f"{event.get('totalSec')}s")
            elif kind == "voiceprint_ready":
                print(f"[{fmt(event['_t'])}] · 声纹就绪："
                      f"{event.get('enrollSamples')} 段样本 / "
                      f"{event.get('enrollSegments')} 个 embedding")
            elif kind == "transcript" and event.get("isFinal"):
                transcript.append(event)
                print(f"[{fmt(event['_t'])}] 【{event.get('speaker')}】"
                      f"{event.get('text')}")
            elif kind == "suggestions":
                batches.append(event)
                print(f"[{fmt(event['_t'])}] ★ 建议 ×"
                      f"{len(event.get('suggestions') or [])}"
                      f"（{event.get('trigger')}，耗时 {event.get('elapsed')}s）")
            elif kind == "error":
                print(f"[{fmt(event['_t'])}] ✗ {event.get('stage')}: "
                      f"{event.get('message')}")
            elif kind == "ended":
                print(f"[{fmt(event['_t'])}] · 结束")
    except KeyboardInterrupt:
        proc.terminate()
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        proc.wait(timeout=30)

    stderr = proc.stderr.read() or ""
    ended = next((e for e in reversed(events) if e.get("type") == "ended"), None)

    print("\n" + "=" * 70)
    print("回放报告")
    print("=" * 70)
    wall = time.time() - started
    print(f"墙上耗时 {wall:.0f}s")
    print(f"转写 final：{len(transcript)} 条"
          f"　|　总字数 {sum(len(t.get('text') or '') for t in transcript)}")
    if transcript:
        lengths = sorted(len(t.get("text") or "") for t in transcript)
        print(f"  单条字数：中位 {lengths[len(lengths) // 2]}　最长 {lengths[-1]}")
        by_speaker = {}
        for t in transcript:
            by_speaker[t.get("speaker")] = by_speaker.get(t.get("speaker"), 0) + 1
        print("  说话人分布：" + "　".join(f"{k} {v} 条" for k, v in by_speaker.items()))
    print(f"建议批次：{len(batches)}")
    for b in batches:
        print(f"  · {fmt(b['_t'])} {b.get('trigger')}　"
              f"{len(b.get('suggestions') or [])} 条　耗时 {b.get('elapsed')}s"
              + ("　⚠️ 解析失败" if b.get("parseError") else ""))
    if ended and ended.get("voiceprint"):
        vp = ended["voiceprint"]
        print(f"声纹：{vp.get('segments')} 段，其中判为「我」{vp.get('meSegments')} 段"
              f"　|　自适应切点 {vp.get('adaptiveCut')}")
    errs = [e for e in events if e.get("type") == "error"]
    if errs:
        print(f"错误 {len(errs)} 条：")
        for e in errs[:5]:
            print(f"  · {e.get('stage')}: {e.get('message')}")
    if stderr.strip():
        tail = [l for l in stderr.strip().split("\n") if l.strip()][-5:]
        print("stderr 末尾：")
        for line in tail:
            print(f"  {line}")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(events, f, ensure_ascii=False, indent=1)
        print(f"\n事件流已写入 {args.out}（{len(events)} 条）")


if __name__ == "__main__":
    main()
