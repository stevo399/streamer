import json
import logging
import random
import shutil
import subprocess
import threading
import time
import urllib.request

from google import genai
from google.genai import types

from streamer.catalog import build_catalog
from streamer.config import (
    CURATOR_CHAT_MODEL,
    GEMINI_API_KEY,
    NOTES_DIR,
    OLLAMA_MODEL,
    OLLAMA_URL,
)

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
    "Track format: show_name/season_folder/episode_stem for entertainment "
    "(episode stems are zero-padded numbers like 01, 02, 03), or "
    "podcast_name/episode_stem for podcasts.\n\n"
    "Queue between 3 and 8 tracks. The reason field should be a short "
    "human-readable explanation. Keep your response concise."
)


CURATOR_CHAT_PROMPT = (
    "You are the curator for a personal audio streaming station. The listener "
    "is chatting with you about the media library. You have knowledge of the "
    "full media catalog and recent play history.\n\n"
    "You can:\n"
    "- Answer questions about available shows, podcasts, and episodes\n"
    "- Make recommendations based on what's available\n"
    "- Take requests to play specific content\n\n"
    "When the listener asks you to play something, respond conversationally "
    "AND include a JSON block with the tracks to queue:\n"
    '{"action": "queue", "tracks": ["ShowName/season NN/episode_stem", ...], '
    '"reason": "Brief explanation"}\n\n'
    "Track format: show_name/season_folder/episode_stem for entertainment, "
    "or podcast_name/episode_stem for podcasts.\n\n"
    "Be friendly, knowledgeable, and concise. If you don't recognize something "
    "in the catalog, say so rather than guessing."
)


