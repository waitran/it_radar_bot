"""Bounded, process-local limits; the production image deliberately runs one worker."""
from collections import OrderedDict
import threading
import time


class RateLimiter:
    def __init__(self, limit, *, capacity=4096, window=60):
        self.limit, self.capacity, self.window = limit, capacity, window
        self._buckets = OrderedDict()
        self._lock = threading.Lock()

    def allow(self, key, *, now=None):
        now = time.monotonic() if now is None else now
        with self._lock:
            start, count = self._buckets.pop(key, (now, 0))
            if now - start >= self.window:
                start, count = now, 0
            allowed = count < self.limit
            self._buckets[key] = (start, count + int(allowed))
            while len(self._buckets) > self.capacity:
                self._buckets.popitem(last=False)
            return allowed
