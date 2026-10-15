import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from streamer.pipeline import AudioPipeline, RingBuffer
from streamer.scanner import Scanner
from streamer.server import _pcm_chunks, _state_events, create_app
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

    def test_pause_redirects(self, client):
        resp = client.post("/pause", follow_redirects=False)
        assert resp.status_code == 303

    def test_resume_redirects(self, client):
        resp = client.post("/resume", follow_redirects=False)
        assert resp.status_code == 303

    def test_seek_redirects(self, client):
        resp = client.post(
            "/seek",
            data={"position": "12.5"},
            follow_redirects=False,
        )
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
        "paused": False,
    }
    pipeline.get_chapters = MagicMock(return_value=[])
    pipeline.request_next = MagicMock()
    pipeline.request_previous = MagicMock(return_value=True)
    pipeline.request_play = MagicMock()
    pipeline.request_pause = MagicMock(return_value=True)
    pipeline.request_resume = MagicMock(return_value=True)
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
    pipeline.request_seek = MagicMock(return_value=True)
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

    def test_seek(self, api_client, mock_pipeline):
        resp = api_client.post(
            "/api/tracks/seek",
            json={"position": 42.5},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert resp.json()["position"] == 42.5
        mock_pipeline.request_seek.assert_called_once_with(42.5)

    def test_seek_invalid_when_pipeline_missing(self, client):
        resp = client.post(
            "/api/tracks/seek",
            json={"position": 10.0},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is False

    def test_pause(self, api_client, mock_pipeline):
        resp = api_client.post("/api/playback/pause")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        mock_pipeline.request_pause.assert_called_once()

    def test_resume(self, api_client, mock_pipeline):
        resp = api_client.post("/api/playback/resume")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        mock_pipeline.request_resume.assert_called_once()

    def test_pause_invalid_when_pipeline_missing(self, client):
        resp = client.post("/api/playback/pause")
        assert resp.status_code == 200
        assert resp.json()["ok"] is False

    def test_resume_invalid_when_pipeline_missing(self, client):
        resp = client.post("/api/playback/resume")
        assert resp.status_code == 200
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

    def test_state_includes_paused_flag(self, api_client):
        resp = api_client.get("/api/state")
        data = resp.json()
        assert "paused" in data
        assert data["paused"] is False

    def test_state_reflects_paused_from_state(self, client, app):
        app.state.server_state.paused = True
        resp = client.get("/api/state")
        data = resp.json()
        assert data["paused"] is True


class TestControlPanelUpdates:
    def test_has_timing_element(self, client):
        resp = client.get("/")
        assert 'id="track-timing"' in resp.text

    def test_has_seek_control(self, client):
        resp = client.get("/")
        # UI now exposes a seek amount selector and back/forward buttons
        assert 'id="seek-amount"' in resp.text
        assert 'id="seek-back"' in resp.text
        assert 'id="seek-forward"' in resp.text

    def test_has_pause_controls(self, client):
        resp = client.get("/")
        # single toggle button replaces separate pause/resume forms
        assert 'id="pause-toggle"' in resp.text
        assert 'id="pause-state"' in resp.text
        # ensure previous/next controls exist
        assert 'id="previous-button"' in resp.text
        assert 'id="next-button"' in resp.text

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


class TestExplorerAPI:
    def test_start_returns_error_without_notes_dir(self, api_client):
        with patch("streamer.explorer.NOTES_DIR", None):
            resp = api_client.post("/api/explorer/start", json={})
        data = resp.json()
        assert data["ok"] is False
        assert "NOTES_DIR" in data["error"]

    def test_start_begins_exploration(self, api_client, test_media_dir, tmp_path):
        notes_dir = tmp_path / "notes"
        notes_dir.mkdir()
        with patch("streamer.explorer.NOTES_DIR", notes_dir), \
             patch("streamer.explorer.GEMINI_API_KEY", "fake-key"), \
             patch("streamer.explorer.genai") as mock_genai:
            mock_response = MagicMock()
            mock_response.text = "Description."
            mock_client = MagicMock()
            mock_client.models.generate_content.return_value = mock_response
            mock_genai.Client.return_value = mock_client

            resp = api_client.post("/api/explorer/start", json={})

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True

    def test_start_returns_conflict_when_running(self, api_client, app_with_pipeline):
        app_with_pipeline.state.explorer_status.running = True
        resp = api_client.post("/api/explorer/start", json={})
        assert resp.status_code == 409
        data = resp.json()
        assert data["ok"] is False
        app_with_pipeline.state.explorer_status.running = False

    def test_status_returns_current_state(self, api_client):
        resp = api_client.get("/api/explorer/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "running" in data
        assert "total" in data
        assert "completed" in data

    def test_progress_returns_sse_stream(self, api_client):
        with api_client.stream("GET", "/api/explorer/progress") as resp:
            assert resp.status_code == 200
            assert "text/event-stream" in resp.headers["content-type"]


class TestStateEvents:
    def test_first_event_is_full_state(self):
        state = ServerState()
        events = _state_events(state, lambda: {"paused": state.paused})
        assert next(events) == 'data: {"paused": false}\n\n'

    def test_emits_again_after_change(self):
        state = ServerState()
        events = _state_events(state, lambda: {"paused": state.paused})
        next(events)
        state.paused = True
        assert next(events) == 'data: {"paused": true}\n\n'

    def test_heartbeat_repeats_without_change(self):
        state = ServerState()
        events = _state_events(state, lambda: {"n": 1}, heartbeat=0.05)
        assert next(events) == next(events)

    def test_events_endpoint_is_documented(self, api_client):
        paths = api_client.get("/openapi.json").json()["paths"]
        assert "get" in paths["/api/events"]


class TestExplorerUI:
    def test_explorer_section_present(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "Library Explorer" in resp.text
        assert 'id="explorer-start"' in resp.text
        assert 'id="explorer-force"' in resp.text

    def test_explorer_section_has_accessible_structure(self, client):
        resp = client.get("/")
        assert 'aria-label="Library Explorer"' in resp.text
        assert 'id="explorer-log"' in resp.text

    def test_explorer_progress_initially_hidden(self, client):
        resp = client.get("/")
        assert 'id="explorer-progress" hidden' in resp.text


class TestRealtimeControlPanel:
    def test_subscribes_to_event_stream(self, client):
        resp = client.get("/")
        assert 'new EventSource("/api/events")' in resp.text

    def test_forms_declare_json_api_fallback(self, client):
        resp = client.get("/")
        assert 'formaction="/next"' in resp.text
        assert 'formaction="/previous"' in resp.text
        assert 'id="transport-form"' in resp.text

    def test_pause_toggle_label_follows_state(self, client, app):
        assert "Pause Stream" in client.get("/").text
        app.state.server_state.paused = True
        assert "Resume Stream" in client.get("/").text

    def test_has_seek_position_controls(self, client):
        resp = client.get("/")
        assert 'id="seek-position"' in resp.text
        assert 'for="seek-position"' in resp.text
        assert 'id="seek-time"' in resp.text
        assert 'id="action-status"' in resp.text


class TestPlayPageActions:
    def test_actions_use_json_api(self, client):
        resp = client.get(
            "/browse/play?file=entertainment/Test Show/season 01/01.mp3"
        )
        assert 'data-api="/api/tracks/play"' in resp.text
        assert 'data-api="/api/queue"' in resp.text
        assert 'id="action-status"' in resp.text


class TestBookModeAPI:
    def test_status_defaults_off(self, api_client):
        assert api_client.get("/api/book-mode").json() == {"enabled": False}

    def test_enable_and_disable(self, api_client, app_with_pipeline):
        resp = api_client.post("/api/book-mode", json={"enabled": True})
        assert resp.json() == {"enabled": True}
        assert app_with_pipeline.state.server_state.book_mode is True
        resp = api_client.post("/api/book-mode", json={"enabled": False})
        assert resp.json() == {"enabled": False}

    def test_state_includes_book_mode(self, api_client, app_with_pipeline):
        app_with_pipeline.state.server_state.book_mode = True
        assert api_client.get("/api/state").json()["book_mode"] is True

    def test_html_toggle_redirects(self, client, app):
        resp = client.post("/book/toggle", follow_redirects=False)
        assert resp.status_code == 303
        assert app.state.server_state.book_mode is True

    def test_panel_has_book_mode_section(self, client):
        resp = client.get("/")
        assert 'id="book-toggle"' in resp.text
        assert "Book reading mode" in resp.text


class TestQueueFolder:
    FOLDER = "entertainment/Test Show/season 01"

    def test_queues_all_files_in_order(self, api_client, app_with_pipeline, test_media_dir):
        resp = api_client.post("/api/queue/folder", json={"path": self.FOLDER})
        assert resp.json() == {"ok": True, "added": 3}
        queued = [p.rsplit("\\", 1)[-1].rsplit("/", 1)[-1] for p in app_with_pipeline.state.server_state.queue]
        assert queued == ["01.mp3", "02.mp3", "03.mp3"]

    def test_play_first_plays_first_and_queues_rest(self, api_client, app_with_pipeline, mock_pipeline):
        resp = api_client.post(
            "/api/queue/folder", json={"path": self.FOLDER, "play_first": True},
        )
        assert resp.json() == {"ok": True, "added": 3}
        mock_pipeline.request_play.assert_called_once()
        assert len(app_with_pipeline.state.server_state.queue) == 2

    def test_invalid_folder(self, api_client):
        resp = api_client.post("/api/queue/folder", json={"path": "nope/missing"})
        assert resp.json() == {"ok": False, "added": 0}

    def test_html_form_redirects(self, client, app):
        resp = client.post(
            "/queue/folder", data={"folder": self.FOLDER, "play_first": ""},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert len(app.state.server_state.queue) == 3

    def test_browse_page_offers_folder_actions(self, client):
        resp = client.get(f"/browse/{self.FOLDER}")
        assert 'id="folder-form"' in resp.text
        assert "Play Folder" in resp.text


class TestChaptersAPI:
    def test_chapters_empty_without_pipeline(self, client):
        assert client.get("/api/chapters").json() == {"chapters": []}

    def test_chapters_from_pipeline(self, api_client, mock_pipeline):
        mock_pipeline.get_chapters.return_value = [{"title": "Intro", "start": 0.0}]
        assert api_client.get("/api/chapters").json() == {
            "chapters": [{"title": "Intro", "start": 0.0}],
        }

    def test_state_reports_chapter_fields(self, api_client):
        data = api_client.get("/api/state").json()
        assert data["chapter"] is None
        assert data["chapter_count"] == 0

    def test_panel_has_chapter_select(self, client):
        assert 'id="chapter-select"' in client.get("/").text


class TestLoopAPI:
    def test_status_defaults_off(self, api_client):
        assert api_client.get("/api/loop").json() == {"enabled": False}

    def test_enable_and_disable(self, api_client, app_with_pipeline):
        assert api_client.post("/api/loop", json={"enabled": True}).json() == {"enabled": True}
        assert app_with_pipeline.state.server_state.loop_current is True
        assert api_client.post("/api/loop", json={"enabled": False}).json() == {"enabled": False}

    def test_state_includes_loop(self, api_client, app_with_pipeline):
        app_with_pipeline.state.server_state.loop_current = True
        assert api_client.get("/api/state").json()["loop_current"] is True

    def test_html_toggle_redirects(self, client, app):
        resp = client.post("/loop/toggle", follow_redirects=False)
        assert resp.status_code == 303
        assert app.state.server_state.loop_current is True


class TestPanelLayoutAndShortcuts:
    def test_pause_toggle_sits_between_previous_and_next(self, client):
        text = client.get("/").text
        assert text.index('id="previous-button"') < text.index('id="pause-toggle"') < text.index('id="next-button"')

    def test_toggles_share_one_options_section(self, client):
        text = client.get("/").text
        assert 'aria-label="Options"' in text
        for label in ("Book reading mode", "AI DJ", "AI Curator", "Loop current track"):
            assert label in text
        assert 'aria-label="AI DJ"' not in text
        assert 'aria-label="Book reading mode"' not in text

    def test_has_loop_toggle(self, client, app):
        assert 'id="loop-toggle"' in client.get("/").text
        app.state.server_state.loop_current = True
        assert "Turn off" in client.get("/").text

    def test_has_shortcuts_dialog(self, client):
        text = client.get("/").text
        assert '<dialog id="shortcuts-dialog"' in text
        assert 'aria-labelledby="shortcuts-title"' in text
        assert 'id="shortcuts-open"' in text
        assert "Shift+N" in text

    def test_has_live_announcement_region(self, client):
        assert 'id="announce"' in client.get("/").text


class TestPcmStream:
    def test_yields_data_written_after_connect(self):
        buf = RingBuffer(size=1024)
        chunks = _pcm_chunks(buf)
        buf.write(b"\x01\x02\x03\x04" * 4)
        assert next(chunks) == b"\x01\x02\x03\x04" * 4

    def test_does_not_replay_old_audio(self):
        buf = RingBuffer(size=1024)
        buf.write(b"\x00" * 64)
        chunks = _pcm_chunks(buf)
        buf.write(b"\x07\x07\x07\x07")
        assert next(chunks) == b"\x07\x07\x07\x07"

    def test_starts_on_frame_boundary(self):
        buf = RingBuffer(size=1024)
        buf.write(b"\x00" * 6)
        chunks = _pcm_chunks(buf)
        buf.write(b"\x01\x02")
        assert len(next(chunks)) % 4 == 0

    def test_recovers_when_reader_is_lapped(self):
        buf = RingBuffer(size=64)
        chunks = _pcm_chunks(buf)
        buf.write(b"\x09" * 200)
        data = next(chunks)
        assert data and len(data) % 4 == 0

    def test_endpoint_without_pipeline_is_empty(self, client):
        resp = client.get("/stream.pcm")
        assert resp.status_code == 200
        assert resp.content == b""

    def test_listen_page(self, client):
        resp = client.get("/listen")
        assert resp.status_code == 200
        assert 'id="listen-toggle"' in resp.text
        assert 'id="latency"' in resp.text
        assert "/stream.pcm" in resp.text

    def test_panel_links_to_listener(self, client):
        assert 'href="/listen"' in client.get("/").text


class TestSingleFormGroups:
    def test_transport_buttons_share_one_form(self, client):
        text = client.get("/").text
        start = text.index('id="transport-form"')
        end = text.index("</form>", start)
        group = text[start:end]
        for button in ("previous-button", "pause-toggle", "next-button"):
            assert f'id="{button}"' in group

    def test_option_toggles_share_one_form(self, client):
        text = client.get("/").text
        start = text.index('id="options-form"')
        end = text.index("</form>", start)
        group = text[start:end]
        for button in ("loop-toggle", "book-toggle", "dj-toggle", "curator-toggle"):
            assert f'id="{button}"' in group

    def test_shortcut_list_uses_colon_separators(self, client):
        text = client.get("/").text
        assert "<kbd>K</kbd>: pause or resume" in text
        assert "<kbd>Shift+N</kbd>: next track" in text
