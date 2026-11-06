# rate_limiters.py
import threading
import time
from collections import deque


class SlidingWindowRateLimiter:
    """Allow at most max_requests in any window_seconds. Returns the wait in ms.

    Sending in parallel means the ceiling has to be held somewhere other than
    the loop that starts the work, because every thread is starting work at
    once. acquire() blocks the calling thread until there is room, so the
    pool can be sized for latency and the limiter decides the actual rate.
    """

    def __init__(self, max_requests: int, window_seconds: float):
        self.max_requests = max_requests
        self.window = window_seconds
        self.events = deque()
        self._lock = threading.Lock()

    def _drop_expired(self, now: float) -> None:
        while self.events and now - self.events[0] > self.window:
            self.events.popleft()

    def acquire(self) -> float:
        start = time.time()
        with self._lock:
            now = time.time()
            self._drop_expired(now)

            if len(self.events) >= self.max_requests:
                # Wait for the oldest event to age out of the window.
                sleep_for = self.window - (now - self.events[0])
                if sleep_for > 0:
                    time.sleep(sleep_for)
                self._drop_expired(time.time())

            self.events.append(time.time())
        return (time.time() - start) * 1000.0
