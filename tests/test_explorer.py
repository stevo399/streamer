import queue
from pathlib import Path
from unittest.mock import MagicMock, patch

from streamer.explorer import ExplorerStatus, _collect_shows, explore
from streamer.scanner import Scanner


class TestExplorerStatus:
    def test_initial_state(self):
        status = ExplorerStatus()
        assert status.running is False
        assert status.total == 0
        assert status.completed == 0
        assert status.current_show == ""
        assert status.log == []
        assert status.error is None

    def test_push_event_appends_to_log(self):
        status = ExplorerStatus()
        status.push_event({"type": "exploring", "show": "Family Guy"})
        assert len(status.log) == 1
        assert status.log[0]["show"] == "Family Guy"

    def test_push_event_caps_log_at_100(self):
        status = ExplorerStatus()
        for i in range(120):
            status.push_event({"type": "exploring", "show": f"Show {i}"})
        assert len(status.log) == 100
        assert status.log[0]["show"] == "Show 20"

    def test_subscribe_and_receive_events(self):
        status = ExplorerStatus()
        q = status.subscribe()
        status.push_event({"type": "exploring", "show": "Test"})
        event = q.get(timeout=1)
        assert event["show"] == "Test"

    def test_unsubscribe_stops_events(self):
        status = ExplorerStatus()
        q = status.subscribe()
        status.unsubscribe(q)
        status.push_event({"type": "exploring", "show": "Test"})
        assert q.empty()

    def test_to_dict(self):
        status = ExplorerStatus()
        status.running = True
        status.total = 10
        status.completed = 3
        status.current_show = "Seinfeld"
        d = status.to_dict()
        assert d == {
            "running": True,
            "total": 10,
            "completed": 3,
            "current_show": "Seinfeld",
            "log": [],
            "error": None,
        }


class TestCollectShows:
    def test_finds_entertainment_shows(self, tmp_path):
        ent = tmp_path / "entertainment"
        show1 = ent / "Family Guy" / "season 01"
        show1.mkdir(parents=True)
        (show1 / "01.mp3").write_bytes(b"")
        show2 = ent / "Seinfeld" / "season 01"
        show2.mkdir(parents=True)
        (show2 / "01.mp3").write_bytes(b"")

        scanner = Scanner(roots=[ent])
        shows = _collect_shows(scanner)
        names = {s["name"] for s in shows}
        assert "Family Guy" in names
        assert "Seinfeld" in names

    def test_finds_podcast_shows(self, tmp_path):
        pod = tmp_path / "Podcast"
        show = pod / "My Podcast"
        show.mkdir(parents=True)
        (show / "ep01.mp3").write_bytes(b"")

        scanner = Scanner(roots=[pod])
        shows = _collect_shows(scanner)
        assert len(shows) == 1
        assert shows[0]["name"] == "My Podcast"

    def test_collects_season_and_episode_info(self, tmp_path):
        ent = tmp_path / "entertainment"
        s1 = ent / "Show" / "season 01"
        s1.mkdir(parents=True)
        (s1 / "01.mp3").write_bytes(b"")
        (s1 / "02.mp3").write_bytes(b"")
        s2 = ent / "Show" / "season 02"
        s2.mkdir(parents=True)
        (s2 / "01.mp3").write_bytes(b"")

        scanner = Scanner(roots=[ent])
        shows = _collect_shows(scanner)
        assert len(shows) == 1
        show = shows[0]
        assert "season 01" in show["seasons"]
        assert "season 02" in show["seasons"]
        assert len(show["seasons"]["season 01"]) == 2
        assert len(show["seasons"]["season 02"]) == 1

    def test_skips_empty_show_dirs(self, tmp_path):
        ent = tmp_path / "entertainment"
        (ent / "EmptyShow").mkdir(parents=True)
        scanner = Scanner(roots=[ent])
        shows = _collect_shows(scanner)
        assert len(shows) == 0


