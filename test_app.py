"""The server's own logic: path safety, spaces, edited notes, reversible delete.

`ical.py` was tested and `app.py` was not, which had it backwards — the parser
handles text, while this file resolves paths off a query string and then deletes
directories with the result.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest
from helmcode_whisper.config import Config
from helmcode_whisper.store import Meeting

import app


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    """Point the server at an empty archive of its own."""
    archive = tmp_path / "archive"
    archive.mkdir()
    monkeypatch.setattr(
        app,
        "CONFIG",
        Config(
            api_key="test-key",
            base_url="https://api.example.invalid/v1",
            hf_token=None,
            home=archive,
            stt_model="whisper",
            notes_model="notes",
            embed_model="embed",
            rerank_model="rerank",
            stt_concurrency=4,
        ),
    )
    monkeypatch.setattr(app, "recording", None)
    monkeypatch.setattr(app, "processing", None)
    return archive


def make_meeting(home: Path, title: str = "reunión") -> Meeting:
    meeting = Meeting.create(home, title, datetime(2026, 8, 12, 10, 0))
    meeting.notes_json.write_text(
        json.dumps({
            "summary": "original del modelo",
            "decisions": ["una"],
            "action_items": [],
            "open_questions": [],
            "quotes": [],
        }),
        encoding="utf-8",
    )
    meeting.notes_md.write_text("# original\n", encoding="utf-8")
    meeting.notes_html.write_text("<p>original</p>", encoding="utf-8")
    meeting.transcript_json.write_text(
        json.dumps({"segments": [
            {"start": 0.0, "end": 2.0, "text": "hola", "track": "system",
             "speaker": "SPEAKER_00"},
        ]}),
        encoding="utf-8",
    )
    return meeting


# ── resolving a name off the query string ────────────────────────


def test_a_real_meeting_resolves(home: Path) -> None:
    meeting = make_meeting(home)

    assert app.resolve(meeting.path.name).path == meeting.path


@pytest.mark.parametrize(
    "attempt",
    ["..", "../..", "../../Windows", "..\\..\\Windows", "a/../..", "", "."],
)
def test_escaping_the_archive_is_refused(home: Path, attempt: str) -> None:
    """One of the endpoints behind this deletes directories."""
    make_meeting(home)

    with pytest.raises(FileNotFoundError):
        app.resolve(attempt)


def test_a_folder_without_meta_is_not_a_meeting(home: Path) -> None:
    (home / "no-es-una-reunion").mkdir()

    with pytest.raises(FileNotFoundError):
        app.resolve("no-es-una-reunion")


# ── spaces ───────────────────────────────────────────────────────


def test_spaces_keep_their_order_and_ignore_duplicates(home: Path) -> None:
    app.save_spaces(["Clientes", "Interno", "Clientes", "  ", "Interno"])

    assert app.load_spaces() == ["Clientes", "Interno"]


def test_the_unassigned_label_cannot_be_created_as_a_space(home: Path) -> None:
    """It is the name of the group for meetings with no space, not a space."""
    app.save_spaces(["Clientes", app.UNASSIGNED])

    assert app.load_spaces() == ["Clientes"]


def test_renaming_a_space_carries_its_meetings(home: Path) -> None:
    meeting = make_meeting(home)
    app.save_spaces(["Clientes"])
    meeting.save_meta({"space": "Clientes"})

    moved = app.rename_space("Clientes", "Clientes 2026")

    assert moved == 1
    assert app.load_spaces() == ["Clientes 2026"]
    assert meeting.load_meta()["space"] == "Clientes 2026"


def test_deleting_a_space_frees_its_meetings_rather_than_taking_them(home: Path) -> None:
    meeting = make_meeting(home)
    app.save_spaces(["Clientes"])
    meeting.save_meta({"space": "Clientes"})

    freed = app.drop_space("Clientes")

    assert freed == 1
    assert app.load_spaces() == []
    assert meeting.load_meta()["space"] == ""
    assert meeting.path.is_dir()


def test_a_corrupt_registry_reads_as_empty_rather_than_raising(home: Path) -> None:
    app.spaces_path().write_text("{no es json", encoding="utf-8")

    assert app.load_spaces() == []


# ── editing the notes ────────────────────────────────────────────


def test_saving_notes_rewrites_all_three_files(home: Path) -> None:
    meeting = make_meeting(home)

    app.save_notes(meeting, {
        "summary": "corregido a mano",
        "decisions": ["la primera", "la segunda"],
        "action_items": [{"task": "medir", "owner": "Borja", "due": "viernes"}],
        "open_questions": [],
        "quotes": [{"text": "ya funciona", "speaker": "Ana"}],
    })

    stored = json.loads(meeting.notes_json.read_text(encoding="utf-8"))
    assert stored["summary"] == "corregido a mano"
    assert "corregido a mano" in meeting.notes_md.read_text(encoding="utf-8")
    assert "corregido a mano" in meeting.notes_html.read_text(encoding="utf-8")
    assert meeting.load_meta()["notes_edited_at"]


def test_empty_rows_are_dropped_on_the_way_in(home: Path) -> None:
    """The editor lets you add a row and change your mind about filling it."""
    meeting = make_meeting(home)

    result = app.save_notes(meeting, {
        "summary": "x",
        "decisions": ["buena", "   ", ""],
        "action_items": [{"task": "buena"}, {"task": "  ", "owner": "nadie"}],
        "quotes": [{"text": "buena"}, {"speaker": "nadie", "text": ""}],
        "open_questions": [],
    })

    assert result["notes"]["decisions"] == ["buena"]
    assert len(result["notes"]["action_items"]) == 1
    assert len(result["notes"]["quotes"]) == 1


def test_a_newline_separated_string_is_accepted_as_a_list(home: Path) -> None:
    """The textareas send one item per line."""
    meeting = make_meeting(home)

    result = app.save_notes(meeting, {"summary": "x", "decisions": "una\ndos\n\ntres"})

    assert result["notes"]["decisions"] == ["una", "dos", "tres"]


def test_the_model_original_survives_an_edit(home: Path) -> None:
    """Which is why the UI warns before reprocessing restores it."""
    meeting = make_meeting(home)
    meeting.write_cached_json({"notes": {"summary": "del modelo"}, "stats": {}}, "notes.json")

    app.save_notes(meeting, {"summary": "mío"})

    assert meeting.read_cached_json("notes.json")["notes"]["summary"] == "del modelo"


# ── deleting ─────────────────────────────────────────────────────


def test_deleting_moves_to_the_trash_instead_of_erasing(home: Path) -> None:
    meeting = make_meeting(home)
    name = meeting.path.name

    result = app.delete_meeting(name)

    assert result["ok"] is True
    assert not meeting.path.exists()
    assert (home / app.TRASH / name / "notes.md").is_file()


def test_the_trash_is_not_listed_as_a_meeting(home: Path) -> None:
    """`.trash/<name>/meta.json` is one level too deep to be picked up."""
    meeting = make_meeting(home)
    app.delete_meeting(meeting.path.name)

    assert app.list_meetings() == []


def test_deleting_the_same_title_twice_keeps_both_copies(home: Path) -> None:
    first = make_meeting(home, "precios")
    app.delete_meeting(first.path.name)
    second = make_meeting(home, "precios")

    app.delete_meeting(second.path.name)

    assert len(list((home / app.TRASH).iterdir())) == 2


def test_the_meeting_being_recorded_cannot_be_deleted(home: Path, monkeypatch) -> None:
    meeting = make_meeting(home)

    class Live:
        pass

    live = Live()
    live.meeting = meeting
    monkeypatch.setattr(app, "recording", live)

    with pytest.raises(RuntimeError, match="grabando"):
        app.delete_meeting(meeting.path.name)
    assert meeting.path.is_dir()
