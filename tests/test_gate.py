from datetime import datetime, timedelta, timezone

import pytest

from cadence.gate.bucket import Gate, GatePaused

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


class TooManyRequests(Exception):
    """Same class name as tidalapi's; the gate must duck-type on it."""

    def __init__(self, retry_after=None):
        super().__init__("429")
        if retry_after is not None:
            self.retry_after = retry_after


class FakeTime:
    def __init__(self):
        self.t = 0.0
        self.sleeps: list[float] = []

    def clock(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s

    def now(self):
        return T0 + timedelta(seconds=self.t)

    def advance(self, s):
        self.t += s


def make_gate(ft, **kw):
    return Gate("tidal", clock=ft.clock, sleep=ft.sleep, now=ft.now, **kw)


def ok():
    return "ok"


def boom(retry_after=None):
    def fn():
        raise TooManyRequests(retry_after)
    return fn


def test_burst_then_spacing():
    ft = FakeTime()
    g = make_gate(ft)
    for _ in range(3):
        assert g.call(ok) == "ok"
    assert ft.sleeps == []
    g.call(ok)
    g.call(ok)
    assert ft.sleeps == pytest.approx([2.0, 2.0])
    assert g.calls == 5


def test_tokens_refill_up_to_burst_only():
    ft = FakeTime()
    g = make_gate(ft)
    for _ in range(3):
        g.call(ok)
    ft.advance(100)
    for _ in range(3):
        g.call(ok)
    assert ft.sleeps == []
    g.call(ok)
    assert ft.sleeps == pytest.approx([2.0])


def test_partial_refill_sleeps_only_the_remainder():
    ft = FakeTime()
    g = make_gate(ft, burst=1)
    g.call(ok)
    ft.advance(0.5)
    g.call(ok)
    assert ft.sleeps == pytest.approx([1.5])


def test_passes_args_and_kwargs():
    g = make_gate(FakeTime())
    assert g.call(lambda a, b=0: a + b, 1, b=2) == 3


def test_other_exceptions_pass_through_without_tripping():
    g = make_gate(FakeTime())
    with pytest.raises(ValueError):
        g.call(lambda: (_ for _ in ()).throw(ValueError("nope")))
    assert g.paused_until() is None
    assert g.calls == 1


def test_paused_gate_raises_without_calling_fn():
    ft = FakeTime()
    g = make_gate(ft)
    g.trip(300, "test")
    called = []
    with pytest.raises(GatePaused) as ei:
        g.call(lambda: called.append(1))
    assert called == []
    assert ei.value.until == T0 + timedelta(seconds=300)
    assert ei.value.reason == "test"
    assert g.calls == 0
    assert ft.sleeps == []


def test_too_many_requests_with_retry_after_trips_for_that_long():
    ft = FakeTime()
    g = make_gate(ft)
    with pytest.raises(GatePaused) as ei:
        g.call(boom(120))
    assert isinstance(ei.value.__cause__, TooManyRequests)
    assert ei.value.until == T0 + timedelta(seconds=120)
    assert g.paused_until() == T0 + timedelta(seconds=120)
    ft.advance(119)
    with pytest.raises(GatePaused):
        g.call(ok)
    ft.advance(2)
    assert g.paused_until() is None
    assert g.call(ok) == "ok"


def test_retry_after_has_a_60s_minimum():
    g = make_gate(FakeTime())
    with pytest.raises(GatePaused) as ei:
        g.call(boom(5))
    assert ei.value.until == T0 + timedelta(seconds=60)


def test_ladder_when_retry_after_unknown():
    ft = FakeTime()
    g = make_gate(ft, burst=10)
    waits = []
    for fn in (boom(-1), boom(None), boom(-1), boom(-1)):
        with pytest.raises(GatePaused) as ei:
            g.call(fn)
        waits.append((ei.value.until - ft.now()).total_seconds())
        ft.advance(waits[-1] + 1)
    assert waits == [15 * 60, 60 * 60, 4 * 3600, 4 * 3600]


def test_success_resets_the_ladder():
    ft = FakeTime()
    g = make_gate(ft, burst=10)
    for expected in (900, 3600):
        with pytest.raises(GatePaused) as ei:
            g.call(boom(-1))
        assert (ei.value.until - ft.now()).total_seconds() == expected
        ft.advance(expected + 1)
    g.call(ok)
    with pytest.raises(GatePaused) as ei:
        g.call(boom(-1))
    assert (ei.value.until - ft.now()).total_seconds() == 900


def test_state_persisted_and_honored_by_new_gate():
    ft = FakeTime()
    store: dict[str, str] = {}
    g = make_gate(ft, save_state=store.__setitem__)
    with pytest.raises(GatePaused) as ei:
        g.call(boom(120))
    assert store == {"gate.tidal.paused_until": (T0 + timedelta(seconds=120)).isoformat(),
                     "gate.tidal.trips": "1"}

    g2 = make_gate(ft, load_state=store.get)
    assert g2.paused_until() == ei.value.until
    called = []
    with pytest.raises(GatePaused):
        g2.call(lambda: called.append(1))
    assert called == []
    ft.advance(121)
    assert g2.paused_until() is None
    assert g2.call(ok) == "ok"


def test_load_state_ignores_missing_or_garbage():
    ft = FakeTime()
    assert make_gate(ft, load_state=lambda k: None).paused_until() is None
    assert make_gate(ft, load_state=lambda k: "not a date").paused_until() is None


def test_load_state_naive_timestamp_is_utc():
    ft = FakeTime()
    naive = (T0 + timedelta(seconds=50)).replace(tzinfo=None).isoformat()
    assert make_gate(ft, load_state=lambda k: naive).paused_until() == T0 + timedelta(seconds=50)


def test_ladder_position_survives_a_new_process_and_resets_only_on_success():
    ft = FakeTime()
    store: dict[str, str] = {}
    g = make_gate(ft, burst=10, load_state=store.get, save_state=store.__setitem__)
    with pytest.raises(GatePaused):
        g.call(boom(-1))                                   # 15 min
    assert store["gate.tidal.trips"] == "1"
    ft.advance(901)

    g2 = make_gate(ft, burst=10, load_state=store.get, save_state=store.__setitem__)
    with pytest.raises(GatePaused) as ei:
        g2.call(boom(-1))                                  # second trip in a row -> 1 h, not 15 min again
    assert (ei.value.until - ft.now()).total_seconds() == 3600
    assert store["gate.tidal.trips"] == "2"
    ft.advance(3601)
    g2.call(ok)
    assert store["gate.tidal.trips"] == "0"


def test_failed_non_429_call_does_not_reset_the_ladder():
    ft = FakeTime()
    store: dict[str, str] = {}
    g = make_gate(ft, burst=10, load_state=store.get, save_state=store.__setitem__)
    with pytest.raises(GatePaused):
        g.call(boom(-1))
    ft.advance(901)
    with pytest.raises(ValueError):
        g.call(lambda: (_ for _ in ()).throw(ValueError("x")))
    assert store["gate.tidal.trips"] == "1"


class Resp:
    def __init__(self, status, headers=None):
        self.status_code = status
        self.headers = headers or {}


class HTTPishError(Exception):
    def __init__(self, status, headers=None):
        super().__init__(f"http {status}")
        self.response = Resp(status, headers)


def test_429_found_in_cause_chain_counts_as_too_many_requests():
    """tidalapi does int(Retry-After) while handling the HTTPError; a bad header raises ValueError from it."""
    ft = FakeTime()
    g = make_gate(ft)

    def fn():
        try:
            raise HTTPishError(429, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})
        except HTTPishError:
            raise ValueError("invalid literal for int()")    # __context__ is the 429
    with pytest.raises(GatePaused) as ei:
        g.call(fn)
    assert isinstance(ei.value.__cause__, ValueError)
    assert (ei.value.until - ft.now()).total_seconds() == 15 * 60      # unparsable header -> ladder


def test_429_response_retry_after_header_is_used_and_other_statuses_pass_through():
    ft = FakeTime()
    g = make_gate(ft, burst=10)

    def direct():
        raise HTTPishError(429, {"Retry-After": "300"})
    with pytest.raises(GatePaused) as ei:
        g.call(direct)
    assert (ei.value.until - ft.now()).total_seconds() == 300
    ft.advance(301)
    with pytest.raises(HTTPishError):
        g.call(lambda: (_ for _ in ()).throw(HTTPishError(500)))
    assert g.paused_until() is None


def test_pause_saved_by_another_process_is_honored_mid_run():
    ft = FakeTime()
    store: dict[str, str] = {}
    g = make_gate(ft, load_state=store.get, save_state=store.__setitem__)
    assert g.call(ok) == "ok"
    store["gate.tidal.paused_until"] = (T0 + timedelta(seconds=500)).isoformat()   # a second process tripped
    called = []
    with pytest.raises(GatePaused) as ei:
        g.call(lambda: called.append(1))
    assert called == [] and ei.value.until == T0 + timedelta(seconds=500)
    assert g.paused_until() == T0 + timedelta(seconds=500)
