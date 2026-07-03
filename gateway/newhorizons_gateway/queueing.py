from __future__ import annotations

import queue
from typing import Generic, TypeVar


T = TypeVar("T")


class DropOldestQueue(Generic[T]):
    def __init__(self, maxsize: int) -> None:
        self._queue: queue.Queue[T] = queue.Queue(maxsize=max(1, int(maxsize)))
        self.dropped = 0

    def put(self, item: T) -> None:
        while True:
            try:
                self._queue.put_nowait(item)
                return
            except queue.Full:
                try:
                    self._queue.get_nowait()
                    self.dropped += 1
                except queue.Empty:
                    return

    def get(self, timeout: float) -> T:
        return self._queue.get(timeout=timeout)

    def qsize(self) -> int:
        return self._queue.qsize()
