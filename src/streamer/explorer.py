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


def _collect_shows(scanner) -> list[dict]:
    shows = []
    for root in scanner.roots:
        if not root.exists():
            continue
        is_podcast = "podcast" in root.name.lower()
        for show_dir in sorted(root.iterdir()):
            if not show_dir.is_dir():
                continue
            if is_podcast:
                episodes = sorted(
                    f.stem for f in show_dir.rglob("*")
                    if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS
                )
                if episodes:
                    shows.append({
                        "name": show_dir.name,
                        "type": "podcast",
                        "seasons": {},
                        "episodes": episodes,
                    })
            else:
                seasons: dict[str, list[str]] = {}
                for sub in sorted(show_dir.iterdir()):
                    if sub.is_dir():
                        eps = sorted(
                            f.stem for f in sub.iterdir()
                            if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS
                        )
                        if eps:
                            seasons[sub.name] = eps
                if seasons:
                    all_eps = [ep for eps in seasons.values() for ep in eps]
                    shows.append({
                        "name": show_dir.name,
                        "type": "entertainment",
                        "seasons": seasons,
                        "episodes": all_eps,
                    })
    return shows
