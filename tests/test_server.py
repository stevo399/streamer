import time
from unittest.mock import MagicMock

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


# ── Fixtures for API tests (with mock pipeline) ─────────────────────────


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


# ── API tests ────────────────────────────────────────────────────────────


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
