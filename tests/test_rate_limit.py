"""GL-03: RateLimiter — half-open window boundary, no sleep, fake clock only."""

from golf_league.domain.rate_limit import RateLimiter


class FakeClock:
    def __init__(self, start: float = 0.0):
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


def test_rate_limiter_full_window_lifecycle():
    clock = FakeClock(start=1_000.0)
    limiter = RateLimiter(limit=3, window_seconds=60, clock=clock)

    # Zero requests so far, then the first request is allowed.
    assert limiter.check("alice") is True

    # Two more requests still within the limit (limit=3 total).
    assert limiter.check("alice") is True
    assert limiter.check("alice") is True  # last allowed

    # The next request in the same window is blocked (first blocked).
    assert limiter.check("alice") is False

    # Advance to exactly window_start + window_seconds: allowed again —
    # the window is half-open [start, end).
    clock.advance(60)
    assert limiter.check("alice") is True


def test_rate_limiter_boundary_is_half_open_not_closed():
    clock = FakeClock(start=0.0)
    limiter = RateLimiter(limit=1, window_seconds=10, clock=clock)

    assert limiter.check("k") is True
    assert limiter.check("k") is False  # still inside the window

    clock.advance(9.999)
    assert limiter.check("k") is False  # just before the boundary: still blocked

    clock.advance(0.001)  # now exactly at window_start + window_seconds
    assert limiter.check("k") is True  # boundary itself is allowed


def test_rate_limiter_independent_keys_do_not_interfere():
    clock = FakeClock(start=0.0)
    limiter = RateLimiter(limit=1, window_seconds=100, clock=clock)

    assert limiter.check("alice") is True
    assert limiter.check("alice") is False

    # A different key has its own independent budget.
    assert limiter.check("bob") is True
    assert limiter.check("bob") is False

    # Alice is still blocked; bob is still blocked; no cross-talk.
    assert limiter.check("alice") is False
    assert limiter.check("bob") is False


def test_rate_limiter_zero_limit_always_blocks():
    clock = FakeClock(start=0.0)
    limiter = RateLimiter(limit=0, window_seconds=10, clock=clock)

    assert limiter.check("k") is False
