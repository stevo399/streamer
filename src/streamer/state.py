import threading
from collections import deque


class ServerState:
    def __init__(self):
        self._lock = threading.Lock()
        self._changed = threading.Condition(self._lock)
        self._version: int = 0
        self._current_track: str | None = None
        self._queue: list[str] = []
        self._history: deque[str] = deque(maxlen=100)
        self._dj_enabled: bool = False
        self._paused: bool = False
        self._book_mode: bool = False
        self._loop_current: bool = False
        self._curator_enabled: bool = False
        self._curator_reason: str | None = None
        self._curator_should_announce: bool = False

    def _touch(self) -> None:
        """Record a change and wake any waiters. Caller must hold the lock."""
        self._version += 1
        self._changed.notify_all()

    @property
    def version(self) -> int:
        with self._lock:
            return self._version

    def wait_for_change(self, last_version: int, timeout: float | None = None) -> int:
        """Block until the version differs from last_version or timeout elapses."""
        with self._changed:
            self._changed.wait_for(lambda: self._version != last_version, timeout)
            return self._version

    def notify_change(self) -> None:
        """Signal a change to data that lives outside ServerState."""
        with self._lock:
            self._touch()

    @property
    def queue(self) -> list[str]:
        with self._lock:
            return list(self._queue)

    def queue_add(self, path: str) -> None:
        with self._lock:
            self._queue.append(path)
            self._touch()

    def queue_remove(self, index: int) -> bool:
        with self._lock:
            if 0 <= index < len(self._queue):
                self._queue.pop(index)
                self._touch()
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
            self._touch()

    @property
    def history(self) -> list[str]:
        with self._lock:
            return list(self._history)

    def history_push(self, path: str) -> None:
        with self._lock:
            self._history.append(path)
            self._touch()

    @property
    def dj_enabled(self) -> bool:
        with self._lock:
            return self._dj_enabled

    @dj_enabled.setter
    def dj_enabled(self, value: bool) -> None:
        with self._lock:
            self._dj_enabled = value
            self._touch()

    @property
    def paused(self) -> bool:
        with self._lock:
            return self._paused

    @paused.setter
    def paused(self, value: bool) -> None:
        with self._lock:
            self._paused = value
            self._touch()

    @property
    def loop_current(self) -> bool:
        with self._lock:
            return self._loop_current

    @loop_current.setter
    def loop_current(self, value: bool) -> None:
        with self._lock:
            self._loop_current = value
            self._touch()

    @property
    def book_mode(self) -> bool:
        with self._lock:
            return self._book_mode

    @book_mode.setter
    def book_mode(self, value: bool) -> None:
        with self._lock:
            self._book_mode = value
            self._touch()

    @property
    def curator_enabled(self) -> bool:
        with self._lock:
            return self._curator_enabled

    @curator_enabled.setter
    def curator_enabled(self, value: bool) -> None:
        with self._lock:
            self._curator_enabled = value
            self._touch()

    @property
    def curator_reason(self) -> str | None:
        with self._lock:
            return self._curator_reason

    @curator_reason.setter
    def curator_reason(self, value: str | None) -> None:
        with self._lock:
            self._curator_reason = value
            self._touch()

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
            self._touch()
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
            self._touch()
            return self._current_track

    def play_now(self, path: str) -> None:
        with self._lock:
            if self._current_track:
                self._history.append(self._current_track)
            self._current_track = path
            self._touch()
