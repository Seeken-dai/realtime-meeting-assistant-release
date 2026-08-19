import io
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

from desktop_bridge import command_loop, wait_for_recording_start


begin_event = threading.Event()
stop_event = threading.Event()
command_loop(
    {},
    stop_event,
    threading.Event(),
    begin_event,
    input_stream=io.StringIO('{"command":"begin_recording"}\n'),
)
assert begin_event.is_set()
assert not stop_event.is_set()

begin_event = threading.Event()
stop_event = threading.Event()
command_loop(
    {},
    stop_event,
    threading.Event(),
    begin_event,
    input_stream=io.StringIO('{"command":"stop"}\n'),
)
assert begin_event.is_set(), "stop must release the preparation wait"
assert stop_event.is_set()


class FakeBeginEvent:
    def __init__(self):
        self.waited = None
        self.was_set = False

    def set(self):
        self.was_set = True

    def wait(self, timeout=None):
        self.waited = timeout
        return False


fake = FakeBeginEvent()
assert wait_for_recording_start(fake, threading.Event(), timeout_seconds=120)
assert fake.waited == 120, "fallback must wait for the documented 120 seconds"

fake = FakeBeginEvent()
assert wait_for_recording_start(fake, threading.Event(), wav_in=True)
assert fake.was_set, "WAV replay should start without waiting for renderer input"

stopped = threading.Event()
stopped.set()
assert not wait_for_recording_start(FakeBeginEvent(), stopped, timeout_seconds=0)

script = Path(__file__).resolve().parents[1] / "warmup_meeting.py"
run = subprocess.run(
    [sys.executable, str(script), "--asr-provider", "xfyun"],
    cwd=script.parent,
    capture_output=True,
    text=True,
    encoding="utf-8",
    errors="replace",
    env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
    timeout=30,
    check=False,
)
assert run.returncode == 0, run.stderr
payload = json.loads(run.stdout.strip().splitlines()[-1])
assert payload["ok"] is True
assert any(step["name"] == "imports" and step["ok"] for step in payload["steps"])

print("ok: warmup script + begin/stop/fallback recording gate")