class TestExplore:
    @patch("streamer.explorer.GEMINI_API_KEY", "fake-key")
    @patch("streamer.explorer.genai")
    def test_explore_writes_show_md(self, mock_genai, tmp_path):
        ent = tmp_path / "entertainment"
        s1 = ent / "Family Guy" / "season 01"
        s1.mkdir(parents=True)
        (s1 / "01.mp3").write_bytes(b"")

        notes_dir = tmp_path / "notes"
        notes_dir.mkdir()

        mock_response = MagicMock()
        mock_response.text = "Family Guy is an animated sitcom."
        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = mock_response
        mock_genai.Client.return_value = mock_client

        scanner = Scanner(roots=[ent])
        status = ExplorerStatus()

        with patch("streamer.explorer.NOTES_DIR", notes_dir):
            explore(scanner, status)

        show_md = notes_dir / "Family Guy" / "show.md"
        assert show_md.exists()
        assert "animated sitcom" in show_md.read_text()
        assert status.completed == 1
        assert status.running is False

    @patch("streamer.explorer.GEMINI_API_KEY", "fake-key")
    @patch("streamer.explorer.genai")
    def test_explore_skips_existing(self, mock_genai, tmp_path):
        ent = tmp_path / "entertainment"
        s1 = ent / "Family Guy" / "season 01"
        s1.mkdir(parents=True)
        (s1 / "01.mp3").write_bytes(b"")

        notes_dir = tmp_path / "notes"
        show_notes = notes_dir / "Family Guy"
        show_notes.mkdir(parents=True)
        (show_notes / "show.md").write_text("existing notes")

        scanner = Scanner(roots=[ent])
        status = ExplorerStatus()

        with patch("streamer.explorer.NOTES_DIR", notes_dir):
            explore(scanner, status)

        assert (show_notes / "show.md").read_text() == "existing notes"
        mock_genai.Client.assert_not_called()
        assert status.total == 0

    @patch("streamer.explorer.GEMINI_API_KEY", "fake-key")
    @patch("streamer.explorer.genai")
    def test_explore_force_overwrites(self, mock_genai, tmp_path):
        ent = tmp_path / "entertainment"
        s1 = ent / "Family Guy" / "season 01"
        s1.mkdir(parents=True)
        (s1 / "01.mp3").write_bytes(b"")

        notes_dir = tmp_path / "notes"
        show_notes = notes_dir / "Family Guy"
        show_notes.mkdir(parents=True)
        (show_notes / "show.md").write_text("old notes")

        mock_response = MagicMock()
        mock_response.text = "New notes content."
        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = mock_response
        mock_genai.Client.return_value = mock_client

        scanner = Scanner(roots=[ent])
        status = ExplorerStatus()

        with patch("streamer.explorer.NOTES_DIR", notes_dir):
            explore(scanner, status, force=True)

        assert "New notes" in (show_notes / "show.md").read_text()

    @patch("streamer.explorer.GEMINI_API_KEY", "")
    def test_explore_fails_without_api_key(self, tmp_path):
        notes_dir = tmp_path / "notes"
        notes_dir.mkdir()
        scanner = Scanner(roots=[])
        status = ExplorerStatus()

        with patch("streamer.explorer.NOTES_DIR", notes_dir):
            explore(scanner, status)

        assert status.error is not None
        assert "API key" in status.error

    def test_explore_fails_without_notes_dir(self):
        scanner = Scanner(roots=[])
        status = ExplorerStatus()

        with patch("streamer.explorer.NOTES_DIR", None):
            explore(scanner, status)

        assert status.error is not None
        assert "NOTES_DIR" in status.error

    @patch("streamer.explorer.GEMINI_API_KEY", "fake-key")
    @patch("streamer.explorer.genai")
    def test_explore_continues_after_single_show_failure(self, mock_genai, tmp_path):
        ent = tmp_path / "entertainment"
        for name in ["ShowA", "ShowB"]:
            s = ent / name / "season 01"
            s.mkdir(parents=True)
            (s / "01.mp3").write_bytes(b"")

        notes_dir = tmp_path / "notes"
        notes_dir.mkdir()

        call_count = [0]

        def generate_side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise Exception("API error")
            mock_resp = MagicMock()
            mock_resp.text = "Description of show."
            return mock_resp

        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = generate_side_effect
        mock_genai.Client.return_value = mock_client

        scanner = Scanner(roots=[ent])
        status = ExplorerStatus()

        with patch("streamer.explorer.NOTES_DIR", notes_dir):
            explore(scanner, status)

        assert status.completed == 1
        assert status.running is False

    @patch("streamer.explorer.GEMINI_API_KEY", "fake-key")
    @patch("streamer.explorer.genai")
    def test_explore_publishes_events(self, mock_genai, tmp_path):
        ent = tmp_path / "entertainment"
        s1 = ent / "TestShow" / "season 01"
        s1.mkdir(parents=True)
        (s1 / "01.mp3").write_bytes(b"")

        notes_dir = tmp_path / "notes"
        notes_dir.mkdir()

        mock_response = MagicMock()
        mock_response.text = "A test show."
        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = mock_response
        mock_genai.Client.return_value = mock_client

        scanner = Scanner(roots=[ent])
        status = ExplorerStatus()
        q = status.subscribe()

        with patch("streamer.explorer.NOTES_DIR", notes_dir):
            explore(scanner, status)

        events = []
        while not q.empty():
            events.append(q.get_nowait())

        types_seen = {e["type"] for e in events}
        assert "exploring" in types_seen
        assert "completed" in types_seen
        assert "finished" in types_seen
