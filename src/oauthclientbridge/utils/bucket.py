import threading


class Bucket:
    """Thread-safe bounded token bucket with explicit add and consume operations."""

    def __init__(self, capacity: int, refill_amount: float):
        self.capacity = capacity
        self.refill_amount = refill_amount
        self._tokens = capacity
        self._lock = threading.Lock()

    def add(self, tokens: float) -> None:
        with self._lock:
            self._tokens = min(self.capacity, self._tokens + tokens)

    def consume(self, tokens: float = 1) -> bool:
        with self._lock:
            if self._tokens < tokens:
                return False

            self._tokens -= tokens
            return True
