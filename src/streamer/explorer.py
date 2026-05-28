import logging
import queue
import threading
from pathlib import Path

from google import genai
from google.genai import types

from streamer.config import GEMINI_API_KEY, NOTES_DIR
from streamer.scanner import AUDIO_EXTENSIONS

logger = logging.getLogger(__name__)


class ExplorerStatus:
    def __init__(self):
        self.running: bool = False
        self.total: int = 0
        self.completed: int = 0
        self.current_show: str = ""
        self.log: list[dict] = []
        self.error: str | None = None
        self._lock = threading.Lock()
        self._subscribers: list[queue.Queue] = []

    def push_event(self, event: dict):
        with self._lock:
            self.log.append(event)
            if len(self.log) > 100:
                self.log = self.log[-100:]
            for q in self._subscribers:
                try:
                    q.put_nowait(event)
                except queue.Full:
                    pass

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=256)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue):
        with self._lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass

    def to_dict(self) -> dict:
        return {
            "running": self.running,
            "total": self.total,
            "completed": self.completed,
            "current_show": self.current_show,
            "log": list(self.log),
            "error": self.error,
        }
