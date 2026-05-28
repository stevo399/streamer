import json as json_mod
import queue
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import quote

import bcrypt
from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from streamer.config import AUTH_PASSWORD_HASH, AUTH_USERNAME
from streamer.explorer import ExplorerStatus, explore
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


# ── Response models ──────────────────────────────────────────────────────


class NowPlayingResponse(BaseModel):
    track_name: str
    track_path: str
    elapsed: float | None
    duration: float | None
    remaining: float | None


class OkResponse(BaseModel):
    ok: bool


class TrackOkResponse(BaseModel):
    ok: bool
    track: str | None = None


class QueueItem(BaseModel):
    name: str
    path: str
    index: int


class QueueListResponse(BaseModel):
    queue: list[QueueItem]


class ToggleResponse(BaseModel):
    enabled: bool


class CuratorStatusResponse(BaseModel):
    enabled: bool
    reason: str | None
    tracks_since_check: int
    next_check_at: int


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatHistoryResponse(BaseModel):
    messages: list[ChatMessage]


class ChatResponse(BaseModel):
    response: str
    queued: list[str]


class BrowseEntry(BaseModel):
    name: str
    path: str


class BrowseResponse(BaseModel):
    dirs: list[BrowseEntry]
    files: list[BrowseEntry]


class StateQueueItem(BaseModel):
    name: str
    path: str


class StateResponse(BaseModel):
    track_name: str
    track_path: str
    queue: list[StateQueueItem]
    dj_enabled: bool
    curator_enabled: bool
    curator_reason: str | None
    elapsed: float | None
    duration: float | None
    remaining: float | None
    curator_tracks_since_check: int | None
    curator_next_check_at: int | None


class ExplorerStartBody(BaseModel):
    force: bool = False


class ExplorerStartResponse(BaseModel):
    ok: bool
    total: int | None = None
    error: str | None = None


