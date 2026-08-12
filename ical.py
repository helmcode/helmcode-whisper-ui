"""Just enough iCalendar to know what meeting you are in and who is in it.

Not a general parser. The job is narrow: given a calendar, list the events near
a point in time with their titles and their attendees, so a recording can be
named after the meeting it belongs to and the speaker-name guesser can be handed
a closed list of people instead of an open question.

What it handles, because real calendars contain it:

- Folded lines. RFC 5545 wraps long values onto continuation lines starting with
  a space, which means naive line-splitting cuts titles and email addresses in
  half.
- Escaped text. `\\,` `\\;` `\\n` inside SUMMARY and CN values.
- The three DTSTART shapes: UTC with a trailing Z, a local time with a TZID,
  and an all-day VALUE=DATE.
- Simple DAILY and WEEKLY recurrence, with INTERVAL, COUNT, UNTIL and BYDAY,
  plus EXDATE — a weekly meeting is the most common thing on anyone's calendar,
  and one that was cancelled last Tuesday should not show up as happening.

What it does not handle, and says so rather than guessing: MONTHLY and YEARLY
recurrence, BYMONTHDAY and friends, RDATE, and per-instance overrides via
RECURRENCE-ID. Timezones are resolved through the standard library's zoneinfo
when a TZID names something it recognises, and fall back to local time when it
does not.
"""

from __future__ import annotations

import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

FETCH_TIMEOUT = 15.0
MAX_BYTES = 8 * 1024 * 1024

_WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
# Expanding a recurrence needs a stop. Nobody is naming a recording after a
# meeting a year from now.
_MAX_OCCURRENCES = 400


@dataclass(frozen=True)
class Attendee:
    name: str
    email: str

    @property
    def label(self) -> str:
        return self.name or self.email


@dataclass(frozen=True)
class Event:
    summary: str
    start: datetime
    end: datetime
    attendees: tuple[Attendee, ...] = ()
    organizer: Attendee | None = None
    all_day: bool = False
    uid: str = ""

    @property
    def people(self) -> list[str]:
        """Everyone on the invitation, by the best name we have for them."""
        seen: dict[str, None] = {}
        for person in ([self.organizer] if self.organizer else []) + list(self.attendees):
            if person.label:
                seen.setdefault(person.label, None)
        return list(seen)

    def covers(self, moment: datetime) -> bool:
        return self.start <= moment <= self.end

    def to_dict(self) -> dict:
        return {
            "uid": self.uid,
            "summary": self.summary,
            "start": self.start.isoformat(timespec="minutes"),
            "end": self.end.isoformat(timespec="minutes"),
            "all_day": self.all_day,
            "people": self.people,
            "attendees": [{"name": a.name, "email": a.email} for a in self.attendees],
        }


# ── reading ──────────────────────────────────────────────────────


def load(source: str) -> str:
    """A calendar from a URL or a path on disk.

    A URL is a request to somebody else's server — for Google's "secret address
    in iCal format", to Google. That is why this lives in the interface and not
    in the helmcode-whisper package, which promises that meeting content only
    ever reaches one host and has a test that enforces it.
    """
    if source.startswith(("http://", "https://", "webcal://")):
        url = source.replace("webcal://", "https://", 1)
        request = urllib.request.Request(url, headers={"User-Agent": "helmcode-whisper-ui"})
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as response:  # noqa: S310
            return response.read(MAX_BYTES).decode("utf-8", "replace")
    return Path(source).expanduser().read_text(encoding="utf-8", errors="replace")


def unfold(text: str) -> list[str]:
    """Join RFC 5545 continuation lines back onto the line they belong to."""
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _split_property(line: str) -> tuple[str, dict[str, str], str]:
    """`ATTENDEE;CN=Ana:mailto:ana@x.com` -> name, params, value."""
    head, _, value = line.partition(":")
    pieces = head.split(";")
    name = pieces[0].upper()
    params: dict[str, str] = {}
    for piece in pieces[1:]:
        key, _, val = piece.partition("=")
        params[key.upper()] = val.strip('"')
    return name, params, value


def _unescape(value: str) -> str:
    out = value.replace("\\n", "\n").replace("\\N", "\n")
    return out.replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")


def _zone(tzid: str | None) -> timezone | ZoneInfo | None:
    if not tzid:
        return None
    try:
        return ZoneInfo(tzid)
    except (ZoneInfoNotFoundError, ValueError):
        return None  # unknown zone: treat as local rather than refuse the event


def _parse_moment(value: str, params: dict[str, str]) -> tuple[datetime, bool]:
    """A DTSTART/DTEND value as a local-naive datetime, plus whether it's all-day."""
    value = value.strip()
    if params.get("VALUE") == "DATE" or (len(value) == 8 and "T" not in value):
        day = datetime.strptime(value, "%Y%m%d")
        return day, True

    if value.endswith("Z"):
        moment = datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        return moment.astimezone().replace(tzinfo=None), False

    moment = datetime.strptime(value, "%Y%m%dT%H%M%S")
    zone = _zone(params.get("TZID"))
    if zone is not None:
        moment = moment.replace(tzinfo=zone).astimezone().replace(tzinfo=None)
    return moment, False


def _parse_person(params: dict[str, str], value: str) -> Attendee:
    email = value.strip()
    for prefix in ("mailto:", "MAILTO:"):
        if email.startswith(prefix):
            email = email[len(prefix) :]
    return Attendee(name=_unescape(params.get("CN", "")).strip(), email=email)


