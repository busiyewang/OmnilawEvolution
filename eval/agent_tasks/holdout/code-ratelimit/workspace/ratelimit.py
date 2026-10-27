import time


class RateLimiter:
    def __init__(self, rate, capacity, clock=time.monotonic):
        raise NotImplementedError

    def allow(self):
        raise NotImplementedError
