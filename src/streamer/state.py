import threading
from collections import deque


class ServerState:
    def __init__(self):
        self._lock = threading.Lock()
        self._current_track: str | None = None
        self._queue: list[str] = []
        self._history: deque[str] = deque(maxlen=100)
        self._dj_enabled: bool = False
        self._curator_enabled: bool = False
        self._curator_reason: str | None = None
        self._curator_should_announce: bool = False

    @property
    def queue(self) -> list[str]:
        with self._lock:
            return list(self._queue)

    def queue_add(self, path: str) -> None:
        with self._lock:
            self._queue.append(path)

    def queue_remove(self, index: int) -> bool:
        with self._lock:
            if 0 <= index < len(self._queue):
                self._queue.pop(index)
                return True
            return False

    @property
    def current_track(self) -> str | None:
        with self._lock:
            return self._current_track

    @current_track.setter
    def current_track(self, path: str) -> None:
        with self._lock:
            self._current_track = path

    @property
    def history(self) -> list[str]:
        with self._lock:
            return list(self._history)

    def history_push(self, path: str) -> None:
        with self._lock:
            self._history.append(path)

    @property
    def dj_enabled(self) -> bool:
        with self._lock:
            return self._dj_enabled

    @dj_enabled.setter
    def dj_enabled(self, value: bool) -> None:
        with self._lock:
            self._dj_enabled = value

    @property
    def curator_enabled(self) -> bool:
        with self._lock:
            return self._curator_enabled

    @curator_enabled.setter
    def curator_enabled(self, value: bool) -> None:
        with self._lock:
            self._curator_enabled = value

    @property
    def curator_reason(self) -> str | None:
        with self._lock:
            return self._curator_reason

    @curator_reason.setter
    def curator_reason(self, value: str | None) -> None:
        with self._lock:
            self._curator_reason = value

    def consume_curator_announcement(self) -> str | None:
        with self._lock:
            if self._curator_should_announce and self._curator_reason:
                self._curator_should_announce = False
                return self._curator_reason
            return None

    def set_curator_announcement(self) -> None:
        with self._lock:
            self._curator_should_announce = True

    def advance(self) -> str | None:
        with self._lock:
            if self._current_track:
                self._history.append(self._current_track)
            if self._queue:
                self._current_track = self._queue.pop(0)
                return self._current_track
            self._current_track = None
            return None

    def go_previous(self) -> str | None:
        with self._lock:
            if not self._history:
                return None
            if self._current_track:
                self._queue.insert(0, self._current_track)
            self._current_track = self._history.pop()
            return self._current_track

    def play_now(self, path: str) -> None:
        with self._lock:
            if self._current_track:
                self._history.append(self._current_track)
            self._current_track = path
