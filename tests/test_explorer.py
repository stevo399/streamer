import queue
from pathlib import Path
from unittest.mock import MagicMock, patch

from streamer.explorer import ExplorerStatus, _collect_shows
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