class ExplorerStatusResponse(BaseModel):
    running: bool
    total: int
    completed: int
    current_show: str
    log: list[dict]
    error: str | None


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

    app = FastAPI(
        title="Streamer",
        description="Personal audio streaming server with AI DJ and Curator.",
        version="1.0.0",
    )
    app.state.server_state = _state
    app.state.scanner = _scanner
    app.state.pipeline = _pipeline
    app.state.explorer_status = ExplorerStatus()

    # ── HTML routes ──────────────────────────────────────────────────────

    @app.get("/", include_in_schema=False)
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

    @app.post("/next", include_in_schema=False)
    def next_track(_user: str = Depends(verify_credentials)):
        if _pipeline:
            _pipeline.request_next()
        return RedirectResponse(url="/", status_code=303)

    @app.post("/previous", include_in_schema=False)
    def previous_track(_user: str = Depends(verify_credentials)):
        if _pipeline:
            _pipeline.request_previous()
        return RedirectResponse(url="/", status_code=303)

    @app.post("/queue/add", include_in_schema=False)
    def queue_add(
        file: str = Form(""),
        _user: str = Depends(verify_credentials),
    ):
        resolved = _scanner.resolve_browse_path(file)
        if resolved and resolved.is_file():
            _state.queue_add(str(resolved))
        return RedirectResponse(url="/", status_code=303)

    @app.post("/queue/remove", include_in_schema=False)
    def queue_remove(
        index: int = Form(...),
        _user: str = Depends(verify_credentials),
    ):
        _state.queue_remove(index)
        return RedirectResponse(url="/", status_code=303)

    @app.post("/dj/toggle", include_in_schema=False)
    def dj_toggle(_user: str = Depends(verify_credentials)):
        _state.dj_enabled = not _state.dj_enabled
        return RedirectResponse(url="/", status_code=303)

    @app.post("/curator/toggle", include_in_schema=False)
    def curator_toggle(_user: str = Depends(verify_credentials)):
        _state.curator_enabled = not _state.curator_enabled
        if _state.curator_enabled and _pipeline:
            _pipeline._curator.trigger()
        return RedirectResponse(url="/", status_code=303)

    @app.post("/play", include_in_schema=False)
    def play_now(
        file: str = Form(""),
        _user: str = Depends(verify_credentials),
    ):
        resolved = _scanner.resolve_browse_path(file)
        if resolved and resolved.is_file():
            if _pipeline:
                _pipeline.request_play(str(resolved))
        return RedirectResponse(url="/", status_code=303)

    @app.get("/browse/play", include_in_schema=False)
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

    @app.get("/browse/", include_in_schema=False)
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

    @app.get("/browse/{subpath:path}", include_in_schema=False)
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

    @app.get("/api/state", tags=["State"], summary="Full server state", response_model=StateResponse)
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

    @app.get("/api/now-playing", tags=["Tracks"], summary="Now playing with timing", response_model=NowPlayingResponse)
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

    @app.post("/api/tracks/next", tags=["Tracks"], summary="Skip to next track", response_model=OkResponse)
    def api_tracks_next(_user: str = Depends(verify_credentials)):
        if _pipeline:
            _pipeline.request_next()
        return {"ok": True}

    @app.post("/api/tracks/previous", tags=["Tracks"], summary="Go to previous track", response_model=TrackOkResponse)
    def api_tracks_previous(_user: str = Depends(verify_credentials)):
        if _pipeline and _pipeline.request_previous():
            return {"ok": True, "track": _state.current_track}
        return {"ok": False}

    @app.post("/api/tracks/play", tags=["Tracks"], summary="Play a specific file", response_model=OkResponse)
    def api_tracks_play(body: PathBody, _user: str = Depends(verify_credentials)):
        resolved = _scanner.resolve_browse_path(body.path)
        if resolved and resolved.is_file():
            if _pipeline:
                _pipeline.request_play(str(resolved))
            return {"ok": True}
        return {"ok": False}

    @app.get("/api/queue", tags=["Queue"], summary="List queued tracks", response_model=QueueListResponse)
    def api_queue_list(_user: str = Depends(verify_credentials)):
        return {
            "queue": [
                {"name": Path(p).name, "path": p, "index": i}
                for i, p in enumerate(_state.queue)
            ],
        }

    @app.post("/api/queue", tags=["Queue"], summary="Add a file to the queue", response_model=OkResponse)
    def api_queue_add(
        body: PathBody,
        _user: str = Depends(verify_credentials),
    ):
        resolved = _scanner.resolve_browse_path(body.path)
        if resolved and resolved.is_file():
            _state.queue_add(str(resolved))
            return {"ok": True}
        return {"ok": False}

    @app.delete("/api/queue/{index}", tags=["Queue"], summary="Remove a track from the queue", response_model=OkResponse)
    def api_queue_remove(
        index: int,
        _user: str = Depends(verify_credentials),
    ):
        if _state.queue_remove(index):
            return {"ok": True}
        return {"ok": False}

    @app.get("/api/dj", tags=["DJ"], summary="Get DJ status", response_model=ToggleResponse)
    def api_dj_status(_user: str = Depends(verify_credentials)):
        return {"enabled": _state.dj_enabled}

    @app.post("/api/dj", tags=["DJ"], summary="Enable or disable the DJ", response_model=ToggleResponse)
    def api_dj_set(
        body: ToggleBody,
        _user: str = Depends(verify_credentials),
    ):
        _state.dj_enabled = body.enabled
        return {"enabled": _state.dj_enabled}

    @app.get("/api/curator", tags=["Curator"], summary="Get curator status and check schedule", response_model=CuratorStatusResponse)
    def api_curator_status(_user: str = Depends(verify_credentials)):
        if _pipeline:
            return _pipeline._curator.get_status()
        return {
            "enabled": _state.curator_enabled,
            "reason": _state.curator_reason,
            "tracks_since_check": 0,
            "next_check_at": 0,
        }

    @app.post("/api/curator", tags=["Curator"], summary="Enable or disable the curator", response_model=ToggleResponse)
    def api_curator_set(
        body: ToggleBody,
        _user: str = Depends(verify_credentials),
    ):
        _state.curator_enabled = body.enabled
        if body.enabled and _pipeline:
            _pipeline._curator.trigger()
        return {"enabled": _state.curator_enabled}

    @app.post("/api/curator/force", tags=["Curator"], summary="Force an immediate curator check", response_model=OkResponse)
    def api_curator_force(_user: str = Depends(verify_credentials)):
        if _pipeline:
            _pipeline._curator.trigger()
        return {"ok": True}

    @app.get("/api/curator/chat", tags=["Curator"], summary="Get chat history with the curator", response_model=ChatHistoryResponse)
    def api_curator_chat_history(_user: str = Depends(verify_credentials)):
        if _pipeline:
            return {"messages": _pipeline._curator.get_chat_history()}
        return {"messages": []}

    @app.post("/api/curator/chat", tags=["Curator"], summary="Send a chat message to the curator", response_model=ChatResponse)
    def api_curator_chat_send(
        body: ChatBody,
        _user: str = Depends(verify_credentials),
    ):
        if not _pipeline:
            return {"response": "Pipeline not available.", "queued": []}
        return _pipeline._curator.chat(body.message)

    @app.get("/api/browse", tags=["Browse"], summary="List media root folders", response_model=BrowseResponse)
    def api_browse_root(_user: str = Depends(verify_credentials)):
        dirs = [
            {"name": root.name, "path": root.name}
            for root in _scanner.roots
            if root.exists()
        ]
        return {"dirs": dirs, "files": []}

    @app.get("/api/browse/{subpath:path}", tags=["Browse"], summary="List directories and audio files at a path", response_model=BrowseResponse)
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

    # ── Explorer ────────────────────────────────────────────────────────

    @app.post(
        "/api/explorer/start",
        tags=["Explorer"],
        summary="Start library exploration",
        response_model=ExplorerStartResponse,
    )
    def api_explorer_start(
        body: ExplorerStartBody = ExplorerStartBody(),
        _user: str = Depends(verify_credentials),
    ):
        explorer_status = app.state.explorer_status
        if explorer_status.running:
            return JSONResponse(
                status_code=409,
                content={"ok": False, "error": "Already running"},
            )

        def run():
            explore(_scanner, explorer_status, force=body.force)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        time.sleep(0.05)

        if explorer_status.error:
            return {"ok": False, "error": explorer_status.error}
        return {"ok": True, "total": explorer_status.total}

    @app.get(
        "/api/explorer/status",
        tags=["Explorer"],
        summary="Get explorer status",
        response_model=ExplorerStatusResponse,
    )
    def api_explorer_status(_user: str = Depends(verify_credentials)):
        return app.state.explorer_status.to_dict()

    @app.get(
        "/api/explorer/progress",
        tags=["Explorer"],
        summary="Stream explorer progress via SSE",
    )
    def api_explorer_progress():
        explorer_status = app.state.explorer_status

        def generate():
            q = explorer_status.subscribe()
            try:
                yield f"data: {json_mod.dumps(explorer_status.to_dict())}\n\n"
                while True:
                    try:
                        event = q.get(timeout=30)
                        yield f"data: {json_mod.dumps(event)}\n\n"
                        if event.get("type") == "finished":
                            return
                    except queue.Empty:
                        yield ": keepalive\n\n"
                    if not explorer_status.running and q.empty():
                        return
            finally:
                explorer_status.unsubscribe(q)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )

    # ── Streaming ────────────────────────────────────────────────────────

    @app.get("/stream.ogg", include_in_schema=False)
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

    @app.get("/stream.mp3", include_in_schema=False)
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
