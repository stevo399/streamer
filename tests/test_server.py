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
