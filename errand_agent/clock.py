"""Clocks. Everything time-dependent (hold expiry, wall-clock limits, slot dates)
reads from a Clock so evals can control time deterministically."""
from __future__ import annotations

import time
from datetime import datetime, timedelta


class Clock:
    def now(self) -> datetime:
        raise NotImplementedError

    def monotonic(self) -> float:
        raise NotImplementedError

    def sleep(self, seconds: float) -> None:
        raise NotImplementedError


class SystemClock(Clock):
    def now(self) -> datetime:
        return datetime.now().replace(microsecond=0)

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class FakeClock(Clock):
    """Deterministic clock. `sleep`/`advance` move both wall time and monotonic time."""

    def __init__(self, start: datetime | None = None):
        self._now = start or datetime(2026, 11, 9, 8, 0, 0)  # a Monday
        self._mono = 0.0

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._mono

    def sleep(self, seconds: float) -> None:
        self.advance(seconds=seconds)

    def advance(self, seconds: float = 0, minutes: float = 0) -> None:
        delta = seconds + minutes * 60
        self._now += timedelta(seconds=delta)
        self._mono += delta
