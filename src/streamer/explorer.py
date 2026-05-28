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


EXPLORER_PROMPT = (
    "You are a media library assistant. Given information about a show or podcast, "
    "write a concise description covering: what it is about, its tone and themes, "
    "and any particularly notable or fan-favorite episodes. If you don't recognize "
    "the show, say so briefly rather than fabricating details."
)


def explore(scanner, status: ExplorerStatus, force: bool = False):
    status.running = True
    status.error = None
    status.completed = 0
    status.current_show = ""

    if NOTES_DIR is None:
        status.error = "NOTES_DIR is not configured."
        status.running = False
        status.push_event({"type": "error", "message": status.error})
        return

    if not GEMINI_API_KEY:
        status.error = "Gemini API key is not configured."
        status.running = False
        status.push_event({"type": "error", "message": status.error})
        return

    try:
        all_shows = _collect_shows(scanner)

        if not force:
            all_shows = [
                s for s in all_shows
                if not (NOTES_DIR / s["name"] / "show.md").is_file()
            ]

        status.total = len(all_shows)

        if not all_shows:
            status.running = False
            status.push_event({"type": "finished", "completed": 0, "total": 0})
            return

        client = genai.Client(api_key=GEMINI_API_KEY)

        for show in all_shows:
            name = show["name"]
            status.current_show = name
            status.push_event({
                "type": "exploring",
                "show": name,
                "completed": status.completed,
                "total": status.total,
            })

            try:
                if show["seasons"]:
                    season_info = []
                    for sname, eps in sorted(show["seasons"].items()):
                        season_info.append(f"  {sname}: {', '.join(eps)}")
                    structure = "\n".join(season_info)
                else:
                    structure = ", ".join(show["episodes"][:50])

                prompt = (
                    f"Here is a {'podcast' if show['type'] == 'podcast' else 'show'} "
                    f"called \"{name}\" with {len(show['episodes'])} episodes.\n\n"
                    f"Structure:\n{structure}\n\n"
                    f"Write a concise description."
                )

                response = client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=EXPLORER_PROMPT,
                        max_output_tokens=1024,
                    ),
                )

                text = response.text.strip() if response.text else ""
                if text:
                    show_dir = NOTES_DIR / name
                    show_dir.mkdir(parents=True, exist_ok=True)
                    (show_dir / "show.md").write_text(text, encoding="utf-8")

                status.completed += 1
                status.push_event({
                    "type": "completed",
                    "show": name,
                    "completed": status.completed,
                    "total": status.total,
                })

            except Exception as e:
                logger.warning("Explorer: failed to process %s: %s", name, e)
                status.push_event({
                    "type": "error",
                    "message": f"Failed to process {name}: {e}",
                })

        status.push_event({
            "type": "finished",
            "completed": status.completed,
            "total": status.total,
        })

    except Exception as e:
        logger.error("Explorer: fatal error: %s", e)
        status.error = str(e)
        status.push_event({"type": "error", "message": str(e)})
    finally:
        status.running = False
        status.current_show = ""
