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
        now = self._clock()
        window_start, count = self._windows.get(key, (now, 0))

        if now - window_start >= self._window_seconds:
            # Outside the current window: start a fresh one.
            window_start = now
            count = 0

        if count >= self._limit:
            # Still record the window state; do not reset the count.
            self._windows[key] = (window_start, count)
            return False

        self._windows[key] = (window_start, count + 1)
        return True
