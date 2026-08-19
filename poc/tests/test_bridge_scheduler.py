import threading
import time

import desktop_bridge


events = []
desktop_bridge.emit = lambda event_type, **payload: events.append(
    {"type": event_type, **payload}
)


class FakeEngine:
    provider = "test"
    model = "fast-test"

    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()

    def suggest(self, _context, count=3):
        self.started.set()
        self.release.wait(2)
        return {
            "suggestions": [
                {
                    "intent": "确认范围",
                    "script": "我先确认范围和验收标准。",
                    "type": "advisory",
                    "references": [],
                    "evidence": [],
                }
            ][:count],
            "hits": [],
        }


engine = FakeEngine()
session = desktop_bridge.BridgeSession(
    engine,
    me_label="me",
    debounce_sec=0.01,
    suggestion_count=2,
    scene="requirements",
)
session.transcript.append(
    {
        "id": "seed",
        "speaker": "对方",
        "speakerId": "other",
        "text": "我们先对齐当前需求范围，下周由产品负责整理验收标准。",
        "at": time.time(),
    }
)

worker = threading.Thread(target=session.fire_suggestion, daemon=True)
worker.start()
assert engine.started.wait(1), "fake request did not start"
session.on_transcript(
    "补充一个待办：下周由产品整理验收标准。",
    "other",
    True,
)
engine.release.set()
worker.join(2)
assert not worker.is_alive(), "suggestion worker did not finish"

suggestion_event = next(event for event in events if event["type"] == "suggestions")
assert suggestion_event["runtime"]["mergeCount"] == 0
assert suggestion_event["context"]
assert suggestion_event["memoryCandidates"]
assert session._pending_merge_count == 1
assert session.pending, "latest context was not scheduled for one supplement"
with session._lock:
    if session._pending_timer is not None:
        session._pending_timer.cancel()
        session._pending_timer = None

mixed_candidates = desktop_bridge._local_memory_candidates([
    {
        "id": "mixed-1",
        "text": "We agreed to go with option B. Action item: Alice will submit the draft by Friday.",
    }
])
assert any(item["kind"] == "decision" for item in mixed_candidates)
assert any(item["kind"] == "action_item" for item in mixed_candidates)
assert any(item["dueAt"] == "by Friday" for item in mixed_candidates)

short_engine = FakeEngine()
short_session = desktop_bridge.BridgeSession(
    short_engine,
    me_label="me",
    debounce_sec=0.01,
    suggestion_count=1,
    scene="requirements",
)
event_count_before_short_signal = len(events)
short_session.on_transcript("Decision: go with option B.", "other", True)
assert short_engine.started.wait(1), "explicit short decision was blocked by char gate"
short_engine.release.set()
deadline = time.time() + 2
while time.time() < deadline and not any(
    item["type"] == "suggestions" for item in events[event_count_before_short_signal:]
):
    time.sleep(0.01)
short_memory_events = events[event_count_before_short_signal:]
assert any(
    item["type"] == "suggestions"
    and any(candidate["kind"] == "decision" for candidate in item["memoryCandidates"])
    for item in short_memory_events
)

print("ok: realtime scheduler coalesces latest context and keeps memory candidates")
