"""The iCalendar subset, including the shapes real calendars actually emit."""

from __future__ import annotations

from datetime import datetime, timedelta

import ical


def calendar(*body: str) -> str:
    return "\r\n".join(["BEGIN:VCALENDAR", "VERSION:2.0", *body, "END:VCALENDAR"])


def event(**fields: str) -> str:
    lines = ["BEGIN:VEVENT"]
    lines += [f"{key}:{value}" for key, value in fields.items()]
    lines.append("END:VEVENT")
    return "\r\n".join(lines)


WINDOW = {
    "window_from": datetime(2026, 8, 12, 0, 0),
    "window_to": datetime(2026, 8, 13, 0, 0),
}


# ── the format's traps ───────────────────────────────────────────


def test_folded_lines_are_rejoined() -> None:
    """RFC 5545 wraps long values; splitting on newlines cuts titles in half.

    The whitespace opening a continuation line is the fold marker and is
    removed, so a generator that needs a space in the content puts it before
    the break. Both halves are joined with nothing between them.
    """
    text = calendar(
        "BEGIN:VEVENT\r\nSUMMARY:Revisión trimestral de precios con el \r\n"
        " equipo de ventas\r\nDTSTART:20260812T100000\r\nDTEND:20260812T110000\r\n"
        "END:VEVENT"
    )
    events = ical.parse(text, **WINDOW)

    assert events[0].summary == "Revisión trimestral de precios con el equipo de ventas"


def test_a_fold_in_the_middle_of_a_word_is_joined_without_a_gap() -> None:
    """The case that proves the marker is dropped rather than kept as a space."""
    text = calendar(
        "BEGIN:VEVENT\r\nSUMMARY:presu\r\n puesto\r\n"
        "DTSTART:20260812T100000\r\nDTEND:20260812T110000\r\nEND:VEVENT"
    )

    assert ical.parse(text, **WINDOW)[0].summary == "presupuesto"


def test_escaped_characters_are_unescaped() -> None:
    text = calendar(event(
        SUMMARY=r"Precios\, márgenes y descuentos\; Q3",
        DTSTART="20260812T100000", DTEND="20260812T110000",
    ))

    assert ical.parse(text, **WINDOW)[0].summary == "Precios, márgenes y descuentos; Q3"


def test_utc_times_are_converted_to_local() -> None:
    text = calendar(event(DTSTART="20260812T090000Z", DTEND="20260812T100000Z", SUMMARY="x"))

    parsed = ical.parse(text, **WINDOW)[0]

    # Whatever this machine's offset is, the hour must not be read as 09:00 local.
    expected = (
        datetime(2026, 8, 12, 9, 0, tzinfo=ical.timezone.utc).astimezone().replace(tzinfo=None)
    )
    assert parsed.start == expected


def test_a_tzid_time_is_converted_too() -> None:
    text = calendar(
        "BEGIN:VEVENT\r\nSUMMARY:x\r\nDTSTART;TZID=Europe/Madrid:20260812T170000\r\n"
        "DTEND;TZID=Europe/Madrid:20260812T180000\r\nEND:VEVENT"
    )
    parsed = ical.parse(text, **WINDOW)[0]
    expected = (
        datetime(2026, 8, 12, 17, 0, tzinfo=ical.ZoneInfo("Europe/Madrid"))
        .astimezone()
        .replace(tzinfo=None)
    )
    assert parsed.start == expected


def test_an_unknown_timezone_is_treated_as_local_rather_than_dropped() -> None:
    text = calendar(
        "BEGIN:VEVENT\r\nSUMMARY:x\r\nDTSTART;TZID=Mars/Olympus:20260812T170000\r\n"
        "DTEND;TZID=Mars/Olympus:20260812T180000\r\nEND:VEVENT"
    )
    parsed = ical.parse(text, **WINDOW)

    assert len(parsed) == 1
    assert parsed[0].start == datetime(2026, 8, 12, 17, 0)


def test_an_all_day_event_is_marked_and_lasts_a_day() -> None:
    text = calendar(
        "BEGIN:VEVENT\r\nSUMMARY:Festivo\r\nDTSTART;VALUE=DATE:20260812\r\nEND:VEVENT"
    )
    parsed = ical.parse(text, **WINDOW)[0]

    assert parsed.all_day is True
    assert parsed.end - parsed.start == timedelta(days=1)


def test_a_missing_dtend_gets_an_hour() -> None:
    text = calendar(event(SUMMARY="x", DTSTART="20260812T100000"))

    assert ical.parse(text, **WINDOW)[0].end == datetime(2026, 8, 12, 11, 0)


def test_one_broken_property_does_not_lose_the_calendar() -> None:
    text = calendar(
        event(SUMMARY="roto", DTSTART="no-es-una-fecha", DTEND="20260812T110000"),
        event(SUMMARY="bueno", DTSTART="20260812T120000", DTEND="20260812T130000"),
    )
    parsed = ical.parse(text, **WINDOW)

    assert [e.summary for e in parsed] == ["bueno"]


