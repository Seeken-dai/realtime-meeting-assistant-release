"""
录一段自己的声音，用于声纹注册（方案 D1）。

录出来的 wav 直接喂给 verify_speaker.py --enroll。

⚠️ 录音条件必须和真实开会时一致：同一支麦克风、同样的摆放距离。
   声纹对信道敏感 —— 用耳机录、开会用桌面麦，注册就白做了。

用法：
    python record_enroll.py                    # 用默认麦克风录 20 秒
    python record_enroll.py --seconds 30 --device 1
    python record_enroll.py --list             # 先看有哪些麦克风
"""

import argparse
import sys
import time
import wave

import numpy as np
import sounddevice as sd

from mic_stream import MicStream

try:
    import config
except ImportError:
    sys.exit("未找到 config.py")


# 注册语料覆盖 21 个声母与四声，避免只有几个字导致向量片面。
# 全长约 30 秒 > 默认录制 20 秒，读不完会被截断 —— 这是故意的，
# 宁可截断也不要读完后剩时间在那沉默（沉默段不产生 embedding）。
PROMPT = """请用【平时开会的音量和语速】朗读下面这段话：

  今天这场评审，主要把需求范围和排期确认下来。
  审批流程还得再细化，特别是金额分档和多级审批的规则。
  涉及定制开发的部分，工作量评估要重新过一遍，
  不要直接参考上个项目的口径。
  另外，接口对接的时间点，也要跟对方团队再沟通一次。
  内部资源如果排不开，可以先做第一期，剩下的放到后面迭代。

读不完会自动截断，不影响。读完还有时间就随便再说几句，不要沉默。

── 或者不念稿 ──
直接聊一个真实议题（比如"明天这场会我准备问客户哪几个问题"）。
自然说话的语调比朗读更接近真实开会状态，注册效果通常更好；
稿子的价值只是"不用想说什么"。内容随意，别停顿就行。"""


def list_devices():
    host_apis = sd.query_hostapis()
    for index, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] <= 0:
            continue
        api = host_apis[dev["hostapi"]]["name"]
        if api != "MME" or dev["name"].startswith("Microsoft 声音映射器"):
            continue
        print(f"  [{index}] {dev['name']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="enroll_me.wav")
    ap.add_argument("--seconds", type=float, default=20)
    ap.add_argument("--device", type=int)
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    if args.list:
        print("可用麦克风：")
        list_devices()
        return

    print(PROMPT)
    print(f"\n将录制 {args.seconds:.0f} 秒 → {args.out}")
    input("准备好后按回车开始…")

    frames = []
    started = time.time()
    with MicStream(sample_rate=config.SAMPLE_RATE, channels=config.CHANNELS,
                   frame_ms=config.FRAME_MS, device=args.device) as mic:
        for pcm in mic.frames():
            frames.append(pcm)
            passed = time.time() - started
            level = int(np.abs(np.frombuffer(pcm, dtype=np.int16)).mean())
            bars = "█" * min(int(level / 200), 30)
            print(f"\r  {passed:5.1f}s / {args.seconds:.0f}s  {bars:<30}",
                  end="", flush=True)
            if passed >= args.seconds:
                break

    audio = b"".join(frames)
    with wave.open(args.out, "wb") as f:
        f.setnchannels(config.CHANNELS)
        f.setsampwidth(2)
        f.setframerate(config.SAMPLE_RATE)
        f.writeframes(audio)

    samples = np.frombuffer(audio, dtype=np.int16)
    peak = int(np.abs(samples).max())
    mean = int(np.abs(samples).mean())
    print(f"\n\n已保存 {args.out}（{len(samples)/config.SAMPLE_RATE:.1f}s）")
    print(f"  峰值 {peak}  平均 {mean}")
    if mean < 150:
        print("  ⚠️ 音量偏低，声纹质量会受影响 —— 建议靠近麦克风重录")
    elif peak > 32000:
        print("  ⚠️ 出现削波，可能失真 —— 建议离远一点重录")
    else:
        print("  ✓ 音量正常")
    print(f"\n下一步：\n  python verify_speaker.py --wav <会议录音.wav> --enroll {args.out}")


if __name__ == "__main__":
    main()
