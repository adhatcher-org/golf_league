"""Pure rate limiter: no I/O, no wall clock — the caller injects one."""

from collections.abc import Callable


class RateLimiter:
    """A fixed-window rate limiter keyed by an arbitrary string.

    The window is half-open: `[window_start, window_start + window_seconds)`.
    A request arriving exactly at `window_start + window_seconds` starts a
    new window and is allowed.

    `clock` is a zero-argument callable returning the current time in
    seconds (an `int` or `float`). Tests inject a fake clock; this class
    never calls `time.time()` itself.
    """

    def __init__(self, limit: int, window_seconds: int, clock: Callable[[], float]):
        self._limit = limit
        self._window_seconds = window_seconds
        self._clock = clock
        # key -> (window_start, count)
        self._windows: dict[str, tuple[float, int]] = {}

    def check(self, key: str) -> bool:
        """Record an attempt for `key` and return True if it is allowed."""
        allowed, state = check_window(
            self._windows.get(key), now=self._clock(),
            limit=self._limit, window_seconds=self._window_seconds,
        )
        self._windows[key] = state
        return allowed


def check_window(
    state: tuple[float, int] | None, *, now: float, limit: int, window_seconds: int,
) -> tuple[bool, tuple[float, int]]:
    """Check one immutable half-open window, returning its prospective state."""
    start, count = state if state is not None else (now, 0)
    if now - start >= window_seconds:
        start, count = now, 0
    if count >= limit:
        return False, (start, count)
    return True, (start, count + 1)
