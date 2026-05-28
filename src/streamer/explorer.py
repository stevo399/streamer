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
        for dirpath in sorted(_walk_dirs(root)):
            rel = dirpath.relative_to(root)
            rel_str = str(rel).replace("\\", "/")

            audio_files = sorted(
                f.stem for f in dirpath.iterdir()
                if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS
            )
            subdirs = sorted(
                d.name for d in dirpath.iterdir() if d.is_dir()
            )

            if not audio_files and not subdirs:
                continue
            if not audio_files and not _has_audio_below(dirpath):
                continue

            shows.append({
                "name": rel_str,
                "type": "podcast" if is_podcast else "entertainment",
                "audio_files": audio_files,
                "subdirs": subdirs,
            })
    return shows


def _walk_dirs(root: Path) -> list[Path]:
    dirs = []
    for item in sorted(root.iterdir()):
        if item.is_dir():
            dirs.append(item)
            dirs.extend(_walk_dirs(item))
    return dirs


def _has_audio_below(dirpath: Path) -> bool:
    for f in dirpath.rglob("*"):
        if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS:
            return True
    return False


EXPLORER_PROMPT = (
    "You are a media library assistant. Given a directory from a user's media "
    "library, write a concise description covering: what the content is about, "
    "its tone and themes, and any particularly notable or fan-favorite entries. "
    "The full path is provided for context. If you don't recognize the content, "
    "say so briefly rather than fabricating details."
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
                parts = []
                if show["subdirs"]:
                    parts.append(f"Subdirectories: {', '.join(show['subdirs'])}")
                if show["audio_files"]:
                    file_list = show["audio_files"][:50]
                    parts.append(f"Audio files: {', '.join(file_list)}")
                    if len(show["audio_files"]) > 50:
                        parts.append(f"  ... and {len(show['audio_files']) - 50} more")
                structure = "\n".join(parts) if parts else "(empty directory)"

                prompt = (
                    f"Here is a directory from a media library.\n"
                    f"Full path: {name}\n"
                    f"Type: {'podcast' if show['type'] == 'podcast' else 'entertainment'}\n\n"
                    f"Contents:\n{structure}\n\n"
                    f"Write a concise description of this content."
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
