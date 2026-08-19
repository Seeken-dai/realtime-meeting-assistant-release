"""
POC 运行器 —— M1 技术验证入口。

功能：
  - 采集麦克风 → 实时流式送 ASR → 打印【带说话人标签】的转写
  - 统计端到端延迟（首次收到该句结果 - 说话结束的近似时刻）
  - 支持切换厂商，用同一场会议横向对比

用法：
    python run_poc.py --vendor aliyun     # 阿里云
    python run_poc.py --vendor xfyun      # 讯飞
    python run_poc.py --list-devices      # 查看麦克风设备
    python run_poc.py --vendor xfyun --device 2   # 指定麦克风

POC 验收口径（对照 PRD 6.3）：
    找 3-4 人在会议室真实对话 ≥ 10 分钟，观察：
    ① 转写是否可用（专有名词错漏可接受）
    ② 同一个人的连续发言是否稳定归到同一说话人（不频繁跳号）
    ③ 端到端延迟是否 < 2s
"""

import argparse
import sys
import time

from mic_stream import MicStream, list_input_devices

try:
    import config
except ImportError:
    print("未找到 config.py，请先执行：cp config.example.py config.py 并填入密钥")
    sys.exit(1)


# ── 说话人标签着色（终端可读性）────────────────────────────
_COLORS = ["\033[96m", "\033[93m", "\033[92m", "\033[95m", "\033[91m"]
_RESET = "\033[0m"


class Printer:
    """把 ASR 回调打印成带说话人标签的对话流，并统计端到端延迟。

    延迟算法：以开始录音为 t0，某句音频在流中的结束时刻是 end_ms，
    则真实延迟 = (收到结果的墙钟时间 - t0) - end_ms。
    （不能用"距上次发送音频的时间"，音频是连续流，那个值恒为 0。）
    """

    def __init__(self):
        self._t_start = time.time()
        self._latencies = []

    def note_audio(self):
        pass  # 保留接口，延迟改由 end_ms 计算

    def on_result(self, text, speaker, is_final, end_ms=0, **_extra):
        spk = speaker if speaker is not None else "?"
        label = f"说话人{spk}"
        color = _COLORS[hash(str(spk)) % len(_COLORS)] if speaker is not None else ""
        if is_final:
            suffix = ""
            if end_ms:
                latency = (time.time() - self._t_start) - end_ms / 1000.0
                if latency > 0:
                    self._latencies.append(latency)
                    suffix = f"   \033[90m({latency:.1f}s)\033[0m"
            print(f"\r{color}[{label}]{_RESET} {text}{suffix}")
        else:
            # 中间结果灰色、可覆盖
            print(f"\r\033[90m[{label}] {text}\033[0m", end="", flush=True)

    def summary(self):
        if not self._latencies:
            return
        avg = sum(self._latencies) / len(self._latencies)
        mx = max(self._latencies)
        verdict = "✅ 达标 (<2s)" if avg < 2.0 else "⚠️ 超出 2s 目标"
        print(f"\n延迟统计：{len(self._latencies)} 句，"
              f"平均 {avg:.2f}s，最大 {mx:.2f}s  {verdict}")


import providers

VENDORS = ["aliyun", "xfyun", "volcano", "tencent", "mimo"]


def build_asr(vendor, debug=False, model=None):
    return providers.build_asr(provider=vendor, debug=debug, model=model)


def main():
    parser = argparse.ArgumentParser(description="实时会议话术助手 - ASR POC")
    parser.add_argument("--vendor", choices=VENDORS,
                        help="选择 ASR 厂商")
    parser.add_argument("--device", type=int, default=None, help="麦克风设备 index")
    parser.add_argument("--list-devices", action="store_true", help="列出麦克风设备")
    parser.add_argument("--debug", action="store_true",
                        help="打印 ASR 原始返回，用于排查说话人字段")
    parser.add_argument("--model", default=None,
                        help="覆盖模型名（阿里云可试 fun-asr-realtime）")
    args = parser.parse_args()

    if args.list_devices:
        list_input_devices()
        return
    if not args.vendor:
        parser.error("请用 --vendor 指定厂商，或用 --list-devices 查看设备")

    asr = build_asr(args.vendor, debug=args.debug, model=args.model)
    printer = Printer()

    print(f"\n=== POC 开始：{asr.name} ===")
    print("对着麦克风说话，实时转写将逐句显示。按 Ctrl+C 结束。\n")

    asr.start(printer.on_result)
    try:
        with MicStream(sample_rate=config.SAMPLE_RATE,
                       channels=config.CHANNELS,
                       frame_ms=config.FRAME_MS,
                       device=args.device) as mic:
            for pcm in mic.frames():
                printer.note_audio()
                asr.send(pcm)
    except KeyboardInterrupt:
        print("\n\n结束录音，正在关闭连接…")
    finally:
        asr.stop()
        printer.summary()
        print("=== POC 结束 ===")


if __name__ == "__main__":
    main()
