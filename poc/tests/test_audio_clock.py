from audio_clock import RecordingSampleClock


def pcm(seconds: float, sample_rate: int = 1000) -> bytes:
    return bytes(int(seconds * sample_rate) * 2)


clock = RecordingSampleClock(sample_rate=1000)

# 正常 1 秒：ASR 与 WAV 同步。
clock.advance(pcm(1), recorded=True)
assert clock.map_ms(500) == 500
assert clock.map_ms(1000) == 1000

# 暂停 1 秒：ASR 收到静音维持连接，WAV 不增长。
clock.advance(pcm(1), recorded=False)
assert clock.map_ms(1500) == 1000
assert clock.map_ms(2000) == 1000

# 恢复后继续线性映射。
clock.advance(pcm(1), recorded=True)
assert clock.map_ms(2500) == 1500
assert clock.map_ms(3000) == 2000

# 断线期间 WAV 继续录，但 ASR 不接收；重连后的会话时间从 0 开始，
# 映射应接到 WAV 的 3 秒位置。
clock.set_accepting_audio(False)
clock.advance(pcm(1), recorded=True)
clock.reset_asr_session()
clock.advance(pcm(1), recorded=True)
assert clock.map_ms(0) == 3000
assert clock.map_ms(500) == 3500
assert clock.map_ms(1000) == 4000
assert clock.recorded_ms == 4000

# 明显超出已发送音频的供应商时间不能伪装成精确值。
assert clock.map_ms(5000) is None

print("ok: recording sample clock")
