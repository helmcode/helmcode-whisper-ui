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

import argparse
import json
import os
import shutil
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
from helmcode_whisper.pipeline.index import connect as index_connect
from helmcode_whisper.pipeline.index import delete_meeting as index_delete
from helmcode_whisper.pipeline.search import search_hits
from helmcode_whisper.store import Meeting

HERE = Path(__file__).parent
CONFIG = load_config()
_lock = threading.Lock()


# ── recording ────────────────────────────────────────────────────


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
            "lines": self.lines[-60:],
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

    # Never re-enumerate while a take is in progress. Finding the loopback
    # constructs a PyAudio instance and terminates it, which initialises and
    # tears down PortAudio; doing that every few seconds underneath a live
    # WASAPI stream in the same process is a risk with no upside, because the
    # devices cannot change mid-recording anyway — the UI shows the ones the
    # recording is actually using. An hour-long meeting is not the place to
    # find out how well reference counting holds up.
    if recording is not None:
        if cached:
            return cached
        snapshot = {
            "mic": recording.tracks[0][1].device.name if recording.tracks else None,
            "system": recording.tracks[1][1].device.name if len(recording.tracks) > 1 else None,
        }
        _devices_cache = (now, snapshot)
        return snapshot

    try:
        mic = find_mic_device()
        system = find_system_device()
        snapshot = {"mic": mic.name if mic else None, "system": system.name if system else None}
    except Exception as exc:  # never take the UI down over device enumeration
        snapshot = {"mic": None, "system": None, "error": f"{type(exc).__name__}: {exc}"}

    _devices_cache = (now, snapshot)
    return snapshot


# ── meetings, safely ─────────────────────────────────────────────


def resolve(name: str) -> Meeting:
    """A meeting by folder name, or an error.

    This is an HTTP server and `name` comes off the query string, so the
    resolved path is checked to be a direct child of the archive. Without it,
    `?name=../../..` reads and — for delete — removes whatever it likes.
    """
    candidate = (CONFIG.home / name).resolve()
    if candidate.parent != CONFIG.home.resolve() or not (candidate / "meta.json").is_file():
        raise FileNotFoundError(name)
    return Meeting(candidate)


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
                "speakers": speaker_names(meta, meta.get("speakers") or []),
            }
        )
    return meetings


def speaker_names(meta: dict, speakers: list[str]) -> list[str]:
    mapping = meta.get("speaker_names") or {}
    return [mapping.get(speaker, speaker) for speaker in speakers]


def rename_in_text(text: str, mapping: dict[str, str]) -> str:
    """Apply the mapping inside prose the model wrote.

    Renaming the label on a turn is not enough: the summary says "SPEAKER_01
    asked about pricing" in its own words. Longest label first, so SPEAKER_1
    cannot eat the front of SPEAKER_10.
    """
    for label in sorted(mapping, key=len, reverse=True):
        text = text.replace(label, mapping[label])
    return text


def meeting_detail(name: str) -> dict:
    meeting = resolve(name)
    meta = meeting.load_meta()
    mapping = meta.get("speaker_names") or {}

    notes = None
    if meeting.notes_json.is_file():
        notes = json.loads(meeting.notes_json.read_text(encoding="utf-8"))
    else:
        # Meetings processed before notes.json became a real artifact.
        cached = meeting.read_cached_json("notes.json")
        if cached:
            notes = cached.get("notes")

    if notes and mapping:
        notes = json.loads(rename_in_text(json.dumps(notes, ensure_ascii=False), mapping))

    transcript = None
    detected: list[str] = []
    if meeting.transcript_json.is_file():
        data = json.loads(meeting.transcript_json.read_text(encoding="utf-8"))
        transcript = []
        for segment in data.get("segments", []):
            if segment.get("dropped"):
                continue
            speaker = segment.get("speaker", "")
            if speaker not in detected:
                detected.append(speaker)
            transcript.append(
                {
                    "start": segment["start"],
                    "end": segment.get("end", segment["start"]),
                    "speaker": mapping.get(speaker, speaker),
                    "raw_speaker": speaker,
                    "text": segment["text"],
                }
            )

    return {
        "name": meeting.path.name,
        "meta": meta,
        "notes": notes,
        "transcript": transcript,
        "speaker_names": mapping,
        # The labels as the pipeline produced them, which is what a rename form
        # has to edit — the display names are the output, not the key.
        "raw_speakers": detected or (meta.get("speakers") or []),
        "files": [
            name
            for name, path in (
                ("notes.html", meeting.notes_html),
                ("notes.md", meeting.notes_md),
                ("notes.json", meeting.notes_json),
                ("transcript.json", meeting.transcript_json),
            )
            if path.is_file()
        ],
        "has_audio": bool(
            (meeting.mic_wav.is_file() and meeting.mic_wav.stat().st_size > 44)
            or (meeting.system_wav.is_file() and meeting.system_wav.stat().st_size > 44)
        ),
    }