# ── people ───────────────────────────────────────────────────────


def test_attendees_come_back_with_names_and_addresses() -> None:
    text = calendar(
        "BEGIN:VEVENT\r\nSUMMARY:x\r\nDTSTART:20260812T100000\r\nDTEND:20260812T110000\r\n"
        'ATTENDEE;CN="Ana Pérez";ROLE=REQ-PARTICIPANT:mailto:ana@example.com\r\n'
        "ATTENDEE;CN=Luis:mailto:luis@example.com\r\n"
        "ATTENDEE:mailto:sinnombre@example.com\r\n"
        "ORGANIZER;CN=Borja:mailto:borja@example.com\r\nEND:VEVENT"
    )
    parsed = ical.parse(text, **WINDOW)[0]

    assert [a.name for a in parsed.attendees] == ["Ana Pérez", "Luis", ""]
    assert parsed.attendees[0].email == "ana@example.com"
    # The organizer leads, and someone with no CN falls back to their address:
    # a closed list of five people is the point, even if one is an email.
    assert parsed.people == ["Borja", "Ana Pérez", "Luis", "sinnombre@example.com"]


# ── recurrence ───────────────────────────────────────────────────


def test_a_weekly_meeting_is_expanded() -> None:
    text = calendar(event(
        SUMMARY="Semanal", DTSTART="20260805T100000", DTEND="20260805T110000",
        RRULE="FREQ=WEEKLY;BYDAY=WE",
    ))
    parsed = ical.parse(
        text, window_from=datetime(2026, 8, 1), window_to=datetime(2026, 9, 1)
    )

    starts = [e.start.date().isoformat() for e in parsed]
    assert starts == ["2026-08-05", "2026-08-12", "2026-08-19", "2026-08-26"]


def test_a_cancelled_instance_does_not_show_up() -> None:
    """EXDATE is why last Tuesday's cancelled standup must not appear."""
    text = calendar(event(
        SUMMARY="Semanal", DTSTART="20260805T100000", DTEND="20260805T110000",
        RRULE="FREQ=WEEKLY;BYDAY=WE", EXDATE="20260812T100000",
    ))
    parsed = ical.parse(
        text, window_from=datetime(2026, 8, 1), window_to=datetime(2026, 9, 1)
    )

    assert "2026-08-12" not in [e.start.date().isoformat() for e in parsed]


def test_count_and_until_stop_the_series() -> None:
    counted = calendar(event(
        SUMMARY="x", DTSTART="20260805T100000", DTEND="20260805T110000",
        RRULE="FREQ=WEEKLY;COUNT=2",
    ))
    parsed = ical.parse(
        counted, window_from=datetime(2026, 8, 1), window_to=datetime(2026, 9, 1)
    )
    assert len(parsed) == 2

    bounded = calendar(event(
        SUMMARY="x", DTSTART="20260805T100000", DTEND="20260805T110000",
        RRULE="FREQ=DAILY;UNTIL=20260807T100000",
    ))
    parsed = ical.parse(
        bounded, window_from=datetime(2026, 8, 1), window_to=datetime(2026, 9, 1)
    )
    assert [e.start.date().isoformat() for e in parsed] == [
        "2026-08-05", "2026-08-06", "2026-08-07"
    ]


def test_unsupported_recurrence_yields_one_real_instance_not_a_guess() -> None:
    text = calendar(event(
        SUMMARY="Mensual", DTSTART="20260812T100000", DTEND="20260812T110000",
        RRULE="FREQ=MONTHLY;BYMONTHDAY=12",
    ))
    parsed = ical.parse(text, **WINDOW)

    assert len(parsed) == 1
    assert parsed[0].start == datetime(2026, 8, 12, 10, 0)


# ── picking the meeting you are in ───────────────────────────────


def test_current_prefers_the_meeting_happening_now() -> None:
    events = [
        ical.Event("Antes", datetime(2026, 8, 12, 9, 0), datetime(2026, 8, 12, 9, 30)),
        ical.Event("Ahora", datetime(2026, 8, 12, 10, 0), datetime(2026, 8, 12, 11, 0)),
    ]

    assert ical.current(events, datetime(2026, 8, 12, 10, 15)).summary == "Ahora"


def test_current_takes_the_one_about_to_start() -> None:
    events = [ical.Event("En diez", datetime(2026, 8, 12, 10, 0), datetime(2026, 8, 12, 11, 0))]

    assert ical.current(events, datetime(2026, 8, 12, 9, 50)).summary == "En diez"
    # ...but not one that is an hour away.
    assert ical.current(events, datetime(2026, 8, 12, 8, 30)) is None


def test_current_ignores_all_day_entries() -> None:
    """A holiday spanning the day is not the meeting being recorded."""
    events = [
        ical.Event("Festivo", datetime(2026, 8, 12), datetime(2026, 8, 13), all_day=True),
    ]

    assert ical.current(events, datetime(2026, 8, 12, 10, 15)) is None
