# FastAPI Migration & API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Flask with FastAPI, add a comprehensive JSON API for programmatic control, and update the control panel with track timing, curator status, and curator chat.

**Architecture:** Single-file `server.py` rewrite from Flask to FastAPI. All routes (HTML pages, JSON API, streaming) in one file. Auth via FastAPI's `HTTPBasic` dependency with bcrypt. Jinja2Templates for HTML. uvicorn replaces Flask's dev server.

**Tech Stack:** FastAPI, uvicorn, python-multipart, Jinja2, httpx (test), Pydantic, bcrypt

---

## File Structure

### Files to modify:
- `pyproject.toml` — swap Flask deps for FastAPI deps
- `src/streamer/server.py` — full rewrite (Flask → FastAPI + new API endpoints)
- `src/streamer/__main__.py` — switch to uvicorn
- `src/streamer/pipeline.py` — add ffprobe duration, `_track_bytes_written`, `get_playback_info()`
- `src/streamer/curator.py` — add `get_status()`, `get_chat_history()`, `chat()`, `CURATOR_CHAT_PROMPT`
- `src/streamer/templates/index.html` — add timing display, curator status/force, chat UI
- `tests/test_server.py` — full rewrite for FastAPI TestClient + new API tests

---

### Task 1: Update Dependencies

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Update pyproject.toml**

Replace the Flask dependencies with FastAPI equivalents. Add httpx as a dev dependency for testing.

```toml
[project]
name = "streamer"
version = "0.1.0"
description = "Local network audio streaming server with web control panel and AI DJ"
readme = "README.md"
authors = [
    { name = "stevo399", email = "57786372+stevo399@users.noreply.github.com" }
]
requires-python = ">=3.13"
dependencies = [
    "bcrypt>=5.0.0",
    "fastapi>=0.115.0",
    "google-cloud-texttospeech>=2.36.0",
    "google-genai>=1.0.0",
    "jinja2>=3.1.0",
    "python-dotenv>=1.2.2",
    "python-multipart>=0.0.20",
    "uvicorn>=0.34.0",
]

[project.scripts]
streamer = "streamer:main"
streamer-hashpw = "streamer:hashpw"

[build-system]
requires = ["uv_build>=0.8.8,<0.9.0"]
build-backend = "uv_build"

[dependency-groups]
dev = [
    "httpx>=0.28.0",
    "pytest>=9.0.3",
]
```

- [ ] **Step 2: Install dependencies**

Run: `uv sync`
Expected: All dependencies install successfully.

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "build: replace Flask with FastAPI dependencies"
```

---

### Task 2: Rewrite server.py to FastAPI

**Files:**
- Modify: `src/streamer/server.py` (full rewrite)

This rewrites every existing Flask route as a FastAPI equivalent. No new API endpoints yet — just migrate existing functionality.

- [ ] **Step 1: Write the new server.py**

Replace the entire contents of `src/streamer/server.py` with:

```python
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import quote

import bcrypt
from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from streamer.config import AUTH_PASSWORD_HASH, AUTH_USERNAME
from streamer.scanner import Scanner
from streamer.state import ServerState

_templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
_security = HTTPBasic(auto_error=False)


class PathBody(BaseModel):
    path: str


class ToggleBody(BaseModel):
    enabled: bool


class ChatBody(BaseModel):
    message: str


def verify_credentials(
    credentials: HTTPBasicCredentials | None = Depends(_security),
) -> str:
    if not AUTH_USERNAME or not AUTH_PASSWORD_HASH:
        return "anonymous"
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            headers={"WWW-Authenticate": "Basic"},
        )
    if credentials.username == AUTH_USERNAME and bcrypt.checkpw(
        credentials.password.encode("utf-8"),
        AUTH_PASSWORD_HASH.encode("utf-8"),
    ):
        return credentials.username
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        headers={"WWW-Authenticate": "Basic"},
    )


