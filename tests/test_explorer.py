import queue
from pathlib import Path
from unittest.mock import MagicMock, patch

from streamer.explorer import ExplorerStatus


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
