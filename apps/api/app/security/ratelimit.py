"""Sliding-window rate limits and open-stream counts, in process memory.

The limiter slows down guessing and email flooding on the sign-in endpoints, and caps each
user's API calls and stream tokens; `streams` caps each user's open live streams. Both are per
API process: with several replicas each keeps its own count, so the durable guard against
password guessing is the per-account lockout in the database (users.failed_sign_ins). nginx
limits each client address in front of this (infra/platform/host).
"""

import time
from collections import deque
from dataclasses import dataclass, field
from threading import Lock

# How often idle keys are dropped, and how long a key is kept after its last hit (the longest
# window in use).
_SWEEP_SECONDS = 600.0
_KEEP_SECONDS = 3600.0


@dataclass
class RateLimiter:
    _hits: dict[str, deque[float]] = field(default_factory=dict)
    _lock: Lock = field(default_factory=Lock)
    _swept: float = field(default_factory=time.monotonic)

    def allow(self, key: str, limit: int, window_seconds: float) -> bool:
        """Count one attempt for `key`; False when it already made `limit` in the window."""
        now = time.monotonic()
        with self._lock:
            self._sweep(now)
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= now - window_seconds:
                hits.popleft()
            if len(hits) >= limit:
                return False
            hits.append(now)
            return True

    def _sweep(self, now: float) -> None:
        if now - self._swept < _SWEEP_SECONDS:
            return
        self._swept = now
        for key in [k for k, v in self._hits.items() if not v or v[-1] < now - _KEEP_SECONDS]:
            del self._hits[key]

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


@dataclass
class OpenStreams:
    """How many live streams each key holds open."""

    _open: dict[str, int] = field(default_factory=dict)
    _lock: Lock = field(default_factory=Lock)

    def acquire(self, key: str, limit: int) -> bool:
        with self._lock:
            if self._open.get(key, 0) >= limit:
                return False
            self._open[key] = self._open.get(key, 0) + 1
            return True

    def release(self, key: str) -> None:
        with self._lock:
            left = self._open.get(key, 0) - 1
            if left > 0:
                self._open[key] = left
            else:
                self._open.pop(key, None)

    def reset(self) -> None:
        with self._lock:
            self._open.clear()


limiter = RateLimiter()
streams = OpenStreams()
