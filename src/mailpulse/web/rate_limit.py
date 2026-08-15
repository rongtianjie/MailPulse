from __future__ import annotations

from collections import defaultdict, deque
from threading import Lock
from time import monotonic


class LoginRateLimiter:
    def __init__(self, max_failures: int = 10, window_seconds: float = 300.0):
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self._failures: defaultdict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def allowed(self, key: str, now: float | None = None) -> bool:
        current = monotonic() if now is None else now
        with self._lock:
            self._purge(key, current)
            return len(self._failures[key]) < self.max_failures

    def record_failure(self, key: str, now: float | None = None) -> None:
        current = monotonic() if now is None else now
        with self._lock:
            self._purge(key, current)
            self._failures[key].append(current)

    def clear(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)

    def _purge(self, key: str, now: float) -> None:
        attempts = self._failures[key]
        threshold = now - self.window_seconds
        while attempts and attempts[0] <= threshold:
            attempts.popleft()


_LOGIN_RATE_LIMITER = LoginRateLimiter()
_REGISTER_RATE_LIMITER = LoginRateLimiter(max_failures=5, window_seconds=600.0)


def get_login_rate_limiter() -> LoginRateLimiter:
    return _LOGIN_RATE_LIMITER


def get_register_rate_limiter() -> LoginRateLimiter:
    return _REGISTER_RATE_LIMITER
