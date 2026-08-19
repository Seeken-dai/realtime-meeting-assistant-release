"""Crash-tolerant WAV writers for one or more synchronized audio tracks.

The mixed track is the backwards-compatible playback file.  Auxiliary tracks
are best-effort: a failure opening or writing mic/system audio is reported for
that track while other writers continue.  Each writer owns its own lock and WAV
header, so closing one track cannot discard the others.
"""

from __future__ import annotations

import os
import threading
import wave
from pathlib import Path
from typing import Any, Dict


class _TrackWriter:
    def __init__(self, name: str, path: str, sample_rate: int, channels: int):
        self.name = str(name)
        self.path = str(path)
        self.sample_rate = max(1, int(sample_rate))
        self.channels = max(1, int(channels))
        self._file = None
        self._frames = 0
        self._lock = threading.Lock()
        self.error: str | None = None
        try:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            self._file = wave.open(self.path, "wb")
            self._file.setnchannels(self.channels)
            self._file.setsampwidth(2)
            self._file.setframerate(self.sample_rate)
        except Exception as exc:  # an auxiliary track must not stop recording
            self.error = str(exc) or exc.__class__.__name__
            self._file = None

    @property
    def opened(self) -> bool:
        return self._file is not None and self.error is None

    def write(self, pcm: bytes) -> bool:
        with self._lock:
            if self._file is None:
                return False
            try:
                self._file.writeframes(pcm)
                self._frames += len(pcm) // (2 * self.channels)
                return True
            except Exception as exc:
                self.error = str(exc) or exc.__class__.__name__
                try:
                    self._file.close()
                except Exception:
                    pass
                self._file = None
                return False

    def close(self) -> dict[str, Any]:
        with self._lock:
            if self._file is not None:
                try:
                    self._file.close()
                except Exception as exc:
                    self.error = self.error or str(exc) or exc.__class__.__name__
                self._file = None
        exists = os.path.isfile(self.path)
        size = 0
        if exists:
            try:
                size = os.path.getsize(self.path)
            except OSError:
                size = 0
        result: dict[str, Any] = {
            "name": self.name,
            "path": self.path,
            "ok": self.error is None and exists,
            "seconds": round(self._frames / self.sample_rate, 1),
            "bytes": size,
        }
        if self.error:
            result["error"] = self.error
        return result

    def status(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "ok": self.error is None and self._file is not None,
            **({"error": self.error} if self.error else {}),
        }


class AudioRecorder:
    """Write synchronized PCM frames to a mixed file and optional tracks."""

    def __init__(
        self,
        path: str,
        sample_rate: int,
        channels: int,
        track_paths: Dict[str, str] | None = None,
    ):
        paths: Dict[str, str] = {"mixed": str(path)}
        for name, track_path in (track_paths or {}).items():
            if track_path:
                paths[str(name)] = str(track_path)
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        self._writers = {
            name: _TrackWriter(name, track_path, self.sample_rate, self.channels)
            for name, track_path in paths.items()
        }
        self._closed = False

    @property
    def path(self) -> str:
        return self._writers["mixed"].path

    @property
    def failures(self) -> list[dict[str, str]]:
        return [
            {"name": writer.name, "path": writer.path, "error": writer.error}
            for writer in self._writers.values()
            if writer.error
        ]

    def status(self) -> dict[str, dict[str, Any]]:
        return {name: writer.status() for name, writer in self._writers.items()}

    def write(self, pcm: bytes) -> None:
        self.write_tracks(mixed=pcm)

    def write_tracks(self, **tracks: bytes | None) -> None:
        if self._closed:
            return
        for name, writer in self._writers.items():
            pcm = tracks.get(name)
            if pcm is None:
                # A missing auxiliary frame is not silently copied from mixed;
                # keeping the track aligned is more important than inventing data.
                continue
            writer.write(pcm)

    def close(self) -> dict[str, Any]:
        if self._closed:
            return {"path": self.path, "tracks": {}}
        self._closed = True
        results = {name: writer.close() for name, writer in self._writers.items()}
        mixed = results.get("mixed") or {}
        payload: dict[str, Any] = {
            "path": mixed.get("path", self.path),
            "seconds": mixed.get("seconds"),
            "bytes": mixed.get("bytes", 0),
            "tracks": results,
        }
        return payload
