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
        return _templates.TemplateResponse(request, "index.html", {
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
        return _templates.TemplateResponse(request, "play.html", {
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
        return _templates.TemplateResponse(request, "browse.html", {
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

        return _templates.TemplateResponse(request, "browse.html", {
            "dirs": dirs,
            "files": files,
            "breadcrumbs": breadcrumbs,
        })

    # ── Legacy API ───────────────────────────────────────────────────────

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
    def api_tracks_play(body: PathBody, _user: str = Depends(verify_credentials)):
        resolved = _scanner.resolve_browse_path(body.path)
        if resolved and resolved.is_file():
            if _pipeline:
                _pipeline.request_play(str(resolved))
            return {"ok": True}
        return {"ok": False}

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

    @app.get("/api/dj")
    def api_dj_status(_user: str = Depends(verify_credentials)):
        return {"enabled": _state.dj_enabled}

    @app.post("/api/dj")
    def api_dj_set(
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
    def api_curator_set(
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