# ── playback ─────────────────────────────────────────────────────

_playback_locks: dict[str, threading.Lock] = {}
_playback_registry = threading.Lock()


def _playback_lock(name: str) -> threading.Lock:
    with _playback_registry:
        return _playback_locks.setdefault(name, threading.Lock())


def playback_file(meeting: Meeting) -> Path:
    """One compact, seekable file with both tracks mixed, built once.

    The browser is not going to be handed the WAVs: an hour of meeting is
    ~660 MB across the two of them, and they are two files when the thing
    anyone wants to hear is the conversation. ffmpeg mixes them down once and
    the result is cached beside the rest of the meeting's derived data.

    `normalize=0` keeps each track at its own level rather than halving both —
    a silent microphone should not make the remote side quiet — and the limiter
    catches the clipping that occasionally invites.
    """
    target = meeting.cache_path("playback", "mixed.m4a")
    if target.is_file() and target.stat().st_size > 0:
        return target

    sources = [
        path
        for path in (meeting.mic_wav, meeting.system_wav)
        if path.is_file() and path.stat().st_size > 44
    ]
    if not sources:
        raise FileNotFoundError("no audio in this meeting")

    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    for source in sources:
        args += ["-i", str(source)]
    if len(sources) > 1:
        args += [
            "-filter_complex",
            f"amix=inputs={len(sources)}:duration=longest:normalize=0,alimiter=limit=0.95",
        ]
    args += ["-ac", "1", "-c:a", "aac", "-b:a", "64k", "-movflags", "+faststart", str(target)]

    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0 or not target.is_file():
        target.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg could not build the playback file: {result.stderr[:300]}")
    return target


def ensure_playback(meeting: Meeting) -> Path:
    with _playback_lock(meeting.path.name):
        return playback_file(meeting)


def warm_playback(name: str) -> None:
    """Start building the mix when a meeting is opened, not when play is hit."""

    def work() -> None:
        try:
            ensure_playback(resolve(name))
        except Exception:
            pass  # the audio endpoint will report it properly if asked

    threading.Thread(target=work, name=f"playback-{name}", daemon=True).start()


# ── http ─────────────────────────────────────────────────────────

