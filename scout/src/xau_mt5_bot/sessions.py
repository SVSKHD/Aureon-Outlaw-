from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .config import SessionConfig
from .models import SessionName


@dataclass(frozen=True, slots=True)
class SessionBoundary:
    timestamp: datetime
    kind: str
    session: SessionName


class SessionEngine:
    """DST-aware, half-open session lifecycle in UTC."""

    def __init__(self, config: SessionConfig) -> None:
        self.config = config

    @staticmethod
    def _parse(value: str) -> time:
        hour, minute = (int(part) for part in value.split(":"))
        return time(hour, minute)

    @staticmethod
    def _local_to_utc(day: date, value: str, zone: str) -> datetime:
        local = datetime.combine(day, SessionEngine._parse(value), ZoneInfo(zone))
        return local.astimezone(UTC)

    def boundaries_for_utc_day(self, utc_day: date) -> list[SessionBoundary]:
        result: list[SessionBoundary] = []
        # Local dates around a UTC day are considered so boundaries never disappear at DST/date edges.
        for delta in (-1, 0, 1):
            probe = utc_day + timedelta(days=delta)
            result.extend(
                [
                    SessionBoundary(
                        self._local_to_utc(probe, self.config.asia_open, self.config.asia_timezone),
                        "START", SessionName.ASIA,
                    ),
                    SessionBoundary(
                        self._local_to_utc(probe, self.config.london_open, self.config.london_timezone),
                        "START", SessionName.LONDON,
                    ),
                    SessionBoundary(
                        self._local_to_utc(probe, self.config.new_york_open, self.config.new_york_timezone),
                        "START", SessionName.NEW_YORK,
                    ),
                    SessionBoundary(
                        self._local_to_utc(probe, self.config.new_york_close, self.config.new_york_timezone),
                        "CLOSE", SessionName.NEW_YORK,
                    ),
                ]
            )
            early = self.config.market_early_closes.get(probe.isoformat())               # v2.0.0 item 9: early close is a real CLOSE boundary
            if early:
                early_utc = self._local_to_utc(probe, early, self.config.new_york_timezone)
                # the early close ends whichever session is running at that time (NY, or London if it closes before NY opens)
                running = self._session_started_before(probe, early_utc)
                result.append(SessionBoundary(early_utc, "CLOSE", running or SessionName.NEW_YORK))
        start = datetime.combine(utc_day, time.min, UTC)
        end = start + timedelta(days=1)
        return sorted({item for item in result if start <= item.timestamp < end}, key=lambda x: x.timestamp)

    def _session_started_before(self, probe: date, moment: datetime) -> SessionName | None:
        starts = [
            (self._local_to_utc(probe, self.config.asia_open, self.config.asia_timezone), SessionName.ASIA),
            (self._local_to_utc(probe, self.config.london_open, self.config.london_timezone), SessionName.LONDON),
            (self._local_to_utc(probe, self.config.new_york_open, self.config.new_york_timezone), SessionName.NEW_YORK),
        ]
        before = [(t, name) for t, name in starts if t <= moment]
        return max(before, key=lambda item: item[0])[1] if before else None

    @staticmethod
    def market_open(now: datetime) -> bool:
        """Forex/gold weekend: closed from Friday 17:00 New York until Sunday 17:00 New York."""
        local = now.astimezone(ZoneInfo("America/New_York"))
        wd, t = local.weekday(), local.time()
        if wd == 5: return False
        if wd == 4 and t >= time(17, 0): return False
        if wd == 6 and t < time(17, 0): return False
        return True

    def calendar_open(self, now: datetime) -> bool:
        """Configured broker calendar layered over the normal FX weekend."""
        if not self.market_open(now):
            return False
        local = now.astimezone(ZoneInfo("America/New_York"))
        key = local.date().isoformat()
        if key in set(self.config.market_holidays):
            return False
        early = self.config.market_early_closes.get(key)
        if early and local.time().replace(tzinfo=None) >= self._parse(early):
            return False
        return True

    def session_at(self, now: datetime) -> SessionName:
        now = now.astimezone(UTC)
        if not self.calendar_open(now):
            return SessionName.CLOSED
        events: list[SessionBoundary] = []
        for delta in (-2, -1, 0, 1):
            events.extend(self.boundaries_for_utc_day((now + timedelta(days=delta)).date()))
        current = SessionName.CLOSED
        for event in sorted(set(events), key=lambda item: item.timestamp):
            if event.timestamp > now:
                break
            current = event.session if event.kind == "START" else SessionName.CLOSED
        return current

    def events_between(self, previous: datetime, now: datetime) -> list[SessionBoundary]:
        previous, now = previous.astimezone(UTC), now.astimezone(UTC)
        if now < previous:
            raise ValueError("now must not be before previous")
        days = (now.date() - previous.date()).days
        values: list[SessionBoundary] = []
        for offset in range(days + 1):
            values.extend(self.boundaries_for_utc_day(previous.date() + timedelta(days=offset)))
        return [event for event in sorted(set(values), key=lambda x: x.timestamp) if previous < event.timestamp <= now]

    def most_recent_friday_close(self, now: datetime) -> datetime:
        """Latest configured Friday NY close at or before now, including configured early closes."""
        local = now.astimezone(ZoneInfo(self.config.new_york_timezone))
        friday = local.date() - timedelta(days=(local.weekday() - 4) % 7)
        close_text = self.config.market_early_closes.get(friday.isoformat(), self.config.new_york_close)
        close_local = datetime.combine(friday, self._parse(close_text), ZoneInfo(self.config.new_york_timezone))
        if close_local > local:
            friday -= timedelta(days=7)
            close_text = self.config.market_early_closes.get(friday.isoformat(), self.config.new_york_close)
            close_local = datetime.combine(friday, self._parse(close_text), ZoneInfo(self.config.new_york_timezone))
        return close_local.astimezone(UTC)

    @staticmethod
    def broker_trading_date(timestamp: datetime, rollover_zone: str = "America/New_York") -> date:
        """Forex trading date using the conventional 17:00 New York rollover."""
        local = timestamp.astimezone(ZoneInfo(rollover_zone))
        if local.timetz().replace(tzinfo=None) >= time(17, 0):
            return local.date() + timedelta(days=1)
        return local.date()