class Curator:
    def __init__(self, state, scanner):
        self.state = state
        self.scanner = scanner
        self._running = False
        self._thread: threading.Thread | None = None
        self._tracks_since_check = 0
        self._next_check_at = random.randint(3, 10)
        self._force_check = threading.Event()
        self._chat_history: list[dict] = []

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def trigger(self):
        self._force_check.set()

    def get_status(self) -> dict:
        return {
            "enabled": self.state.curator_enabled,
            "reason": self.state.curator_reason,
            "tracks_since_check": self._tracks_since_check,
            "next_check_at": self._next_check_at,
        }

    def get_chat_history(self) -> list[dict]:
        return list(self._chat_history)

    def chat(self, message: str) -> dict:
        if CURATOR_CHAT_MODEL.lower() == "ollama":
            return self._chat_ollama(message)
        return self._chat_gemini(message)

    def _chat_gemini(self, message: str) -> dict:
        if not GEMINI_API_KEY:
            return {"response": "Gemini API key is not configured.", "queued": []}

        self._chat_history.append({"role": "user", "content": message})

        catalog_text, path_lookup = build_catalog(
            self.scanner,
            notes_dir=str(NOTES_DIR) if NOTES_DIR else None,
        )
        history = self.state.history[-20:]
        history_text = "\n".join(history) if history else "(nothing played yet)"

        context_msg = (
            f"Media catalog:\n{catalog_text}\n\n"
            f"Recent play history:\n{history_text}"
        )

        contents = [
            types.Content(role="user", parts=[types.Part(text=context_msg)]),
            types.Content(role="model", parts=[types.Part(text="Got it, I have the catalog and history. How can I help?")]),
        ]
        for entry in self._chat_history:
            role = "model" if entry["role"] == "assistant" else "user"
            contents.append(types.Content(role=role, parts=[types.Part(text=entry["content"])]))

        try:
            client = genai.Client(api_key=GEMINI_API_KEY)
            response = client.models.generate_content(
                model=CURATOR_CHAT_MODEL,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=CURATOR_CHAT_PROMPT,
                    max_output_tokens=2048,
                ),
            )
            response_text = response.text.strip() if response.text else ""
        except Exception as e:
            logger.warning("Curator Gemini chat failed: %s", e)
            self._chat_history.pop()
            return {"response": "Sorry, something went wrong.", "queued": []}

        self._chat_history.append({"role": "assistant", "content": response_text})
        queued = self._extract_and_queue(response_text, path_lookup)
        return {"response": response_text, "queued": queued}

    def _chat_ollama(self, message: str) -> dict:
        if not OLLAMA_URL:
            return {"response": "Ollama is not configured.", "queued": []}

        if not self._ensure_ollama_running():
            return {"response": "Cannot connect to Ollama.", "queued": []}

        self._chat_history.append({"role": "user", "content": message})

        catalog_text, path_lookup = build_catalog(
            self.scanner,
            notes_dir=str(NOTES_DIR) if NOTES_DIR else None,
        )
        history = self.state.history[-20:]
        history_text = "\n".join(history) if history else "(nothing played yet)"

        context_msg = (
            f"Media catalog:\n{catalog_text}\n\n"
            f"Recent play history:\n{history_text}"
        )

        messages = [
            {"role": "system", "content": CURATOR_CHAT_PROMPT},
            {"role": "user", "content": context_msg},
            {"role": "assistant", "content": "Got it, I have the catalog and history. How can I help?"},
        ] + self._chat_history

        payload = json.dumps({
            "model": OLLAMA_MODEL,
            "messages": messages,
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
                response_text = data["message"]["content"]
        except Exception as e:
            logger.warning("Curator chat failed: %s", e)
            self._chat_history.pop()
            return {"response": "Sorry, something went wrong.", "queued": []}

        self._chat_history.append({"role": "assistant", "content": response_text})
        queued = self._extract_and_queue(response_text, path_lookup)
        return {"response": response_text, "queued": queued}

    def _extract_and_queue(self, response_text: str, path_lookup: dict) -> list[str]:
        import re

        try:
            data = json.loads(response_text.strip())
            if isinstance(data, dict) and data.get("action") == "queue":
                return self._queue_from_chat(data, path_lookup)
        except (json.JSONDecodeError, ValueError):
            pass

        for match in re.finditer(
            r'```(?:json)?\s*(\{.*?\})\s*```', response_text, re.DOTALL,
        ):
            try:
                data = json.loads(match.group(1))
                if isinstance(data, dict) and data.get("action") == "queue":
                    return self._queue_from_chat(data, path_lookup)
            except (json.JSONDecodeError, ValueError):
                continue

        for match in re.finditer(
            r'\{[^{}]*"action"\s*:\s*"queue"[^{}]*\}', response_text,
        ):
            try:
                data = json.loads(match.group())
                if isinstance(data, dict) and data.get("action") == "queue":
                    return self._queue_from_chat(data, path_lookup)
            except (json.JSONDecodeError, ValueError):
                continue

        return []

    def _queue_from_chat(self, data: dict, path_lookup: dict) -> list[str]:
        tracks = data.get("tracks", [])
        reason = data.get("reason", "Chat request")
        queued = []

        for track_id in tracks:
            resolved = self._resolve_tracks(track_id, path_lookup)
            for path in resolved:
                self.state.queue_add(path)
                queued.append(path)

        if queued and reason:
            self.state.curator_reason = reason

        return queued

    def _run(self):
        last_track = self.state.current_track
        while self._running:
            if self._force_check.wait(timeout=2):
                self._force_check.clear()
                logger.info("Curator: forced check triggered")
                self._check(force=True)
                continue

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

    def _check(self, force: bool = False):
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
        logger.info("Curator: consulting Ollama (%d tracks in catalog)", len(path_lookup))

        history = self.state.history[-20:]
        history_text = "\n".join(history) if history else "(nothing played yet)"

        response = self._ask_ollama(catalog_text, history_text, force=force)
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

    def _ask_ollama(self, catalog_text: str, history_text: str, force: bool = False) -> dict | None:
        if not OLLAMA_URL:
            return None

        if not self._ensure_ollama_running():
            return None

        user_message = (
            f"Media catalog:\n{catalog_text}\n\n"
            f"Recent history:\n{history_text}"
        )
        if force:
            user_message += (
                "\n\nThe listener just activated you. Pick something "
                "interesting from the catalog and queue it. Do NOT pass."
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

    def _resolve_tracks(self, track_id: str, path_lookup: dict[str, str]) -> list[str]:
        import re

        if track_id in path_lookup:
            return [path_lookup[track_id]]

        parts = track_id.split("/")
        if len(parts) < 2:
            return []

        prefix = track_id.lower().rstrip("/")
        folder_matches = sorted(
            path for key, path in path_lookup.items()
            if key.lower().startswith(prefix + "/")
        )
        if folder_matches:
            return folder_matches

        model_show = parts[0].lower().strip()
        model_ep = parts[-1].strip()
        model_season = "/".join(parts[1:-1]).strip() if len(parts) > 2 else None

        season_num = None
        if model_season:
            m = re.search(r"(\d+)", model_season)
            if m:
                season_num = int(m.group(1))

        ep_num = None
        m = re.search(r"(\d+)", model_ep)
        if m:
            ep_num = int(m.group(1))

        for key, path in path_lookup.items():
            key_parts = key.split("/")
            if len(key_parts) < 2:
                continue

            key_show = key_parts[0].lower()
            key_ep = key_parts[-1]
            key_season = "/".join(key_parts[1:-1]) if len(key_parts) > 2 else None

            if model_show not in key_show and key_show not in model_show:
                continue

            if season_num is not None and key_season:
                km = re.search(r"(\d+)", key_season)
                if km and int(km.group(1)) != season_num:
                    continue

            if ep_num is not None:
                key_ep_num = self._extract_episode_number(key_ep)
                if key_ep_num == ep_num:
                    return [path]

            if model_ep.lower() == key_ep.lower():
                return [path]

        return self._search_filesystem(model_ep, model_show, season_num)

    @staticmethod
    def _extract_episode_number(stem: str) -> int | None:
        import re
        m = re.search(r"[Ee](\d+)", stem)
        if m:
            return int(m.group(1))
        m = re.search(r"x(\d+)", stem)
        if m:
            return int(m.group(1))
        m = re.match(r"^(\d+)$", stem.strip())
        if m:
            return int(m.group(1))
        m = re.search(r"(\d+)", stem)
        if m:
            return int(m.group(1))

    def _search_filesystem(
        self, title: str, show_hint: str | None = None, season_num: int | None = None,
    ) -> list[str]:
        import re
        from pathlib import Path

        from streamer.scanner import AUDIO_EXTENSIONS

        words = re.findall(r"[a-zA-Z]+", title)
        if not words or len("".join(words)) < 4:
            return []

        glob_pattern = "**/*" + "*".join(words) + "*"

        for root in self.scanner.roots:
            search_root = root
            if show_hint:
                for d in root.iterdir():
                    if not d.is_dir():
                        continue
                    if show_hint in d.name.lower() or d.name.lower() in show_hint:
                        search_root = d
                        break

            try:
                proc = subprocess.run(
                    ["rg", "--files", "--iglob", glob_pattern, str(search_root)],
                    capture_output=True, text=True, timeout=5,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired):
                continue

            matches = [
                line.strip() for line in proc.stdout.splitlines()
                if line.strip() and Path(line.strip()).suffix.lower() in AUDIO_EXTENSIONS
            ]
            if not matches:
                continue

            if season_num is not None:
                filtered = [
                    m for m in matches
                    if any(
                        re.search(r"(\d+)", p) and int(re.search(r"(\d+)", p).group(1)) == season_num
                        for p in Path(m).parent.parts
                        if "season" in p.lower() or re.fullmatch(r"[Ss]\d+", p)
                    )
                ]
                if filtered:
                    matches = filtered

            return sorted(matches[:1])

        return []

    def _handle_response(self, response: dict, path_lookup: dict[str, str]):
        action = response.get("action", "pass")
        if action != "queue":
            return

        tracks = response.get("tracks", [])
        reason = response.get("reason", "")
        logger.info("Curator: wants to queue %d tracks: %s", len(tracks), tracks)

        queued = 0
        for track_id in tracks:
            resolved = self._resolve_tracks(track_id, path_lookup)
            if resolved:
                for path in resolved:
                    self.state.queue_add(path)
                    queued += 1
            else:
                logger.warning("Curator: track not found in catalog: %r", track_id)

        if queued > 0 and reason:
            self.state.curator_reason = reason
            self.state.set_curator_announcement()
        else:
            logger.warning("Curator: none of the requested tracks resolved")
