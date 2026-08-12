"""A small local interface for helmcode-whisper.

Deliberately outside the tool's repo: helmcode-whisper is a CLI, and the point
of a CLI is that everyone can put whatever front end they like on top of it.
This is mine. It is one file, uses nothing but the Python standard library and
the helmcode-whisper package itself, and binds to 127.0.0.1 only — a meeting
archive should not become a service on your LAN because a UI was convenient.

    python app.py            # then open http://127.0.0.1:7864

Recording runs in this process, through the same capture classes the CLI uses.
Processing shells out to `hcw process`, so a long transcription cannot take the
UI down with it.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from helmcode_whisper.capture import (
    find_mic_device,
    find_system_device,
    make_mic_recorder,
    make_system_recorder,
)
from helmcode_whisper.config import load_config
from helmcode_whisper.pipeline.search import search_hits
from helmcode_whisper.store import Meeting

HOST = "127.0.0.1"
PORT = 7864
HERE = Path(__file__).parent

CONFIG = load_config()
_lock = threading.Lock()


class Recording:
    """The take in progress. At most one at a time, on purpose."""

    def __init__(self, title: str) -> None:
        mic_device = find_mic_device()
        if mic_device is None:
            raise RuntimeError("No microphone found.")
        system_device = find_system_device()

        CONFIG.home.mkdir(parents=True, exist_ok=True)
        self.meeting = Meeting.create(CONFIG.home, title, datetime.now())
        self.tracks = [("me", make_mic_recorder(mic_device, self.meeting.mic_wav))]
        if system_device:
            self.tracks.append(
                ("others", make_system_recorder(system_device, self.meeting.system_wav))
            )
        self.has_system = system_device is not None
        self.started_at = time.monotonic()
        for _, recorder in self.tracks:
            recorder.start()

    def status(self) -> dict:
        return {
            "meeting": self.meeting.path.name,
            "title": self.meeting.title,
            "elapsed": time.monotonic() - self.started_at,
            "has_system": self.has_system,
            "tracks": [
                {"label": label, "level": recorder.level, "seconds": recorder.duration}
                for label, recorder in self.tracks
            ],
        }

    def stop(self) -> dict:
        for _, recorder in self.tracks:
            recorder.stop()
        duration = max((recorder.duration for _, recorder in self.tracks), default=0.0)
        self.meeting.save_meta(
            {
                "duration_seconds": round(duration, 2),
                "ended_at": datetime.now().isoformat(timespec="seconds"),
                "tracks": {
                    label: {
                        "file": recorder.path.name,
                        "device": recorder.device.name,
                        "backend": recorder.device.backend,
                        "samplerate": recorder.device.samplerate,
                        "seconds": round(recorder.duration, 2),
                        "silence_padded_seconds": round(recorder.padded_seconds, 2),
                    }
                    for label, recorder in self.tracks
                },
                "dropped_blocks": sum(recorder.dropped_blocks for _, recorder in self.tracks),
                "recorded_by": "local-ui",
            }
        )
        # A silent take is worth saying out loud here too — the CLI learned that
        # lesson from a microphone that was muted for a whole test session.
        silent = [label for label, recorder in self.tracks if recorder.max_peak < 0.001]
        return {"meeting": self.meeting.path.name, "seconds": duration, "silent_tracks": silent}


class Processing:
    """One `hcw process` run, with its output kept for the UI to show."""

    def __init__(self, meeting_name: str) -> None:
        self.meeting = meeting_name
        self.lines: list[str] = []
        self.done = False
        self.failed = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        process = subprocess.Popen(
            [sys.executable, "-m", "helmcode_whisper.cli", "process", self.meeting],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            # No cwd override: the child inherits this process's environment,
            # including whatever the .env put there, so it resolves the API key
            # exactly the way the server did.
        )
        assert process.stdout is not None
        for line in process.stdout:
            line = line.rstrip()
            if line:
                self.lines.append(line)
        self.failed = process.wait() != 0
        self.done = True

    def status(self) -> dict:
        return {
            "meeting": self.meeting,
            "lines": self.lines[-40:],
            "done": self.done,
            "failed": self.failed,
        }


recording: Recording | None = None
processing: Processing | None = None


# ── devices ──────────────────────────────────────────────────────

_devices_cache: tuple[float, dict] = (0.0, {})


def devices_snapshot() -> dict:
    """Which inputs are available, cached briefly.

    The UI needs this *before* anyone presses record: a missing loopback means
    the meeting is captured with the microphone only, and finding that out
    afterwards costs the recording. Enumerating PortAudio is not free and the
    UI polls, hence the short cache — short enough that plugging in a headset
    shows up within a few seconds.
    """
    global _devices_cache
    cached_at, cached = _devices_cache
    now = time.monotonic()
    if cached and now - cached_at < 5.0:
        return cached

    try:
        mic = find_mic_device()
        system = find_system_device()
        snapshot = {
            "mic": mic.name if mic else None,
            "system": system.name if system else None,
        }
    except Exception as exc:  # never take the UI down over device enumeration
        snapshot = {"mic": None, "system": None, "error": f"{type(exc).__name__}: {exc}"}

    _devices_cache = (now, snapshot)
    return snapshot


# ── data ─────────────────────────────────────────────────────────


def list_meetings() -> list[dict]:
    meetings = []
    for meeting in reversed(Meeting.all(CONFIG.home)):
        meta = meeting.load_meta()
        meetings.append(
            {
                "name": meeting.path.name,
                "title": meta.get("title") or meeting.path.name,
                "date": str(meta.get("started_at", ""))[:10],
                "seconds": meta.get("duration_seconds") or 0,
                "processed": meeting.notes_md.is_file(),
                "speakers": meta.get("speakers") or [],
            }
        )
    return meetings


def meeting_detail(name: str) -> dict:
    meeting = Meeting(CONFIG.home / name)
    if not meeting.meta_json.is_file():
        raise FileNotFoundError(name)

    notes = None
    cached = meeting.read_cached_json("notes.json")
    if cached:
        notes = cached.get("notes")

    transcript = None
    if meeting.transcript_json.is_file():
        data = json.loads(meeting.transcript_json.read_text(encoding="utf-8"))
        transcript = [
            {
                "start": segment["start"],
                "speaker": segment.get("speaker", ""),
                "text": segment["text"],
            }
            for segment in data.get("segments", [])
            if not segment.get("dropped")
        ]

    return {
        "meta": meeting.load_meta(),
        "notes": notes,
        "transcript": transcript,
        "notes_markdown": meeting.notes_md.read_text(encoding="utf-8")
        if meeting.notes_md.is_file()
        else None,
    }


# ── http ─────────────────────────────────────────────────────────


class Handler(BaseHTTPRequestHandler):
    server_version = "helmcode-whisper-ui"

    def log_message(self, *args) -> None:  # noqa: ANN002
        pass  # the terminal is for the app's own output, not an access log

    def _send(self, payload: dict | list, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, content_type: str) -> None:
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        route = urlparse(self.path)
        query = parse_qs(route.query)

        try:
            if route.path == "/":
                self._send_file(HERE / "index.html", "text/html; charset=utf-8")
            elif route.path == "/api/state":
                with _lock:
                    self._send(
                        {
                            "recording": recording.status() if recording else None,
                            "processing": processing.status() if processing else None,
                            "meetings": list_meetings(),
                            "devices": devices_snapshot(),
                            "home": str(CONFIG.home),
                        }
                    )
            elif route.path == "/api/meeting":
                self._send(meeting_detail(query["name"][0]))
            elif route.path == "/api/search":
                term = (query.get("q") or [""])[0].strip()
                if not term:
                    self._send({"hits": [], "semantic": True})
                    return
                hits, semantic = search_hits(CONFIG, term, limit=8)
                self._send(
                    {
                        "semantic": semantic,
                        "hits": [
                            {
                                "meeting_id": hit.meeting_id,
                                "meeting": hit.meeting_title,
                                "date": hit.meeting_date,
                                "start": hit.start,
                                "speaker": hit.speaker,
                                "text": hit.text,
                            }
                            for hit in hits
                        ],
                    }
                )
            else:
                self._send({"error": "not found"}, 404)
        except FileNotFoundError:
            self._send({"error": "not found"}, 404)
        except Exception as exc:
            self._send({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def do_POST(self) -> None:  # noqa: N802
        global recording, processing
        route = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or "{}") if length else {}

        try:
            if route.path == "/api/record/start":
                with _lock:
                    if recording is not None:
                        self._send({"error": "already recording"}, 409)
                        return
                    recording = Recording(payload.get("title") or "meeting")
                    self._send(recording.status())
            elif route.path == "/api/record/stop":
                with _lock:
                    if recording is None:
                        self._send({"error": "not recording"}, 409)
                        return
                    result = recording.stop()
                    recording = None
                self._send(result)
            elif route.path == "/api/process":
                with _lock:
                    if processing is not None and not processing.done:
                        self._send({"error": "already processing"}, 409)
                        return
                    processing = Processing(payload["meeting"])
                self._send(processing.status())
            else:
                self._send({"error": "not found"}, 404)
        except Exception as exc:
            self._send({"error": f"{type(exc).__name__}: {exc}"}, 500)


def main() -> None:
    if not CONFIG.api_key:
        # The package finds `.env` by walking up from the working directory, so
        # running this from anywhere else silently loses the key and degrades
        # search to keyword matching. Say so before that confuses anyone.
        print("warning: no HELMCODE_API_KEY found.")
        print("         run this from the helmcode-whisper checkout, e.g.")
        print("         cd C:\\dev\\Helmcode-whisper && "
              ".venv\\Scripts\\python.exe ..\\helmcode-whisper-ui\\app.py")
        print("         recording works; processing and semantic search do not.\n")

    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"helmcode-whisper ui  ->  http://{HOST}:{PORT}")
    print(f"meetings in {CONFIG.home}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        with _lock:
            if recording is not None:
                recording.stop()
        server.server_close()


if __name__ == "__main__":
    main()
