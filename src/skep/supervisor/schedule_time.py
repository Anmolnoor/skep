"""v135-F7 (ADR 0070 §4): wall-clock daily schedules.

A schedule with a ``timezone`` (an IANA name) and an interval of whole days
fires at the same wall-clock time N calendar days later in that zone, so a
07:00 brief stays at 07:00 across daylight-saving changes. Slots stay UTC
``…Z`` strings: the store compares ``next_run_at`` as a string.

Two edge rules (both tested): a wall time that occurs twice on the fall-back
day fires once, at its first occurrence (``fold=0``); a wall time that does
not exist on the spring-forward day fires at the first valid instant after
the gap. The schedule's ``wall_time`` is its anchor, so a 02:30 slot that
fired at 03:00 on the gap day is back at 02:30 the day after.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DAY_SECONDS = 86400
EXAMPLE = "America/Vancouver"
_FMT = "%Y-%m-%dT%H:%M:%SZ"


def zone(name: str) -> ZoneInfo:
    """The IANA zone ``name``, or a ValueError that names a valid one (I9)."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(
            f"unknown timezone {name!r}: use an IANA name such as {EXAMPLE}, "
            "or leave timezone unset for a fixed interval"
        ) from exc


def check_timezone(name: str, interval_seconds: int) -> str:
    """``name`` if a schedule with this interval can keep wall-clock time."""
    zone(name)
    if interval_seconds % DAY_SECONDS:
        raise ValueError(
            f"timezone needs an interval that is a whole number of days (1d, 7d), "
            f"got {interval_seconds}s: an hourly schedule is a fixed interval, "
            "leave timezone unset"
        )
    return name


def wall_time_of(slot: str, name: str) -> str:
    """``slot``'s wall-clock time in zone ``name``, as ``HH:MM:SS``."""
    return _parse(slot).astimezone(zone(name)).strftime("%H:%M:%S")


def resolve(day: date, wall: time, tz: ZoneInfo) -> datetime:
    """The UTC instant of ``wall`` on ``day`` in ``tz``, by the two edge rules."""
    local = datetime.combine(day, wall, tzinfo=tz)  # fold=0: a repeat's first
    instant = local.astimezone(UTC)
    if instant.astimezone(tz).replace(tzinfo=None) == local.replace(tzinfo=None):
        return instant
    # In a gap. fold=1 reads it with the offset after the change and lands
    # before the transition; fold=0 with the offset before and lands after
    # it. The transition is the first instant on the later offset.
    lo, hi = local.replace(fold=1).astimezone(UTC), instant
    after = hi.astimezone(tz).utcoffset()
    while hi - lo > timedelta(seconds=1):
        mid = lo + (hi - lo) / 2
        mid = mid.replace(microsecond=0)
        if mid.astimezone(tz).utcoffset() == after:
            hi = mid
        else:
            lo = mid
    return hi


def next_wall_slot(
    reference: str, days: int, name: str, *, wall_time: str | None = None, now: str | None = None
) -> str:
    """The slot ``days`` calendar days after ``reference`` at ``wall_time``
    (default: the reference's own wall time). With ``now``, missed slots are
    skipped until one is after ``now``: fire once, no catch-up (v122-F5)."""
    tz = zone(name)
    ref_local = _parse(reference).astimezone(tz)
    wall = time.fromisoformat(wall_time) if wall_time else ref_local.time()
    day = ref_local.date() + timedelta(days=days)
    slot = resolve(day, wall, tz)
    if now is not None and slot <= (moment := _parse(now)):
        behind = (moment.astimezone(tz).date() - day).days
        day += timedelta(days=days * max(0, behind // days))
        while (slot := resolve(day, wall, tz)) <= moment:
            day += timedelta(days=days)
    return slot.strftime(_FMT)


def _parse(ts: str) -> datetime:
    return datetime.strptime(ts, _FMT).replace(tzinfo=UTC)
