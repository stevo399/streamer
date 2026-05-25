from pathlib import Path
from urllib.parse import quote

import bcrypt
from flask import Flask, abort, jsonify, redirect, render_template, request
from flask_httpauth import HTTPBasicAuth

from streamer.config import AUTH_PASSWORD_HASH, AUTH_USERNAME
from streamer.scanner import Scanner
from streamer.state import ServerState

auth = HTTPBasicAuth()


@auth.verify_password
def verify_password(username, password):
    if not AUTH_USERNAME or not AUTH_PASSWORD_HASH:
        return True
    if username == AUTH_USERNAME:
        return bcrypt.checkpw(
            password.encode("utf-8"), AUTH_PASSWORD_HASH.encode("utf-8"),
        )
    return False


def create_app(state=None, scanner=None, pipeline=None):
    app = Flask(__name__)
    app.state = state or ServerState()
    app.scanner = scanner or Scanner()
    app.pipeline = pipeline

    @app.route("/")
    @auth.login_required
    def index():
        current = app.state.current_track
        track_name = Path(current).name if current else "Nothing playing"
        track_path = current or ""
        queue_items = [
            {"name": Path(p).name, "path": p} for p in app.state.queue
        ]
        return render_template(
            "index.html",
            track_name=track_name,
            track_path=track_path,
            queue=queue_items,
            dj_enabled=app.state.dj_enabled,
            curator_enabled=app.state.curator_enabled,
            curator_reason=app.state.curator_reason,
        )

    @app.route("/next", methods=["POST"])
    @auth.login_required
    def next_track():
        if app.pipeline:
            app.pipeline.request_next()
        return redirect("/")

    @app.route("/previous", methods=["POST"])
    @auth.login_required
    def previous_track():
        if app.pipeline:
            app.pipeline.request_previous()
        return redirect("/")

    @app.route("/queue/add", methods=["POST"])
    @auth.login_required
    def queue_add():
        browse_path = request.form.get("file", "")
        resolved = app.scanner.resolve_browse_path(browse_path)
        if resolved and resolved.is_file():
            app.state.queue_add(str(resolved))
        return redirect("/")

    @app.route("/queue/remove", methods=["POST"])
    @auth.login_required
    def queue_remove():
        index = request.form.get("index", type=int)
        if index is not None:
            app.state.queue_remove(index)
        return redirect("/")

    @app.route("/dj/toggle", methods=["POST"])
    @auth.login_required
    def dj_toggle():
        app.state.dj_enabled = not app.state.dj_enabled
        return redirect("/")

    @app.route("/curator/toggle", methods=["POST"])
    @auth.login_required
    def curator_toggle():
        app.state.curator_enabled = not app.state.curator_enabled
        return redirect("/")

    @app.route("/api/state")
    @auth.login_required
    def api_state():
        current = app.state.current_track
        return jsonify({
            "track_name": Path(current).name if current else "Nothing playing",
            "track_path": current or "",
            "queue": [
                {"name": Path(p).name, "path": p}
                for p in app.state.queue
            ],
            "dj_enabled": app.state.dj_enabled,
            "curator_enabled": app.state.curator_enabled,
            "curator_reason": app.state.curator_reason,
        })

    @app.route("/play", methods=["POST"])
    @auth.login_required
    def play():
        browse_path = request.form.get("file", "")
        resolved = app.scanner.resolve_browse_path(browse_path)
        if resolved and resolved.is_file():
            if app.pipeline:
                app.pipeline.request_play(str(resolved))
        return redirect("/")

    @app.route("/browse/play")
    @auth.login_required
    def browse_play():
        browse_path = request.args.get("file", "")
        resolved = app.scanner.resolve_browse_path(browse_path)
        if resolved is None or not resolved.is_file():
            abort(404)
        return render_template(
            "play.html",
            file_name=resolved.name,
            file_path=str(resolved),
            browse_path=browse_path,
        )

    @app.route("/browse/")
    @app.route("/browse/<path:subpath>")
    @auth.login_required
    def browse(subpath=""):
        if not subpath:
            dirs = [
                {"name": root.name, "href": f"/browse/{quote(root.name)}"}
                for root in app.scanner.roots
                if root.exists()
            ]
            return render_template(
                "browse.html", dirs=dirs, files=[], breadcrumbs=[]
            )

        resolved = app.scanner.resolve_browse_path(subpath)
        if resolved is None or not resolved.is_dir():
            abort(404)

        dir_names, file_names = app.scanner.list_directory(resolved)
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

        return render_template(
            "browse.html", dirs=dirs, files=files, breadcrumbs=breadcrumbs
        )

    def _stream_response(codec_args, mimetype):
        import subprocess
        import threading
        import time as _time

        from flask import Response

        def generate():
            if not app.pipeline:
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
                pos = app.pipeline.pcm_buffer.get_current_position()
                while not stop.is_set():
                    data, new_pos = app.pipeline.pcm_buffer.read(pos, max_bytes=4096)
                    if data is None:
                        pos = new_pos
                        continue
                    if not data:
                        _time.sleep(0.02)
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

        return Response(
            generate(),
            mimetype=mimetype,
            headers={"Cache-Control": "no-cache"},
        )

    @app.route("/stream.ogg")
    def stream_ogg():
        import time as _time
        from flask import Response

        def generate():
            if not app.pipeline:
                return
            headers = app.pipeline.ogg_buffer.get_headers()
            if headers:
                yield headers
            pos = app.pipeline.ogg_buffer.get_current_position()
            while True:
                data, new_pos = app.pipeline.ogg_buffer.read(pos, max_bytes=4096)
                if data is None:
                    return
                if not data:
                    _time.sleep(0.02)
                    continue
                pos = new_pos
                yield data

        return Response(
            generate(),
            mimetype="audio/ogg",
            headers={"Cache-Control": "no-cache"},
        )

    @app.route("/stream.mp3")
    def stream_mp3():
        return _stream_response(
            ["-f", "mp3", "-b:a", "128k"],
            "audio/mpeg",
        )

    return app