SERVABLE = {
    "notes.html": "text/html; charset=utf-8",
    "notes.md": "text/markdown; charset=utf-8",
    "notes.json": "application/json; charset=utf-8",
    "transcript.json": "application/json; charset=utf-8",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "helmcode-whisper-ui"
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # noqa: ANN002
        pass  # the terminal is for the app's own output, not an access log

    # ── plumbing ─────────────────────────────────────────────

    def _send(self, payload: dict | list, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, body: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_media(self, path: Path, content_type: str, *, head_only: bool = False) -> None:
        """A file, with byte ranges, because an audio element needs to seek.

        Without `Accept-Ranges` and 206 the browser has to download the whole
        thing before it can jump anywhere in it, which for an hour-long meeting
        is the difference between a player and a progress bar.
        """
        size = path.stat().st_size
        start, end, status = 0, size - 1, 200

        header = self.headers.get("Range", "")
        if header.startswith("bytes="):
            first, _, last = header[len("bytes=") :].split(",")[0].partition("-")
            try:
                if first:
                    start = int(first)
                    end = min(int(last), size - 1) if last else size - 1
                elif last:  # a suffix range: the final N bytes
                    start = max(0, size - int(last))
            except ValueError:
                start, end = 0, size - 1
            else:
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = 206

        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if head_only:
            return

        try:
            with path.open("rb") as handle:
                handle.seek(start)
                remaining = length
                while remaining > 0:
                    block = handle.read(min(1 << 16, remaining))
                    if not block:
                        break
                    self.wfile.write(block)
                    remaining -= len(block)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the browser abandons range requests constantly; not an error

    # ── routes ───────────────────────────────────────────────

    def do_HEAD(self) -> None:  # noqa: N802
        route = urlparse(self.path)
        if route.path == "/api/audio":
            try:
                name = parse_qs(route.query)["name"][0]
                self._send_media(ensure_playback(resolve(name)), "audio/mp4", head_only=True)
            except Exception:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        route = urlparse(self.path)
        query = parse_qs(route.query)

        try:
            if route.path == "/":
                self._send_bytes(
                    (HERE / "index.html").read_bytes(), "text/html; charset=utf-8"
                )
            elif route.path == "/api/state":
                with _lock:
                    self._send(
                        {
                            "recording": recording.status() if recording else None,
                            "processing": processing.status() if processing else None,
                            "meetings": list_meetings(),
                            "devices": devices_snapshot(),
                            "home": str(CONFIG.home),
                            "has_key": bool(CONFIG.api_key),
                        }
                    )
            elif route.path == "/api/meeting":
                name = query["name"][0]
                detail = meeting_detail(name)
                if detail["has_audio"]:
                    warm_playback(name)
                self._send(detail)
            elif route.path == "/api/audio":
                self._send_media(ensure_playback(resolve(query["name"][0])), "audio/mp4")
            elif route.path == "/api/file":
                meeting = resolve(query["name"][0])
                wanted = query["file"][0]
                if wanted not in SERVABLE:
                    self._send({"error": "not servable"}, 400)
                    return
                self._send_media(meeting.path / wanted, SERVABLE[wanted])
            elif route.path == "/api/search":
                term = (query.get("q") or [""])[0].strip()
                if not term:
                    self._send({"hits": [], "semantic": True})
                    return
                hits, semantic = search_hits(CONFIG, term, limit=12)
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

        # Inside the try, and checked: this used to sit above it, so a body that
        # was not valid UTF-8 JSON raised straight out of the handler. The
        # client got no response at all and a traceback went to the terminal —
        # for input that arrives over a socket and is not ours to trust.
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._send({"error": "Content-Length inválido"}, 400)
            return
        try:
            payload = json.loads(self.rfile.read(length) or "{}") if length else {}
            if not isinstance(payload, dict):
                raise ValueError("se esperaba un objeto JSON")
        except (ValueError, UnicodeDecodeError) as exc:
            self._send({"error": f"cuerpo ilegible: {exc}"}, 400)
            return

        try:
            if route.path == "/api/record/start":
                with _lock:
                    if recording is not None:
                        self._send({"error": "ya se está grabando"}, 409)
                        return
                    recording = Recording(payload.get("title") or "reunión")
                    self._send(recording.status())
            elif route.path == "/api/record/stop":
                with _lock:
                    if recording is None:
                        self._send({"error": "no se está grabando"}, 409)
                        return
                    result = recording.stop()
                    recording = None
                self._send(result)
            elif route.path == "/api/process":
                with _lock:
                    if processing is not None and not processing.done:
                        self._send({"error": "ya hay un procesado en marcha"}, 409)
                        return
                    processing = Processing(resolve(payload["meeting"]).path.name)
                self._send(processing.status())
            elif route.path == "/api/speakers":
                meeting = resolve(payload["meeting"])
                names = {
                    str(label): str(value).strip()
                    for label, value in (payload.get("names") or {}).items()
                    if str(value).strip()
                }
                meeting.save_meta({"speaker_names": names})
                self._send(meeting_detail(meeting.path.name))
            elif route.path == "/api/rename":
                meeting = resolve(payload["meeting"])
                title = str(payload.get("title") or "").strip()
                if not title:
                    self._send({"error": "el título no puede estar vacío"}, 400)
                    return
                meeting.save_meta({"title": title})
                self._send({"ok": True})
            elif route.path == "/api/delete":
                self._send(delete_meeting(payload["meeting"]))
            elif route.path == "/api/reveal":
                meeting = resolve(payload["meeting"])
                os.startfile(meeting.path)  # noqa: S606 — a local tool, by design
                self._send({"ok": True})
            else:
                self._send({"error": "not found"}, 404)
        except FileNotFoundError:
            self._send({"error": "not found"}, 404)
        except Exception as exc:
            self._send({"error": f"{type(exc).__name__}: {exc}"}, 500)


def delete_meeting(name: str) -> dict:
    """Remove a meeting from disk and from the index, in that order of care."""
    global processing
    meeting = resolve(name)
    with _lock:
        if recording is not None and recording.meeting.path.name == name:
            raise RuntimeError("no se puede borrar la reunión que se está grabando")
        if processing is not None and processing.meeting == name and not processing.done:
            raise RuntimeError("no se puede borrar una reunión mientras se procesa")

    removed = 0
    if CONFIG.db_path.is_file():
        connection = index_connect(CONFIG.db_path)
        try:
            removed = index_delete(connection, name)
        finally:
            connection.close()

    shutil.rmtree(meeting.path)
    with _lock:
        if processing is not None and processing.meeting == name:
            processing = None
    return {"ok": True, "passages_removed": removed}


def main() -> None:
    parser = argparse.ArgumentParser(description="Local interface for helmcode-whisper.")
    parser.add_argument("--host", default="127.0.0.1", help="Default: 127.0.0.1, on purpose.")
    parser.add_argument("--port", type=int, default=7864)
    args = parser.parse_args()

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    if not CONFIG.api_key:
        # The package finds `.env` by walking up from the working directory, so
        # running this from anywhere else silently loses the key and degrades
        # search to keyword matching. Say so before that confuses anyone.
        print("warning: no HELMCODE_API_KEY found.")
        print("         run this from the helmcode-whisper checkout, e.g.")
        print("         cd C:\\dev\\Helmcode-whisper && "
              ".venv\\Scripts\\python.exe ..\\helmcode-whisper-ui\\app.py")
        print("         recording works; processing and semantic search do not.\n")

    if args.host != "127.0.0.1":
        print(f"warning: listening on {args.host}. Your meetings are now reachable "
              "from the network.\n")

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    print(f"helmcode-whisper ui  ->  http://{args.host}:{args.port}")
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
