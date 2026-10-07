from collections import OrderedDict
import time


class TTLCache:
    def __init__(self, maxsize=128, ttl=60.0, clock=time.monotonic):
        self._data = OrderedDict()
        self.maxsize, self.ttl, self._clock = maxsize, ttl, clock

    def get(self, key, default=None):
        item = self._data.get(key)
        if item is None:
            return default
        value, expires = item
        if expires < self._clock():
            del self._data[key]
            return default
        self._data.move_to_end(key)
        return value

    def set(self, key, value):
        self._data[key] = (value, self._clock() + self.ttl)
        self._data.move_to_end(key)
        while len(self._data) > self.maxsize:
            self._data.popitem(last=False)