# ── recurrence ───────────────────────────────────────────────────


@dataclass
class _Rule:
    freq: str = ""
    interval: int = 1
    count: int | None = None
    until: datetime | None = None
    byday: list[int] = field(default_factory=list)


def _parse_rule(value: str) -> _Rule:
    rule = _Rule()
    for part in value.split(";"):
        key, _, val = part.partition("=")
        key = key.upper()
        if key == "FREQ":
            rule.freq = val.upper()
        elif key == "INTERVAL" and val.isdigit():
            rule.interval = max(1, int(val))
        elif key == "COUNT" and val.isdigit():
            rule.count = int(val)
        elif key == "UNTIL":
            try:
                rule.until = _parse_moment(val, {})[0]
            except ValueError:
                rule.until = None
        elif key == "BYDAY":
            rule.byday = [
                _WEEKDAYS[token[-2:].upper()]
                for token in val.split(",")
                if token[-2:].upper() in _WEEKDAYS
            ]
    return rule


def _occurrences(
    start: datetime, rule: _Rule, window_from: datetime, window_to: datetime
) -> list[datetime]:
    """Start times of a recurring event inside the window.

    Only DAILY and WEEKLY. Everything else returns the original occurrence
    alone: showing one real instance beats fabricating a schedule.
    """
    if rule.freq not in ("DAILY", "WEEKLY"):
        return [start]

    step = timedelta(days=rule.interval) if rule.freq == "DAILY" else timedelta(
        weeks=rule.interval
    )
    days = rule.byday or [start.weekday()]

    results: list[datetime] = []
    cursor, emitted = start, 0
    while cursor <= window_to and emitted < _MAX_OCCURRENCES:
        if rule.until and cursor > rule.until:
            break
        week = [cursor] if rule.freq == "DAILY" else [
            cursor + timedelta(days=(day - cursor.weekday())) for day in sorted(days)
        ]
        for moment in week:
            if moment < start:
                continue
            if rule.until and moment > rule.until:
                continue
            emitted += 1
            if rule.count is not None and emitted > rule.count:
                return results
            if window_from <= moment <= window_to:
                results.append(moment)
        cursor += step
    return results


# ── parsing ──────────────────────────────────────────────────────


def parse(text: str, *, window_from: datetime, window_to: datetime) -> list[Event]:
    """Every event overlapping the window, recurrences expanded where supported."""
    events: list[Event] = []
    current: dict | None = None

    for line in unfold(text):
        if line == "BEGIN:VEVENT":
            current = {"attendees": [], "exdates": set()}
            continue
        if line == "END:VEVENT":
            if current is not None:
                events.extend(_build(current, window_from, window_to))
            current = None
            continue
        if current is None or ":" not in line:
            continue

        name, params, value = _split_property(line)
        try:
            if name == "SUMMARY":
                current["summary"] = _unescape(value).strip()
            elif name == "UID":
                current["uid"] = value.strip()
            elif name == "DTSTART":
                current["start"], current["all_day"] = _parse_moment(value, params)
            elif name == "DTEND":
                current["end"], _ = _parse_moment(value, params)
            elif name == "DURATION":
                current["duration"] = value.strip()
            elif name == "RRULE":
                current["rule"] = _parse_rule(value)
            elif name == "EXDATE":
                for piece in value.split(","):
                    current["exdates"].add(_parse_moment(piece, params)[0])
            elif name == "ATTENDEE":
                current["attendees"].append(_parse_person(params, value))
            elif name == "ORGANIZER":
                current["organizer"] = _parse_person(params, value)
        except (ValueError, KeyError):
            continue  # one malformed property should not lose the whole calendar

    events.sort(key=lambda event: event.start)
    return events


def _build(raw: dict, window_from: datetime, window_to: datetime) -> list[Event]:
    start = raw.get("start")
    if start is None:
        return []

    if raw.get("end") is not None:
        length = raw["end"] - start
    elif raw.get("all_day"):
        length = timedelta(days=1)
    else:
        length = timedelta(hours=1)
    if length <= timedelta(0):
        length = timedelta(minutes=30)

    rule = raw.get("rule")
    starts = (
        _occurrences(start, rule, window_from - length, window_to)
        if rule
        else ([start] if start <= window_to and start + length >= window_from else [])
    )

    out: list[Event] = []
    for moment in starts:
        if moment in raw["exdates"]:
            continue
        out.append(
            Event(
                summary=raw.get("summary", "") or "(sin título)",
                start=moment,
                end=moment + length,
                attendees=tuple(raw["attendees"]),
                organizer=raw.get("organizer"),
                all_day=bool(raw.get("all_day")),
                uid=raw.get("uid", ""),
            )
        )
    return out


def around(source: str, moment: datetime, *, hours: float = 12.0) -> list[Event]:
    """Events near a point in time, nearest first."""
    span = timedelta(hours=hours)
    events = parse(load(source), window_from=moment - span, window_to=moment + span)
    return sorted(events, key=lambda event: abs((event.start - moment).total_seconds()))


def current(events: list[Event], moment: datetime) -> Event | None:
    """The event happening now, or the one about to, within fifteen minutes."""
    for event in events:
        if event.all_day:
            continue
        if event.covers(moment):
            return event
    upcoming = [
        event
        for event in events
        if not event.all_day and timedelta(0) <= event.start - moment <= timedelta(minutes=15)
    ]
    return min(upcoming, key=lambda event: event.start) if upcoming else None
