import desktop_bridge
from audio_clock import RecordingSampleClock


events = []
desktop_bridge.emit = lambda event_type, **payload: events.append(
    {"type": event_type, **payload}
)

clock = RecordingSampleClock(sample_rate=1000)
clock.advance(bytes(2000 * 2), recorded=True)
clock.advance(bytes(1000 * 2), recorded=False)  # 暂停：ASR 走、WAV 不走
clock.advance(bytes(1000 * 2), recorded=True)

session = desktop_bridge.BridgeSession(
    engine=None,
    me_label=None,
    audio_clock=clock,
)
session._suggestions_paused = True

session.on_transcript(
    "第一段",
    speaker=None,
    is_final=True,
    begin_ms=3000,
    end_ms=3500,
    words=[{"text": "第一段", "begin_time": 3000, "end_time": 3500}],
)
first = next(event for event in events if event["type"] == "transcript")
assert first["audioStartMs"] == 2000
assert first["audioEndMs"] == 2500

# 只有 end 的供应商以前一条 final 的结束点作为下一段开头。
events.clear()
session.on_transcript(
    "第二段",
    speaker=None,
    is_final=True,
    end_ms=3900,
)
second = next(event for event in events if event["type"] == "transcript")
assert second["audioStartMs"] == 2500
assert second["audioEndMs"] == 2900

context = session._context_range(session.transcript)
assert context["wallStartAt"] <= context["wallEndAt"]
assert context["audioStartMs"] == 2000
assert context["audioEndMs"] == 2900
assert context["approximate"] is False

assert (
    desktop_bridge.classify_startup_error(
        "asr", SystemExit("请填入 ALIYUN_ASR_KEY")
    )
    == "asr_credentials"
)
assert (
    desktop_bridge.classify_startup_error(
        "llm", RuntimeError("No module named local_model")
    )
    == "model_load"
)
assert (
    desktop_bridge.classify_startup_error(
        "asr", RuntimeError("connection timed out")
    )
    == "asr_service"
)

print("ok: bridge transcript timing + suggestion context + startup error classification")
