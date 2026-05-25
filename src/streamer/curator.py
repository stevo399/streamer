import json
import logging
import random
import shutil
import subprocess
import threading
import time
import urllib.request

from streamer.catalog import build_catalog
from streamer.config import NOTES_DIR, OLLAMA_MODEL, OLLAMA_URL

logger = logging.getLogger(__name__)

CURATOR_SYSTEM_PROMPT = (
    "You are a curator for a personal audio streaming station. You review "
    "the media catalog and recent play history, then decide whether to "
    "intervene with a curated playlist or let normal shuffle continue.\n\n"
    "Most of the time, you should pass and let shuffle handle things. "
    "But occasionally, you might:\n"
    "- Run a marathon: queue sequential episodes from a season\n"
    "- Favor a genre: queue several tracks from related shows\n"
    "- Create a themed mix: queue tracks from different shows that share "
    "a theme or mood\n\n"
    "Consider the recent history to avoid repetition and notice gaps. "
    "If a show hasn't been played in a while, that might be a good "
    "candidate.\n\n"
    "You MUST respond with JSON in one of two formats:\n"
    'To pass: {"action": "pass"}\n'
    'To queue tracks: {"action": "queue", "tracks": '
    '["ShowName/season NN/episode_stem", ...], '
    '"reason": "Brief explanation"}\n\n'
    "Track identifiers must match the catalog exactly. Use the format "
    "show_name/season_folder/episode_stem for entertainment, or "
    "podcast_name/episode_stem for podcasts.\n\n"
    "Queue between 3 and 12 tracks. The reason field should be a short "
    "human-readable explanation."
)


class Curator:
    def __init__(self, state, scanner):
        self.state = state
        self.scanner = scanner
        self._running = False
        self._thread: threading.Thread | None = None
        self._tracks_since_check = 0
        self._next_check_at = random.randint(3, 10)

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def _run(self):
        last_track = self.state.current_track
        while self._running:
            time.sleep(2)

            if self.state.curator_reason and not self.state.queue:
                self.state.curator_reason = None

            current_track = self.state.current_track
            if current_track and current_track != last_track:
                self._tracks_since_check += 1
                last_track = current_track

            if self._tracks_since_check >= self._next_check_at:
                self._tracks_since_check = 0
                self._next_check_at = random.randint(3, 10)
                logger.info("Curator: check triggered (next in %d tracks)", self._next_check_at)
                self._check()

    def _check(self):
        if not self.state.curator_enabled:
            logger.debug("Curator: skipping check (disabled)")
            return
        if self.state.queue:
            logger.debug("Curator: skipping check (queue not empty)")
            return

        catalog_text, path_lookup = build_catalog(
            self.scanner,
            notes_dir=str(NOTES_DIR) if NOTES_DIR else None,
        )
        logger.info("Curator: consulting Ollama (%d shows in catalog)", len(path_lookup))

        history = self.state.history[-20:]
        history_text = "\n".join(history) if history else "(nothing played yet)"

        response = self._ask_ollama(catalog_text, history_text)
        if not isinstance(response, dict):
            logger.warning("Curator: no valid response from Ollama")
            return

        logger.info("Curator: Ollama response: %s", response.get("action", "unknown"))
        self._handle_response(response, path_lookup)

    def _ensure_ollama_running(self) -> bool:
        try:
            req = urllib.request.Request(f"{OLLAMA_URL}/api/tags")
            with urllib.request.urlopen(req, timeout=3):
                return True
        except Exception:
            pass

        ollama_bin = shutil.which("ollama")
        if not ollama_bin:
            return False

        try:
            subprocess.Popen(
                [ollama_bin, "serve"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            for _ in range(10):
                time.sleep(1)
                try:
                    req = urllib.request.Request(f"{OLLAMA_URL}/api/tags")
                    with urllib.request.urlopen(req, timeout=3):
                        logger.info("Curator: started Ollama automatically")
                        return True
                except Exception:
                    continue
        except Exception as e:
            logger.warning("Curator: failed to start Ollama: %s", e)

        return False

    def _ask_ollama(self, catalog_text: str, history_text: str) -> dict | None:
        if not OLLAMA_URL:
            return None

        if not self._ensure_ollama_running():
            return None

        user_message = (
            f"Media catalog:\n{catalog_text}\n\n"
            f"Recent history:\n{history_text}"
        )

        payload = json.dumps({
            "model": OLLAMA_MODEL,
            "messages": [
                {"role": "system", "content": CURATOR_SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            "format": "json",
            "stream": False,
        }).encode()

        try:
            req = urllib.request.Request(
                f"{OLLAMA_URL}/api/chat",
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read())
                return json.loads(data["message"]["content"])
        except Exception as e:
            logger.warning("Curator: Ollama request failed: %s", e)
            return None

    def _handle_response(self, response: dict, path_lookup: dict[str, str]):
        action = response.get("action", "pass")
        if action != "queue":
            return

        tracks = response.get("tracks", [])
        reason = response.get("reason", "")

        queued = 0
        for track_id in tracks:
            path = path_lookup.get(track_id)
            if path:
                self.state.queue_add(path)
                queued += 1

        if queued > 0 and reason:
            self.state.curator_reason = reason
