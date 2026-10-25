"""Rate gate for Tidal calls: token bucket plus a persisted pause on HTTP 429."""
from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

LADDER_S = (15 * 60, 60 * 60, 4 * 60 * 60)
MIN_RETRY_AFTER_S = 60


class GatePaused(Exception):
    def __init__(self, until: datetime, reason: str):
        super().__init__(f"gate paused until {until.isoformat()}: {reason}")
        self.until = until
        self.reason = reason


def _response_429(exc: BaseException):
    """The first object in the exception's cause/context chain whose `.response.status_code` is 429."""
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if getattr(getattr(exc, "response", None), "status_code", None) == 429:
            return exc.response
        exc = exc.__cause__ or exc.__context__
    return None


def _is_too_many_requests(exc: BaseException) -> bool:
    # Duck-typed on purpose: only cadence/sources may import tidalapi. Also catches errors raised
    # while tidalapi handles a 429 (e.g. int(Retry-After) -> ValueError), via the chained response.
    return any(c.__name__ == "TooManyRequests" for c in type(exc).__mro__) or _response_429(exc) is not None


def _retry_after(exc: BaseException):
    raw = getattr(exc, "retry_after", None)
    if raw is None:
        resp = _response_429(exc)
        raw = getattr(resp, "headers", {}).get("Retry-After") if resp is not None else None
    return raw


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class Gate:
    def __init__(self, name: str, min_interval: float = 2.0, burst: int = 3, *,
                 clock=time.monotonic, sleep=time.sleep,
                 now=lambda: datetime.now(timezone.utc),
                 load_state: Callable[[str], str | None] | None = None,
                 save_state: Callable[[str, str], None] | None = None):
        self.name = name
        self.min_interval = min_interval
        self.burst = burst
        self.calls = 0
        self._clock = clock
        self._sleep = sleep
        self._now = now
        self._save_state = save_state
        self._tokens = float(burst)
        self._last = clock()
        self._load_state = load_state
        self._trips = 0
        self._reason = "paused"
        self._paused_until: datetime | None = None
        self._refresh()

    @property
    def _key(self) -> str:
        return f"gate.{self.name}.paused_until"

    @property
    def _trips_key(self) -> str:
        return f"gate.{self.name}.trips"

    def _refresh(self) -> None:
        """Adopt state persisted by another process: the later pause wins, the ladder position follows."""
        if not self._load_state:
            return
        saved = _parse_iso(self._load_state(self._key))
        if saved and (self._paused_until is None or saved > self._paused_until):
            self._paused_until = saved
            self._reason = "paused earlier (restored from saved state)"
        try:
            self._trips = max(0, int(self._load_state(self._trips_key)))
        except (TypeError, ValueError):
            pass

    def _set_trips(self, n: int) -> None:
        self._trips = n
        if self._save_state:
            self._save_state(self._trips_key, str(n))

    def paused_until(self) -> datetime | None:
        if self._paused_until and self._paused_until > self._now():
            return self._paused_until
        return None

    def trip(self, retry_after: int | None, reason: str) -> datetime:
        if retry_after is not None and retry_after > 0:
            seconds = max(MIN_RETRY_AFTER_S, retry_after)
        else:
            seconds = LADDER_S[min(self._trips, len(LADDER_S) - 1)]
        self._paused_until = self._now() + timedelta(seconds=seconds)
        self._reason = reason
        if self._save_state:
            self._save_state(self._key, self._paused_until.isoformat())
        self._set_trips(self._trips + 1)
        return self._paused_until

    def _acquire(self) -> None:
        now = self._clock()
        self._tokens = min(float(self.burst), self._tokens + (now - self._last) / self.min_interval)
        self._last = now
        if self._tokens >= 1:
            self._tokens -= 1
            return
        self._sleep((1 - self._tokens) * self.min_interval)
        self._tokens = 0.0  # the token that just arrived is spent on this call
        self._last = self._clock()

    def call(self, fn, *args, **kwargs):
        self._refresh()
        until = self.paused_until()
        if until:
            raise GatePaused(until, self._reason)
        self._acquire()
        self.calls += 1
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:
            if not _is_too_many_requests(exc):
                raise
            raw = _retry_after(exc)
            try:
                retry_after = int(raw) if raw is not None else None
            except (TypeError, ValueError):
                retry_after = None
            reason = f"TooManyRequests (retry_after={raw})"
            raise GatePaused(self.trip(retry_after, reason), reason) from exc
        if self._trips:
            self._set_trips(0)
        return result
