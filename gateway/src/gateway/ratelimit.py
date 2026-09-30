"""In-memory token-bucket rate limiter (single process, single user)."""

from __future__ import annotations

import math
import threading
import time
from collections import OrderedDict


class RateLimiter:
    def __init__(self, per_minute: float, burst: int, max_keys: int = 10_000) -> None:
        self._rate = per_minute / 60.0
        self._burst = float(burst)
        self._max_keys = max_keys
        self._buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()
        self._lock = threading.Lock()

    def hit(self, key: str, now: float | None = None) -> int | None:
        """Consume one token. Returns None if allowed, else seconds to wait."""
        now = time.monotonic() if now is None else now
        with self._lock:
            tokens, updated = self._buckets.pop(key, (self._burst, now))
            tokens = min(self._burst, tokens + (now - updated) * self._rate)
            if tokens >= 1.0:
                self._buckets[key] = (tokens - 1.0, now)
                allowed = True
            else:
                self._buckets[key] = (tokens, now)
                allowed = False
            while len(self._buckets) > self._max_keys:
                self._buckets.popitem(last=False)
        if allowed:
            return None
        return max(1, math.ceil((1.0 - tokens) / self._rate))