def create_app(state=None, scanner=None, pipeline=None):
    _state = state or ServerState()
    _scanner = scanner or Scanner()
    _pipeline = pipeline

    app = FastAPI()
    app.state.server_state = _state
    app.state.scanner = _scanner
    app.state.pipeline = _pipeline

    # ── HTML routes ──────────────────────────────────────────────────────

    @app.get("/")
    def index(request: Request, _user: str = Depends(verify_credentials)):
        current = _state.current_track
        track_name = Path(current).name if current else "Nothing playing"
        track_path = current or ""
        queue_items = [
            {"name": Path(p).name, "path": p} for p in _state.queue
        ]
        return _templates.TemplateResponse("index.html", {
            "request": request,
            "track_name": track_name,
            "track_path": track_path,
            "queue": queue_items,
            "dj_enabled": _state.dj_enabled,
            "curator_enabled": _state.curator_enabled,
            "curator_reason": _state.curator_reason,
        })

    @app.post("/next")
    def next_track(_user: str = Depends(verify_credentials)):
        if _pipeline:
            _pipeline.request_next()
        return RedirectResponse(url="/", status_code=303)

    @app.post("/previous")
    def previous_track(_user: str = Depends(verify_credentials)):
        if _pipeline:
            _pipeline.request_previous()
        return RedirectResponse(url="/", status_code=303)

    @app.post("/queue/add")
    def queue_add(
        file: str = Form(""),
        _user: str = Depends(verify_credentials),
    ):
        resolved = _scanner.resolve_browse_path(file)
        if resolved and resolved.is_file():
            _state.queue_add(str(resolved))
        return RedirectResponse(url="/", status_code=303)

    @app.post("/queue/remove")
    def queue_remove(
        index: int = Form(...),
        _user: str = Depends(verify_credentials),
    ):
        _state.queue_remove(index)
        return RedirectResponse(url="/", status_code=303)

    @app.post("/dj/toggle")
    def dj_toggle(_user: str = Depends(verify_credentials)):
        _state.dj_enabled = not _state.dj_enabled
        return RedirectResponse(url="/", status_code=303)

    @app.post("/curator/toggle")
    def curator_toggle(_user: str = Depends(verify_credentials)):
        _state.curator_enabled = not _state.curator_enabled
        if _state.curator_enabled and _pipeline:
            _pipeline._curator.trigger()
        return RedirectResponse(url="/", status_code=303)

    @app.post("/play")
    def play_now(
        file: str = Form(""),
        _user: str = Depends(verify_credentials),
    ):
        resolved = _scanner.resolve_browse_path(file)
        if resolved and resolved.is_file():
            if _pipeline:
                _pipeline.request_play(str(resolved))
        return RedirectResponse(url="/", status_code=303)

    @app.get("/browse/play")
    def browse_play(
        request: Request,
        file: str = "",
        _user: str = Depends(verify_credentials),
    ):
        resolved = _scanner.resolve_browse_path(file)
        if resolved is None or not resolved.is_file():
            raise HTTPException(status_code=404)
        return _templates.TemplateResponse("play.html", {
            "request": request,
            "file_name": resolved.name,
            "file_path": str(resolved),
            "browse_path": file,
        })

    @app.get("/browse/")
    def browse_root(
        request: Request,
        _user: str = Depends(verify_credentials),
    ):
        dirs = [
            {"name": root.name, "href": f"/browse/{quote(root.name)}"}
            for root in _scanner.roots
            if root.exists()
        ]
        return _templates.TemplateResponse("browse.html", {
            "request": request,
            "dirs": dirs,
            "files": [],
            "breadcrumbs": [],
        })

    @app.get("/browse/{subpath:path}")
    def browse_subpath(
        request: Request,
        subpath: str,
        _user: str = Depends(verify_credentials),
    ):
        resolved = _scanner.resolve_browse_path(subpath)
        if resolved is None or not resolved.is_dir():
            raise HTTPException(status_code=404)

        dir_names, file_names = _scanner.list_directory(resolved)
        dirs = [
            {"name": d, "href": f"/browse/{quote(subpath + '/' + d)}"}
            for d in dir_names
        ]
        files = [
            {
                "name": f,
                "href": f"/browse/play?file={quote(subpath + '/' + f)}",
            }
            for f in file_names
        ]

        parts = subpath.split("/")
        breadcrumbs = []
        for i, part in enumerate(parts):
            bc_path = "/".join(parts[: i + 1])
            breadcrumbs.append(
                {"name": part, "href": f"/browse/{quote(bc_path)}"}
            )

        return _templates.TemplateResponse("browse.html", {
            "request": request,
            "dirs": dirs,
            "files": files,
            "breadcrumbs": breadcrumbs,
        })

    # ── Legacy API ───────────────────────────────────────────────────────

    @app.get("/api/state")
    def api_state(_user: str = Depends(verify_credentials)):
        current = _state.current_track
        return {
            "track_name": Path(current).name if current else "Nothing playing",
            "track_path": current or "",
            "queue": [
                {"name": Path(p).name, "path": p}
                for p in _state.queue
            ],
            "dj_enabled": _state.dj_enabled,
            "curator_enabled": _state.curator_enabled,
            "curator_reason": _state.curator_reason,
        }

    # ── Streaming ────────────────────────────────────────────────────────

    @app.get("/stream.ogg")
    def stream_ogg():
        def generate():
            if not _pipeline:
                return
            headers = _pipeline.ogg_buffer.get_headers()
            if headers:
                yield headers
            pos = _pipeline.ogg_buffer.get_current_position()
            while True:
                data, new_pos = _pipeline.ogg_buffer.read(pos, max_bytes=4096)
                if data is None:
                    return
                if not data:
                    time.sleep(0.02)
                    continue
                pos = new_pos
                yield data

        return StreamingResponse(
            generate(),
            media_type="audio/ogg",
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/stream.mp3")
    def stream_mp3():
        return _stream_response(
            _pipeline,
            ["-f", "mp3", "-b:a", "128k"],
            "audio/mpeg",
        )

    return app


def _stream_response(pipeline, codec_args, mimetype):
    def generate():
        if not pipeline:
            return

        cmd = [
            "ffmpeg", "-v", "error",
            "-f", "s16le", "-ar", "44100", "-ac", "2", "-i", "pipe:0",
        ] + codec_args + ["pipe:1"]

        encoder = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

        stop = threading.Event()

        def feed_encoder():
            pos = pipeline.pcm_buffer.get_current_position()
            while not stop.is_set():
                data, new_pos = pipeline.pcm_buffer.read(pos, max_bytes=4096)
                if data is None:
                    pos = new_pos
                    continue
                if not data:
                    time.sleep(0.02)
                    continue
                pos = new_pos
                try:
                    encoder.stdin.write(data)
                    encoder.stdin.flush()
                except (BrokenPipeError, OSError):
                    return

        feeder = threading.Thread(target=feed_encoder, daemon=True)
        feeder.start()

        try:
            while True:
                chunk = encoder.stdout.read(4096)
                if not chunk:
                    break
                yield chunk
        finally:
            stop.set()
            try:
                encoder.stdin.close()
            except OSError:
                pass
            encoder.kill()
            encoder.wait()

    return StreamingResponse(
        generate(),
        media_type=mimetype,
        headers={"Cache-Control": "no-cache"},
    )
```

- [ ] **Step 2: Commit**

```bash
git add src/streamer/server.py
git commit -m "refactor: rewrite server.py from Flask to FastAPI"
```

---

### Task 3: Migrate Server Tests

**Files:**
- Modify: `tests/test_server.py` (full rewrite)

Rewrite all tests to use FastAPI's `TestClient` (backed by httpx). Key changes: `resp.text` instead of `resp.data.decode()`, `resp.json()` instead of `resp.get_json()`, `auth=(user, pass)` for auth headers, 303 status for POST redirects.

- [ ] **Step 1: Write the migrated test file**

Replace the entire contents of `tests/test_server.py` with:

```python
import time

import pytest
from fastapi.testclient import TestClient

from streamer.pipeline import AudioPipeline
from streamer.scanner import Scanner
from streamer.server import create_app
from streamer.state import ServerState


@pytest.fixture
def app(test_media_dir):
    state = ServerState()
    scanner = Scanner(roots=[
        test_media_dir / "entertainment",
        test_media_dir / "Podcast",
    ])
    state.current_track = str(
        test_media_dir / "entertainment" / "Test Show" / "season 01" / "01.mp3"
    )
    return create_app(state=state, scanner=scanner)


@pytest.fixture
def client(app):
    return TestClient(app)


class TestLandingPage:
    def test_shows_current_track(self, client, app):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "01.mp3" in resp.text
        assert app.state.server_state.current_track in resp.text

    def test_shows_empty_queue_message(self, client):
        resp = client.get("/")
        assert "empty" in resp.text.lower()

    def test_shows_queue_items(self, client, app):
        app.state.server_state.queue_add(r"C:\media\test\02.mp3")
        resp = client.get("/")
        assert "02.mp3" in resp.text

    def test_has_navigation_links(self, client):
        resp = client.get("/")
        assert "/browse" in resp.text
        assert "/stream.ogg" in resp.text

    def test_has_accessible_structure(self, client):
        resp = client.get("/")
        assert "<h1" in resp.text
        assert "<main" in resp.text


class TestControls:
    def test_next_redirects(self, client):
        resp = client.post("/next", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/"

    def test_previous_redirects(self, client):
        resp = client.post("/previous", follow_redirects=False)
        assert resp.status_code == 303

    def test_queue_add(self, client, app, test_media_dir):
        file_path = "entertainment/Test Show/season 01/01.mp3"
        resp = client.post(
            "/queue/add", data={"file": file_path}, follow_redirects=False,
        )
        assert resp.status_code == 303
        assert len(app.state.server_state.queue) == 1

    def test_queue_remove(self, client, app):
        app.state.server_state.queue_add("a.mp3")
        app.state.server_state.queue_add("b.mp3")
        resp = client.post(
            "/queue/remove", data={"index": "0"}, follow_redirects=False,
        )
        assert resp.status_code == 303
        assert app.state.server_state.queue == ["b.mp3"]

    def test_dj_toggle(self, client, app):
        assert app.state.server_state.dj_enabled is False
        resp = client.post("/dj/toggle", follow_redirects=False)
        assert resp.status_code == 303
        assert app.state.server_state.dj_enabled is True


class TestApiState:
    def test_returns_current_state(self, client, app):
        resp = client.get("/api/state")
        assert resp.status_code == 200
        data = resp.json()
        assert "track_name" in data
        assert "queue" in data
        assert data["dj_enabled"] is False
        assert data["curator_enabled"] is False
        assert data["curator_reason"] is None

    def test_reflects_queue_changes(self, client, app):
        app.state.server_state.queue_add(r"C:\media\test\song.mp3")
        resp = client.get("/api/state")
        data = resp.json()
        assert len(data["queue"]) == 1
        assert data["queue"][0]["name"] == "song.mp3"


class TestCuratorToggle:
    def test_curator_toggle(self, client, app):
        assert app.state.server_state.curator_enabled is False
        resp = client.post("/curator/toggle", follow_redirects=False)
        assert resp.status_code == 303
        assert app.state.server_state.curator_enabled is True


class TestFileBrowser:
    def test_browse_root_shows_media_folders(self, client):
        resp = client.get("/browse/")
        assert resp.status_code == 200
        assert "entertainment" in resp.text
        assert "Podcast" in resp.text

    def test_browse_subfolder(self, client):
        resp = client.get("/browse/entertainment")
        assert resp.status_code == 200
        assert "Test Show" in resp.text

    def test_browse_audio_files(self, client):
        resp = client.get("/browse/entertainment/Test Show/season 01")
        assert resp.status_code == 200
        assert "01.mp3" in resp.text
        assert "02.mp3" in resp.text
        assert "notes.txt" not in resp.text

    def test_browse_nonexistent_returns_404(self, client):
        resp = client.get("/browse/nonexistent")
        assert resp.status_code == 404

    def test_play_action_page(self, client):
        resp = client.get(
            "/browse/play?file=entertainment/Test Show/season 01/01.mp3"
        )
        assert resp.status_code == 200
        assert "01.mp3" in resp.text
        assert "Play Now" in resp.text
        assert "Add to Queue" in resp.text

    def test_play_action_nonexistent_returns_404(self, client):
        resp = client.get("/browse/play?file=nope/nope.mp3")
        assert resp.status_code == 404

    def test_play_now_via_post(self, client, app, test_media_dir):
        resp = client.post(
            "/play",
            data={"file": "entertainment/Test Show/season 01/01.mp3"},
            follow_redirects=False,
        )
        assert resp.status_code == 303

    def test_queue_add_via_browse(self, client, app):
        resp = client.post(
            "/queue/add",
            data={"file": "entertainment/Test Show/season 01/02.mp3"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert len(app.state.server_state.queue) == 1
        assert "02.mp3" in app.state.server_state.queue[0]


class TestAuth:
    def test_no_auth_required_when_unconfigured(self, client):
        resp = client.get("/")
        assert resp.status_code == 200

    def test_auth_required_when_configured(self, app):
        import bcrypt

        password = "testpass"
        hashed = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt())

        import streamer.server as srv
        original_username = srv.AUTH_USERNAME
        original_hash = srv.AUTH_PASSWORD_HASH
        srv.AUTH_USERNAME = "admin"
        srv.AUTH_PASSWORD_HASH = hashed.decode("utf-8")
        try:
            client = TestClient(app)
            resp = client.get("/")
            assert resp.status_code == 401

            resp = client.get("/", auth=("admin", "testpass"))
            assert resp.status_code == 200

            resp = client.get("/", auth=("admin", "wrongpass"))
            assert resp.status_code == 401
        finally:
            srv.AUTH_USERNAME = original_username
            srv.AUTH_PASSWORD_HASH = original_hash

    def test_stream_open_when_auth_configured(self, app):
        import bcrypt

        hashed = bcrypt.hashpw(b"testpass", bcrypt.gensalt())

        import streamer.server as srv
        original_username = srv.AUTH_USERNAME
        original_hash = srv.AUTH_PASSWORD_HASH
        srv.AUTH_USERNAME = "admin"
        srv.AUTH_PASSWORD_HASH = hashed.decode("utf-8")
        try:
            client = TestClient(app)
            resp = client.get("/stream.ogg")
            assert resp.status_code == 200

            resp = client.get("/stream.mp3")
            assert resp.status_code == 200
        finally:
            srv.AUTH_USERNAME = original_username
            srv.AUTH_PASSWORD_HASH = original_hash


class TestStreamEndpoint:
    def test_stream_returns_ogg(self, test_media_dir):
        state = ServerState()
        scanner = Scanner(roots=[
            test_media_dir / "entertainment",
            test_media_dir / "Podcast",
        ])
        pipeline = AudioPipeline(state, scanner)
        app = create_app(state=state, scanner=scanner, pipeline=pipeline)

        pipeline.start()
        try:
            time.sleep(2)
            client = TestClient(app)
            with client.stream("GET", "/stream.ogg") as resp:
                assert resp.status_code == 200
                assert "audio/ogg" in resp.headers.get("content-type", "")
                first_chunk = next(resp.iter_bytes())
                assert first_chunk[:4] == b"OggS"
        finally:
            pipeline.stop()
```

- [ ] **Step 2: Run tests to verify migration**

Run: `uv run pytest tests/test_server.py -v`
Expected: All 25 existing tests pass. (The pre-existing `test_pipeline_produces_pcm_data` failure in test_pipeline.py is unrelated.)

- [ ] **Step 3: Run full test suite**

Run: `uv run pytest -v`
Expected: All tests pass (except the known pipeline test issue).

- [ ] **Step 4: Commit**

```bash
git add tests/test_server.py
git commit -m "test: migrate server tests to FastAPI TestClient"
```

---

### Task 4: Update Entry Point

**Files:**
- Modify: `src/streamer/__main__.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_server.py`:

```python
class TestAppCreation:
    def test_create_app_returns_fastapi(self):
        from fastapi import FastAPI
        app = create_app()
        assert isinstance(app, FastAPI)
```

- [ ] **Step 2: Run test to verify it passes**

Run: `uv run pytest tests/test_server.py::TestAppCreation -v`
Expected: PASS (the app is already FastAPI from Task 2).

- [ ] **Step 3: Update __main__.py**

Replace the entire contents of `src/streamer/__main__.py` with:

```python
import logging

import uvicorn

from streamer.config import HOST, PORT
from streamer.pipeline import AudioPipeline
from streamer.scanner import Scanner
from streamer.server import create_app
from streamer.state import ServerState


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(message)s",
        datefmt="%H:%M:%S",
    )

    state = ServerState()
    scanner = Scanner()
    pipeline = AudioPipeline(state, scanner)

    app = create_app(state=state, scanner=scanner, pipeline=pipeline)
    pipeline.start()

    print("Streaming server running")
    print(f"  Control panel: http://localhost:{PORT}")
    print(f"  Stream:        http://localhost:{PORT}/stream.ogg")
    print(f"  API docs:      http://localhost:{PORT}/docs")

    uvicorn.run(app, host=HOST, port=PORT)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Commit**

```bash
git add src/streamer/__main__.py tests/test_server.py
git commit -m "feat: switch entry point from Flask to uvicorn"
```

---

### Task 5: Pipeline Playback Info

**Files:**
- Modify: `src/streamer/pipeline.py:84-100,285-311`
- Test: `tests/test_pipeline.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_pipeline.py`:

```python
from unittest.mock import MagicMock, patch

from streamer.pipeline import AudioPipeline, BYTES_PER_SECOND
from streamer.state import ServerState


class TestPlaybackInfo:
    def test_get_playback_info_initial(self):
        state = ServerState()
        scanner = MagicMock()
        pipeline = AudioPipeline(state, scanner)
        info = pipeline.get_playback_info()
        assert info["elapsed"] == 0.0
        assert info["duration"] is None
        assert info["remaining"] is None

    def test_get_playback_info_with_duration(self):
        state = ServerState()
        scanner = MagicMock()
        pipeline = AudioPipeline(state, scanner)
        pipeline._track_duration = 180.0
        pipeline._track_bytes_written = BYTES_PER_SECOND * 30
        info = pipeline.get_playback_info()
        assert info["elapsed"] == 30.0
        assert info["duration"] == 180.0
        assert info["remaining"] == 150.0

    def test_probe_duration_returns_float(self, test_media_dir):
        state = ServerState()
        scanner = MagicMock()
        pipeline = AudioPipeline(state, scanner)
        path = str(
            test_media_dir / "entertainment" / "Test Show" / "season 01" / "01.mp3"
        )
        duration = pipeline._probe_duration(path)
        assert duration is not None
        assert isinstance(duration, float)
        assert duration > 0

    def test_probe_duration_returns_none_for_bad_file(self):
        state = ServerState()
        scanner = MagicMock()
        pipeline = AudioPipeline(state, scanner)
        duration = pipeline._probe_duration("/nonexistent/file.mp3")
        assert duration is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_pipeline.py::TestPlaybackInfo -v`
Expected: FAIL — `AudioPipeline` has no `get_playback_info` or `_probe_duration` methods.

- [ ] **Step 3: Add playback tracking to AudioPipeline.__init__**

In `src/streamer/pipeline.py`, add to `__init__` (after the existing `self._pre_selected_random` line around line 103):

```python
        self._track_duration: float | None = None
        self._track_bytes_written: int = 0
```

- [ ] **Step 4: Add _probe_duration method**

In `src/streamer/pipeline.py`, add after `_consume_prefetch` (around line 264):

```python
    def _probe_duration(self, path: str) -> float | None:
        try:
            proc = subprocess.run(
                [
                    "ffprobe", "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "csv=p=0",
                    path,
                ],
                capture_output=True,
                text=True,
                timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                return float(proc.stdout.strip())
        except Exception:
            pass
        return None

    def get_playback_info(self) -> dict:
        elapsed = self._track_bytes_written / BYTES_PER_SECOND
        duration = self._track_duration
        return {
            "elapsed": round(elapsed, 1),
            "duration": round(duration, 1) if duration else None,
            "remaining": round(duration - elapsed, 1) if duration else None,
        }
```

- [ ] **Step 5: Update _run to use instance variables**

In the `_run` method, replace the section after `decoder = self._start_decoder(track)`:

Find:
```python
            decoder = self._start_decoder(track)
            self._current_decoder = decoder

            # While this track plays, generate the clip for the next transition.
            if self.state.dj_enabled:
                self._start_clip_prefetch(track)

            track_start = time.monotonic()
            bytes_written = 0

            while self._running:
                chunk = decoder.stdout.read(4096)
                if not chunk:
                    break
                self.pcm_buffer.write(chunk)
                self._write_to_ogg_encoder(chunk)
                bytes_written += len(chunk)

                expected = bytes_written / BYTES_PER_SECOND
```

Replace with:
```python
            decoder = self._start_decoder(track)
            self._current_decoder = decoder
            self._track_duration = self._probe_duration(track)
            self._track_bytes_written = 0

            # While this track plays, generate the clip for the next transition.
            if self.state.dj_enabled:
                self._start_clip_prefetch(track)

            track_start = time.monotonic()

            while self._running:
                chunk = decoder.stdout.read(4096)
                if not chunk:
                    break
                self.pcm_buffer.write(chunk)
                self._write_to_ogg_encoder(chunk)
                self._track_bytes_written += len(chunk)

                expected = self._track_bytes_written / BYTES_PER_SECOND
```

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/test_pipeline.py::TestPlaybackInfo -v`
Expected: All 4 tests PASS.

- [ ] **Step 7: Commit**

```bash
git add src/streamer/pipeline.py tests/test_pipeline.py
git commit -m "feat: add playback duration and elapsed time tracking"
```

---

### Task 6: API — Now-Playing and Track Control

**Files:**
- Modify: `src/streamer/server.py`
- Test: `tests/test_server.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_server.py`:

```python
from unittest.mock import MagicMock


@pytest.fixture
def mock_pipeline():
    pipeline = MagicMock()
    pipeline.get_playback_info.return_value = {
        "elapsed": 30.5,
        "duration": 180.0,
        "remaining": 149.5,
    }
    pipeline.request_next = MagicMock()
    pipeline.request_previous = MagicMock(return_value=True)
    pipeline.request_play = MagicMock()
    pipeline._curator = MagicMock()
    pipeline._curator.get_status.return_value = {
        "enabled": False,
        "reason": None,
        "tracks_since_check": 2,
        "next_check_at": 5,
    }
    pipeline._curator.get_chat_history.return_value = []
    pipeline._curator.chat.return_value = {
        "response": "Sure thing.",
        "queued": [],
    }
    return pipeline


@pytest.fixture
def app_with_pipeline(test_media_dir, mock_pipeline):
    state = ServerState()
    scanner = Scanner(roots=[
        test_media_dir / "entertainment",
        test_media_dir / "Podcast",
    ])
    state.current_track = str(
        test_media_dir / "entertainment" / "Test Show" / "season 01" / "01.mp3"
    )
    return create_app(state=state, scanner=scanner, pipeline=mock_pipeline)


@pytest.fixture
def api_client(app_with_pipeline):
    return TestClient(app_with_pipeline)


class TestApiNowPlaying:
    def test_returns_track_info(self, api_client):
        resp = api_client.get("/api/now-playing")
        assert resp.status_code == 200
        data = resp.json()
        assert data["track_name"] == "01.mp3"
        assert data["elapsed"] == 30.5
        assert data["duration"] == 180.0
        assert data["remaining"] == 149.5

    def test_returns_nulls_without_pipeline(self, client):
        resp = client.get("/api/now-playing")
        assert resp.status_code == 200
        data = resp.json()
        assert data["elapsed"] is None
        assert data["duration"] is None


class TestApiTrackControl:
    def test_next(self, api_client, mock_pipeline):
        resp = api_client.post("/api/tracks/next")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        mock_pipeline.request_next.assert_called_once()

    def test_previous(self, api_client, mock_pipeline):
        resp = api_client.post("/api/tracks/previous")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_previous_no_history(self, api_client, mock_pipeline):
        mock_pipeline.request_previous.return_value = False
        resp = api_client.post("/api/tracks/previous")
        data = resp.json()
        assert data["ok"] is False

    def test_play_valid_path(self, api_client, mock_pipeline):
        resp = api_client.post(
            "/api/tracks/play",
            json={"path": "entertainment/Test Show/season 01/01.mp3"},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        mock_pipeline.request_play.assert_called_once()

    def test_play_invalid_path(self, api_client):
        resp = api_client.post(
            "/api/tracks/play",
            json={"path": "nonexistent/file.mp3"},
        )
        assert resp.json()["ok"] is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_server.py::TestApiNowPlaying tests/test_server.py::TestApiTrackControl -v`
Expected: FAIL — routes not defined yet.

- [ ] **Step 3: Add the routes to server.py**

In `src/streamer/server.py`, add inside `create_app` after the `/api/state` route:

```python
    # ── JSON API ─────────────────────────────────────────────────────────

    @app.get("/api/now-playing")
    def api_now_playing(_user: str = Depends(verify_credentials)):
        current = _state.current_track
        info = {"elapsed": None, "duration": None, "remaining": None}
        if _pipeline:
            info = _pipeline.get_playback_info()
        return {
            "track_name": Path(current).name if current else "Nothing playing",
            "track_path": current or "",
            **info,
        }

    @app.post("/api/tracks/next")
    def api_tracks_next(_user: str = Depends(verify_credentials)):
        if _pipeline:
            _pipeline.request_next()
        return {"ok": True}

    @app.post("/api/tracks/previous")
    def api_tracks_previous(_user: str = Depends(verify_credentials)):
        if _pipeline and _pipeline.request_previous():
            return {"ok": True, "track": _state.current_track}
        return {"ok": False}

    @app.post("/api/tracks/play")
    def api_tracks_play(
        body: PathBody,
        _user: str = Depends(verify_credentials),
    ):
        resolved = _scanner.resolve_browse_path(body.path)
        if resolved and resolved.is_file():
            if _pipeline:
                _pipeline.request_play(str(resolved))
            return {"ok": True}
        return {"ok": False}
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_server.py::TestApiNowPlaying tests/test_server.py::TestApiTrackControl -v`
Expected: All 7 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/streamer/server.py tests/test_server.py
git commit -m "feat: add now-playing and track control API endpoints"
```

---

### Task 7: API — Queue Management

**Files:**
- Modify: `src/streamer/server.py`
- Test: `tests/test_server.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_server.py`:

```python
class TestApiQueue:
    def test_get_empty_queue(self, api_client):
        resp = api_client.get("/api/queue")
        assert resp.status_code == 200
        assert resp.json()["queue"] == []

    def test_get_queue_with_items(self, api_client, app_with_pipeline):
        app_with_pipeline.state.server_state.queue_add(r"C:\media\track.mp3")
        resp = api_client.get("/api/queue")
        data = resp.json()
        assert len(data["queue"]) == 1
        assert data["queue"][0]["name"] == "track.mp3"
        assert data["queue"][0]["index"] == 0

    def test_enqueue_valid_path(self, api_client, app_with_pipeline):
        resp = api_client.post(
            "/api/queue",
            json={"path": "entertainment/Test Show/season 01/01.mp3"},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert len(app_with_pipeline.state.server_state.queue) == 1

    def test_enqueue_invalid_path(self, api_client):
        resp = api_client.post(
            "/api/queue",
            json={"path": "nonexistent/file.mp3"},
        )
        assert resp.json()["ok"] is False

    def test_delete_queue_item(self, api_client, app_with_pipeline):
        app_with_pipeline.state.server_state.queue_add("a.mp3")
        app_with_pipeline.state.server_state.queue_add("b.mp3")
        resp = api_client.delete("/api/queue/0")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert app_with_pipeline.state.server_state.queue == ["b.mp3"]

    def test_delete_invalid_index(self, api_client):
        resp = api_client.delete("/api/queue/99")
        assert resp.json()["ok"] is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_server.py::TestApiQueue -v`
Expected: FAIL — routes not defined.

- [ ] **Step 3: Add queue API routes**

In `src/streamer/server.py`, add inside `create_app` after the track control routes:

```python
    @app.get("/api/queue")
    def api_queue_list(_user: str = Depends(verify_credentials)):
        return {
            "queue": [
                {"name": Path(p).name, "path": p, "index": i}
                for i, p in enumerate(_state.queue)
            ],
        }

    @app.post("/api/queue")
    def api_queue_add(
        body: PathBody,
        _user: str = Depends(verify_credentials),
    ):
        resolved = _scanner.resolve_browse_path(body.path)
        if resolved and resolved.is_file():
            _state.queue_add(str(resolved))
            return {"ok": True}
        return {"ok": False}

    @app.delete("/api/queue/{index}")
    def api_queue_remove(
        index: int,
        _user: str = Depends(verify_credentials),
    ):
        if _state.queue_remove(index):
            return {"ok": True}
        return {"ok": False}
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_server.py::TestApiQueue -v`
Expected: All 6 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/streamer/server.py tests/test_server.py
git commit -m "feat: add queue management API endpoints"
```

---

### Task 8: API — DJ, Curator Status, Force, and Browse

**Files:**
- Modify: `src/streamer/server.py`
- Modify: `src/streamer/curator.py`
- Test: `tests/test_server.py`
- Test: `tests/test_curator.py`

- [ ] **Step 1: Write failing test for Curator.get_status**

Add to `tests/test_curator.py`:

```python
class TestCuratorStatus:
    def test_get_status(self):
        curator = _make_curator()
        curator.state.curator_enabled = True
        curator.state.curator_reason = "Marathon"
        curator._tracks_since_check = 3
        curator._next_check_at = 7
        status = curator.get_status()
        assert status["enabled"] is True
        assert status["reason"] == "Marathon"
        assert status["tracks_since_check"] == 3
        assert status["next_check_at"] == 7
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_curator.py::TestCuratorStatus -v`
Expected: FAIL — `Curator` has no `get_status` method.

- [ ] **Step 3: Add get_status to Curator**

In `src/streamer/curator.py`, add after the `trigger` method:

```python
    def get_status(self) -> dict:
        return {
            "enabled": self.state.curator_enabled,
            "reason": self.state.curator_reason,
            "tracks_since_check": self._tracks_since_check,
            "next_check_at": self._next_check_at,
        }
```

- [ ] **Step 4: Run curator status test**

Run: `uv run pytest tests/test_curator.py::TestCuratorStatus -v`
Expected: PASS.

- [ ] **Step 5: Write failing API tests**

Add to `tests/test_server.py`:

```python
class TestApiDJ:
    def test_get_dj_status(self, api_client):
        resp = api_client.get("/api/dj")
        assert resp.status_code == 200
        assert resp.json()["enabled"] is False

    def test_set_dj_enabled(self, api_client, app_with_pipeline):
        resp = api_client.post("/api/dj", json={"enabled": True})
        assert resp.status_code == 200
        assert resp.json()["enabled"] is True
        assert app_with_pipeline.state.server_state.dj_enabled is True

    def test_set_dj_disabled(self, api_client, app_with_pipeline):
        app_with_pipeline.state.server_state.dj_enabled = True
        resp = api_client.post("/api/dj", json={"enabled": False})
        assert resp.json()["enabled"] is False


class TestApiCurator:
    def test_get_curator_status(self, api_client, mock_pipeline):
        resp = api_client.get("/api/curator")
        assert resp.status_code == 200
        data = resp.json()
        assert "enabled" in data
        assert "tracks_since_check" in data
        assert "next_check_at" in data

    def test_set_curator_enabled(self, api_client, app_with_pipeline):
        resp = api_client.post("/api/curator", json={"enabled": True})
        assert resp.status_code == 200
        assert resp.json()["enabled"] is True

    def test_force_check(self, api_client, mock_pipeline):
        resp = api_client.post("/api/curator/force")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        mock_pipeline._curator.trigger.assert_called_once()


class TestApiBrowse:
    def test_browse_root(self, api_client):
        resp = api_client.get("/api/browse")
        assert resp.status_code == 200
        data = resp.json()
        names = [d["name"] for d in data["dirs"]]
        assert "entertainment" in names
        assert "Podcast" in names
        assert data["files"] == []

    def test_browse_subpath(self, api_client):
        resp = api_client.get("/api/browse/entertainment/Test Show/season 01")
        assert resp.status_code == 200
        data = resp.json()
        file_names = [f["name"] for f in data["files"]]
        assert "01.mp3" in file_names
        assert "02.mp3" in file_names
        assert "notes.txt" not in file_names

    def test_browse_nonexistent_returns_404(self, api_client):
        resp = api_client.get("/api/browse/nonexistent")
        assert resp.status_code == 404

    def test_browse_paths_are_relative(self, api_client):
        resp = api_client.get("/api/browse/entertainment/Test Show/season 01")
        data = resp.json()
        for f in data["files"]:
            assert "path" in f
            assert f["path"].startswith("entertainment/")
```

- [ ] **Step 6: Run tests to verify they fail**

Run: `uv run pytest tests/test_server.py::TestApiDJ tests/test_server.py::TestApiCurator tests/test_server.py::TestApiBrowse -v`
Expected: FAIL — routes not defined.

- [ ] **Step 7: Add DJ, curator, force, and browse API routes**

In `src/streamer/server.py`, add inside `create_app` after the queue routes:

```python
    @app.get("/api/dj")
    def api_dj_status(_user: str = Depends(verify_credentials)):
        return {"enabled": _state.dj_enabled}

    @app.post("/api/dj")
    def api_dj_toggle(
        body: ToggleBody,
        _user: str = Depends(verify_credentials),
    ):
        _state.dj_enabled = body.enabled
        return {"enabled": _state.dj_enabled}

    @app.get("/api/curator")
    def api_curator_status(_user: str = Depends(verify_credentials)):
        if _pipeline:
            return _pipeline._curator.get_status()
        return {
            "enabled": _state.curator_enabled,
            "reason": _state.curator_reason,
            "tracks_since_check": 0,
            "next_check_at": 0,
        }

    @app.post("/api/curator")
    def api_curator_toggle(
        body: ToggleBody,
        _user: str = Depends(verify_credentials),
    ):
        _state.curator_enabled = body.enabled
        if body.enabled and _pipeline:
            _pipeline._curator.trigger()
        return {"enabled": _state.curator_enabled}

    @app.post("/api/curator/force")
    def api_curator_force(_user: str = Depends(verify_credentials)):
        if _pipeline:
            _pipeline._curator.trigger()
        return {"ok": True}

    @app.get("/api/browse")
    def api_browse_root(_user: str = Depends(verify_credentials)):
        dirs = [
            {"name": root.name, "path": root.name}
            for root in _scanner.roots
            if root.exists()
        ]
        return {"dirs": dirs, "files": []}

    @app.get("/api/browse/{subpath:path}")
    def api_browse_subpath(
        subpath: str,
        _user: str = Depends(verify_credentials),
    ):
        resolved = _scanner.resolve_browse_path(subpath)
        if resolved is None or not resolved.is_dir():
            raise HTTPException(status_code=404)
        dir_names, file_names = _scanner.list_directory(resolved)
        dirs = [
            {"name": d, "path": subpath + "/" + d}
            for d in dir_names
        ]
        files = [
            {"name": f, "path": subpath + "/" + f}
            for f in file_names
        ]
        return {"dirs": dirs, "files": files}
```

- [ ] **Step 8: Run tests**

Run: `uv run pytest tests/test_server.py::TestApiDJ tests/test_server.py::TestApiCurator tests/test_server.py::TestApiBrowse tests/test_curator.py::TestCuratorStatus -v`
Expected: All tests PASS.

- [ ] **Step 9: Commit**

```bash
git add src/streamer/server.py src/streamer/curator.py tests/test_server.py tests/test_curator.py
git commit -m "feat: add DJ, curator, force-check, and browse API endpoints"
```

---

### Task 9: Curator Chat

**Files:**
- Modify: `src/streamer/curator.py`
- Modify: `src/streamer/server.py`
- Test: `tests/test_curator.py`
- Test: `tests/test_server.py`

- [ ] **Step 1: Write failing curator chat tests**

Add to `tests/test_curator.py`:

```python
class TestCuratorChat:
    def test_get_chat_history_initially_empty(self):
        curator = _make_curator()
        assert curator.get_chat_history() == []

    def test_chat_appends_to_history(self):
        curator = _make_curator()
        curator._chat_history.append({"role": "user", "content": "hello"})
        curator._chat_history.append({"role": "assistant", "content": "hi"})
        history = curator.get_chat_history()
        assert len(history) == 2
        assert history[0]["role"] == "user"
        assert history[1]["role"] == "assistant"

    @patch("streamer.curator.OLLAMA_URL", "http://localhost:11434")
    @patch("streamer.curator.urllib.request.urlopen")
    def test_chat_returns_response(self, mock_urlopen):
        response_body = json.dumps({
            "message": {"content": "I recommend Red Dwarf!"}
        }).encode()
        mock_response = MagicMock()
        mock_response.read.return_value = response_body
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        curator = _make_curator()
        result = curator.chat("What should I listen to?")
        assert result["response"] == "I recommend Red Dwarf!"
        assert result["queued"] == []
        assert len(curator.get_chat_history()) == 2

    @patch("streamer.curator.OLLAMA_URL", "http://localhost:11434")
    @patch("streamer.curator.urllib.request.urlopen")
    def test_chat_extracts_queue_action(self, mock_urlopen):
        response_text = (
            'Sure! Here you go.\n'
            '{"action": "queue", "tracks": ["Show/season 01/01"], "reason": "By request"}'
        )
        response_body = json.dumps({
            "message": {"content": response_text}
        }).encode()
        mock_response = MagicMock()
        mock_response.read.return_value = response_body
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        state = ServerState()
        scanner = MagicMock()
        scanner.roots = []
        curator = Curator(state, scanner)

        with patch.object(curator, "_resolve_tracks", return_value=["/path/01.mp3"]):
            with patch.object(curator, "_ensure_ollama_running", return_value=True):
                result = curator.chat("Play Show season 1")

        assert len(result["queued"]) == 1
        assert result["queued"][0] == "/path/01.mp3"
        assert state.queue == ["/path/01.mp3"]

    @patch("streamer.curator.OLLAMA_URL", "")
    def test_chat_fails_without_ollama_url(self):
        curator = _make_curator()
        result = curator.chat("hello")
        assert "queued" in result
        assert result["queued"] == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_curator.py::TestCuratorChat -v`
Expected: FAIL — `Curator` has no `chat` or `get_chat_history` methods.

- [ ] **Step 3: Add CURATOR_CHAT_PROMPT and chat methods to curator.py**

In `src/streamer/curator.py`, add the chat prompt after `CURATOR_SYSTEM_PROMPT`:

```python
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
```

In `Curator.__init__`, add after `self._force_check`:

```python
        self._chat_history: list[dict] = []
```

Add these methods to the `Curator` class:

```python
    def get_chat_history(self) -> list[dict]:
        return list(self._chat_history)

    def chat(self, message: str) -> dict:
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

        # Try the whole response as JSON
        try:
            data = json.loads(response_text.strip())
            if isinstance(data, dict) and data.get("action") == "queue":
                return self._queue_from_chat(data, path_lookup)
        except (json.JSONDecodeError, ValueError):
            pass

        # Look for JSON in code blocks
        for match in re.finditer(
            r'```(?:json)?\s*(\{.*?\})\s*```', response_text, re.DOTALL,
        ):
            try:
                data = json.loads(match.group(1))
                if isinstance(data, dict) and data.get("action") == "queue":
                    return self._queue_from_chat(data, path_lookup)
            except (json.JSONDecodeError, ValueError):
                continue

        # Look for bare JSON objects
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
```

- [ ] **Step 4: Run curator chat tests**

Run: `uv run pytest tests/test_curator.py::TestCuratorChat -v`
Expected: All 5 tests PASS.

- [ ] **Step 5: Write failing API chat tests**

Add to `tests/test_server.py`:

```python
class TestApiCuratorChat:
    def test_get_chat_empty(self, api_client):
        resp = api_client.get("/api/curator/chat")
        assert resp.status_code == 200
        assert resp.json()["messages"] == []

    def test_post_chat_message(self, api_client, mock_pipeline):
        resp = api_client.post(
            "/api/curator/chat",
            json={"message": "What should I listen to?"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "response" in data
        assert "queued" in data
        mock_pipeline._curator.chat.assert_called_once_with(
            "What should I listen to?"
        )

    def test_chat_without_pipeline(self, client):
        resp = client.post(
            "/api/curator/chat",
            json={"message": "hello"},
        )
        assert resp.status_code == 200
        assert "response" in resp.json()
```

- [ ] **Step 6: Run tests to verify they fail**

Run: `uv run pytest tests/test_server.py::TestApiCuratorChat -v`
Expected: FAIL — routes not defined.

- [ ] **Step 7: Add chat API routes**

In `src/streamer/server.py`, add inside `create_app` after the curator force route:

```python
    @app.get("/api/curator/chat")
    def api_curator_chat_history(_user: str = Depends(verify_credentials)):
        if _pipeline:
            return {"messages": _pipeline._curator.get_chat_history()}
        return {"messages": []}

    @app.post("/api/curator/chat")
    def api_curator_chat_send(
        body: ChatBody,
        _user: str = Depends(verify_credentials),
    ):
        if not _pipeline:
            return {"response": "Pipeline not available.", "queued": []}
        return _pipeline._curator.chat(body.message)
```

- [ ] **Step 8: Run all chat tests**

Run: `uv run pytest tests/test_server.py::TestApiCuratorChat tests/test_curator.py::TestCuratorChat -v`
Expected: All 8 tests PASS.

- [ ] **Step 9: Commit**

```bash
git add src/streamer/curator.py src/streamer/server.py tests/test_curator.py tests/test_server.py
git commit -m "feat: add curator chat with Ollama integration and API endpoints"
```

---

### Task 10: Enrich /api/state and Update Control Panel

**Files:**
- Modify: `src/streamer/server.py` (enrich `/api/state`)
- Modify: `src/streamer/templates/index.html`
- Test: `tests/test_server.py`

- [ ] **Step 1: Write failing test for enriched /api/state**

Add to `tests/test_server.py`:

```python
class TestApiStateEnriched:
    def test_state_includes_timing(self, api_client):
        resp = api_client.get("/api/state")
        data = resp.json()
        assert "elapsed" in data
        assert "duration" in data
        assert "remaining" in data
        assert data["elapsed"] == 30.5

    def test_state_includes_curator_status(self, api_client):
        resp = api_client.get("/api/state")
        data = resp.json()
        assert "curator_tracks_since_check" in data
        assert "curator_next_check_at" in data

    def test_state_timing_null_without_pipeline(self, client):
        resp = client.get("/api/state")
        data = resp.json()
        assert data["elapsed"] is None
        assert data["duration"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_server.py::TestApiStateEnriched -v`
Expected: FAIL — `/api/state` doesn't include timing or curator status fields.

- [ ] **Step 3: Enrich the /api/state route**

In `src/streamer/server.py`, replace the existing `api_state` function inside `create_app`:

```python
    @app.get("/api/state")
    def api_state(_user: str = Depends(verify_credentials)):
        current = _state.current_track
        info = {"elapsed": None, "duration": None, "remaining": None}
        if _pipeline:
            info = _pipeline.get_playback_info()
        curator_status = {}
        if _pipeline:
            curator_status = _pipeline._curator.get_status()
        return {
            "track_name": Path(current).name if current else "Nothing playing",
            "track_path": current or "",
            "queue": [
                {"name": Path(p).name, "path": p}
                for p in _state.queue
            ],
            "dj_enabled": _state.dj_enabled,
            "curator_enabled": _state.curator_enabled,
            "curator_reason": _state.curator_reason,
            "elapsed": info["elapsed"],
            "duration": info["duration"],
            "remaining": info["remaining"],
            "curator_tracks_since_check": curator_status.get("tracks_since_check"),
            "curator_next_check_at": curator_status.get("next_check_at"),
        }
```

- [ ] **Step 4: Run enriched state tests**

Run: `uv run pytest tests/test_server.py::TestApiStateEnriched -v`
Expected: All 3 tests PASS.

- [ ] **Step 5: Update the control panel HTML**

Replace the entire contents of `src/streamer/templates/index.html` with:

```html
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Streamer</title>
</head>
<body>
<main>
    <h1>Now Playing</h1>
    <p aria-live="polite" id="now-playing"><strong id="track-name">{{ track_name }}</strong> &mdash; <span id="track-path">{{ track_path }}</span></p>
    <p aria-live="off" id="track-timing"></p>

    <section aria-label="Playback controls">
        <h2>Controls</h2>
        <form method="post" action="/previous" style="display:inline">
            <button type="submit">Previous</button>
        </form>
        <form method="post" action="/next" style="display:inline">
            <button type="submit">Next</button>
        </form>
    </section>

    <section aria-label="Queue" id="queue-section">
        <h2>Queue</h2>
        <p id="curator-reason" {% if not curator_reason %}hidden{% endif %}><strong>Curator:</strong> <span id="curator-reason-text">{{ curator_reason or "" }}</span></p>
        <div id="queue-content">
        {% if queue %}
        <ol>
            {% for item in queue %}
            <li>
                <strong>{{ item.name }}</strong> &mdash; {{ item.path }}
                <form method="post" action="/queue/remove" style="display:inline">
                    <input type="hidden" name="index" value="{{ loop.index0 }}">
                    <button type="submit" aria-label="Remove {{ item.name }} from queue">Remove</button>
                </form>
            </li>
            {% endfor %}
        </ol>
        {% else %}
        <p>Queue is empty. A random file will be chosen next.</p>
        {% endif %}
        </div>
    </section>

    <section aria-label="AI DJ">
        <h2>AI DJ</h2>
        <p>DJ is currently <strong id="dj-status">{{ "on" if dj_enabled else "off" }}</strong>.</p>
        <form method="post" action="/dj/toggle">
            <button type="submit" id="dj-toggle">Turn {{ "off" if dj_enabled else "on" }}</button>
        </form>
    </section>

    <section aria-label="AI Curator">
        <h2>AI Curator</h2>
        <p>Curator is currently <strong id="curator-status">{{ "on" if curator_enabled else "off" }}</strong>.</p>
        <form method="post" action="/curator/toggle">
            <button type="submit" id="curator-toggle">Turn {{ "off" if curator_enabled else "on" }}</button>
        </form>
        <p id="curator-check-info" hidden>Tracks until next check: <strong id="curator-tracks-remaining">-</strong></p>
        <button type="button" id="curator-force" hidden>Force Check</button>
    </section>

    <section aria-label="Chat with Curator">
        <h2>Chat with Curator</h2>
        <div id="chat-messages" role="log" aria-live="polite" style="max-height:300px;overflow-y:auto;border:1px solid #ccc;padding:8px;margin-bottom:8px"></div>
        <form id="chat-form">
            <label for="chat-input">Message:</label>
            <input type="text" id="chat-input" name="message" autocomplete="off">
            <button type="submit">Send</button>
        </form>
    </section>

    <nav aria-label="Actions">
        <h2>Actions</h2>
        <ul>
            <li><a href="/browse/">Browse Files</a></li>
            <li><a href="/stream.ogg">Listen to Stream</a></li>
        </ul>
    </nav>
</main>
<script>
(function() {
    var interval = 5000;

    function formatTime(secs) {
        if (secs == null) return "--:--";
        var mins = Math.floor(secs / 60);
        var sec = Math.floor(secs % 60);
        return mins + ":" + (sec < 10 ? "0" : "") + sec;
    }

    function esc(t) {
        var d = document.createElement("div");
        d.textContent = t;
        return d.innerHTML;
    }

    function update() {
        fetch("/api/state").then(function(r) { return r.json(); }).then(function(s) {
            document.getElementById("track-name").textContent = s.track_name;
            document.getElementById("track-path").textContent = s.track_path;

            var timing = document.getElementById("track-timing");
            timing.textContent = formatTime(s.elapsed) + " / " + formatTime(s.duration);

            var reason = document.getElementById("curator-reason");
            var reasonText = document.getElementById("curator-reason-text");
            if (s.curator_reason) {
                reasonText.textContent = s.curator_reason;
                reason.hidden = false;
            } else {
                reason.hidden = true;
            }

            var qc = document.getElementById("queue-content");
            if (s.queue.length === 0) {
                qc.innerHTML = "<p>Queue is empty. A random file will be chosen next.</p>";
            } else {
                var html = "<ol>";
                for (var i = 0; i < s.queue.length; i++) {
                    html += "<li><strong>" + esc(s.queue[i].name) + "</strong> &mdash; " + esc(s.queue[i].path)
                        + ' <form method="post" action="/queue/remove" style="display:inline">'
                        + '<input type="hidden" name="index" value="' + i + '">'
                        + '<button type="submit" aria-label="Remove ' + esc(s.queue[i].name) + ' from queue">Remove</button>'
                        + "</form></li>";
                }
                html += "</ol>";
                qc.innerHTML = html;
            }

            document.getElementById("dj-status").textContent = s.dj_enabled ? "on" : "off";
            document.getElementById("dj-toggle").textContent = s.dj_enabled ? "Turn off" : "Turn on";
            document.getElementById("curator-status").textContent = s.curator_enabled ? "on" : "off";
            document.getElementById("curator-toggle").textContent = s.curator_enabled ? "Turn off" : "Turn on";

            var checkInfo = document.getElementById("curator-check-info");
            var forceBtn = document.getElementById("curator-force");
            if (s.curator_enabled && s.curator_next_check_at != null) {
                var remaining = s.curator_next_check_at - (s.curator_tracks_since_check || 0);
                document.getElementById("curator-tracks-remaining").textContent = remaining;
                checkInfo.hidden = false;
                forceBtn.hidden = false;
            } else {
                checkInfo.hidden = true;
                forceBtn.hidden = true;
            }
        }).catch(function() {});
    }

    document.getElementById("curator-force").addEventListener("click", function() {
        fetch("/api/curator/force", {method: "POST"});
    });

    var chatMessages = document.getElementById("chat-messages");
    var chatForm = document.getElementById("chat-form");
    var chatInput = document.getElementById("chat-input");

    function addChatMessage(role, text) {
        var div = document.createElement("div");
        var strong = document.createElement("strong");
        strong.textContent = role + ": ";
        div.appendChild(strong);
        div.appendChild(document.createTextNode(text));
        chatMessages.appendChild(div);
        chatMessages.scrollTop = chatMessages.scrollHeight;
    }

    chatForm.addEventListener("submit", function(e) {
        e.preventDefault();
        var msg = chatInput.value.trim();
        if (!msg) return;
        chatInput.value = "";
        addChatMessage("You", msg);
        fetch("/api/curator/chat", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({message: msg})
        }).then(function(r) { return r.json(); }).then(function(data) {
            addChatMessage("Curator", data.response);
            if (data.queued && data.queued.length > 0) {
                addChatMessage("System", "Queued " + data.queued.length + " track(s).");
            }
        }).catch(function() {
            addChatMessage("System", "Failed to reach the curator.");
        });
    });

    fetch("/api/curator/chat").then(function(r) { return r.json(); }).then(function(data) {
        for (var i = 0; i < data.messages.length; i++) {
            var m = data.messages[i];
            addChatMessage(m.role === "user" ? "You" : "Curator", m.content);
        }
    }).catch(function() {});

    setInterval(update, interval);
})();
</script>
</body>
</html>
```

- [ ] **Step 6: Write control panel tests**

Add to `tests/test_server.py`:

```python
class TestControlPanelUpdates:
    def test_has_timing_element(self, client):
        resp = client.get("/")
        assert 'id="track-timing"' in resp.text

    def test_has_curator_force_button(self, client):
        resp = client.get("/")
        assert 'id="curator-force"' in resp.text

    def test_has_chat_section(self, client):
        resp = client.get("/")
        assert 'id="chat-messages"' in resp.text
        assert 'id="chat-input"' in resp.text
        assert "Chat with Curator" in resp.text

    def test_chat_section_is_accessible(self, client):
        resp = client.get("/")
        assert 'role="log"' in resp.text
        assert 'for="chat-input"' in resp.text
```

- [ ] **Step 7: Run all tests**

Run: `uv run pytest -v`
Expected: All tests pass.

- [ ] **Step 8: Commit**

```bash
git add src/streamer/server.py src/streamer/templates/index.html tests/test_server.py
git commit -m "feat: enrich /api/state with timing, add curator chat and status to control panel"
```

---

### Task 11: Final Verification

- [ ] **Step 1: Run full test suite**

Run: `uv run pytest -v`
Expected: All tests pass (except the known pre-existing pipeline ogg_buffer test).

- [ ] **Step 2: Manual smoke test**

Start the server: `uv run streamer`

Verify:
- Control panel loads at `http://localhost:8054`
- Timing display shows elapsed/duration
- API docs available at `http://localhost:8054/docs`
- `/api/now-playing` returns track info with timing
- `/api/browse` lists media roots
- Curator chat sends/receives messages
- Force check button triggers curator
- Stream works at `/stream.ogg`

- [ ] **Step 3: Final commit if any fixes needed**

```bash
git add -A
git commit -m "fix: address smoke test findings"
```
