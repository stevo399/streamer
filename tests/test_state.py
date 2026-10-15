import threading

from streamer.state import ServerState


class TestQueue:
    def test_queue_starts_empty(self):
        state = ServerState()
        assert state.queue == []

    def test_queue_add_appends(self):
        state = ServerState()
        state.queue_add("a.mp3")
        state.queue_add("b.mp3")
        assert state.queue == ["a.mp3", "b.mp3"]

    def test_queue_remove_by_index(self):
        state = ServerState()
        state.queue_add("a.mp3")
        state.queue_add("b.mp3")
        state.queue_add("c.mp3")
        assert state.queue_remove(1) is True
        assert state.queue == ["a.mp3", "c.mp3"]

    def test_queue_remove_invalid_index(self):
        state = ServerState()
        state.queue_add("a.mp3")
        assert state.queue_remove(5) is False
        assert state.queue == ["a.mp3"]

    def test_queue_remove_negative_index(self):
        state = ServerState()
        state.queue_add("a.mp3")
        assert state.queue_remove(-1) is False


class TestHistory:
    def test_history_starts_empty(self):
        state = ServerState()
        assert state.history == []

    def test_history_push(self):
        state = ServerState()
        state.history_push("a.mp3")
        state.history_push("b.mp3")
        assert state.history == ["a.mp3", "b.mp3"]

    def test_history_max_100(self):
        state = ServerState()
        for i in range(150):
            state.history_push(f"{i}.mp3")
        assert len(state.history) == 100
        assert state.history[0] == "50.mp3"
        assert state.history[-1] == "149.mp3"


class TestCurrentTrack:
    def test_starts_none(self):
        state = ServerState()
        assert state.current_track is None

    def test_set_and_get(self):
        state = ServerState()
        state.current_track = "test.mp3"
        assert state.current_track == "test.mp3"


class TestDJToggle:
    def test_starts_disabled(self):
        state = ServerState()
        assert state.dj_enabled is False

    def test_toggle_on(self):
        state = ServerState()
        state.dj_enabled = True
        assert state.dj_enabled is True

    def test_toggle_off(self):
        state = ServerState()
        state.dj_enabled = True
        state.dj_enabled = False
        assert state.dj_enabled is False


class TestPauseToggle:
    def test_starts_unpaused(self):
        state = ServerState()
        assert state.paused is False

    def test_pause_on(self):
        state = ServerState()
        state.paused = True
        assert state.paused is True

    def test_pause_off(self):
        state = ServerState()
        state.paused = True
        state.paused = False
        assert state.paused is False


class TestAdvance:
    def test_advance_pops_queue(self):
        state = ServerState()
        state.current_track = "current.mp3"
        state.queue_add("next.mp3")
        result = state.advance()
        assert result == "next.mp3"
        assert state.current_track == "next.mp3"
        assert state.history == ["current.mp3"]

    def test_advance_empty_queue_returns_none(self):
        state = ServerState()
        state.current_track = "current.mp3"
        result = state.advance()
        assert result is None
        assert state.current_track is None
        assert state.history == ["current.mp3"]

    def test_advance_no_current_track(self):
        state = ServerState()
        state.queue_add("next.mp3")
        result = state.advance()
        assert result == "next.mp3"
        assert state.history == []


class TestGoPrevious:
    def test_go_previous_basic(self):
        state = ServerState()
        state.history_push("prev.mp3")
        state.current_track = "current.mp3"
        result = state.go_previous()
        assert result == "prev.mp3"
        assert state.current_track == "prev.mp3"
        assert state.queue == ["current.mp3"]

    def test_go_previous_stacks_queue(self):
        state = ServerState()
        state.history_push("08.mp3")
        state.history_push("09.mp3")
        state.current_track = "10.mp3"

        state.go_previous()
        assert state.current_track == "09.mp3"
        assert state.queue == ["10.mp3"]

        state.go_previous()
        assert state.current_track == "08.mp3"
        assert state.queue == ["09.mp3", "10.mp3"]

    def test_go_previous_no_history_returns_none(self):
        state = ServerState()
        state.current_track = "current.mp3"
        result = state.go_previous()
        assert result is None
        assert state.current_track == "current.mp3"
        assert state.queue == []

    def test_go_previous_preserves_existing_queue(self):
        state = ServerState()
        state.history_push("prev.mp3")
        state.current_track = "current.mp3"
        state.queue_add("queued.mp3")
        state.go_previous()
        assert state.queue == ["current.mp3", "queued.mp3"]


class TestPlayNow:
    def test_play_now_pushes_current_to_history(self):
        state = ServerState()
        state.current_track = "current.mp3"
        state.play_now("new.mp3")
        assert state.current_track == "new.mp3"
        assert state.history == ["current.mp3"]

    def test_play_now_no_current(self):
        state = ServerState()
        state.play_now("new.mp3")
        assert state.current_track == "new.mp3"
        assert state.history == []

    def test_play_now_does_not_affect_queue(self):
        state = ServerState()
        state.current_track = "current.mp3"
        state.queue_add("queued.mp3")
        state.play_now("new.mp3")
        assert state.queue == ["queued.mp3"]


class TestCuratorEnabled:
    def test_starts_disabled(self):
        state = ServerState()
        assert state.curator_enabled is False

    def test_toggle_on(self):
        state = ServerState()
        state.curator_enabled = True
        assert state.curator_enabled is True

    def test_toggle_off(self):
        state = ServerState()
        state.curator_enabled = True
        state.curator_enabled = False
        assert state.curator_enabled is False


class TestCuratorReason:
    def test_starts_none(self):
        state = ServerState()
        assert state.curator_reason is None

    def test_set_and_get(self):
        state = ServerState()
        state.curator_reason = "Marathon: Test Show Season 1"
        assert state.curator_reason == "Marathon: Test Show Season 1"

    def test_clear(self):
        state = ServerState()
        state.curator_reason = "Something"
        state.curator_reason = None
        assert state.curator_reason is None


class TestChangeNotification:
    def test_version_starts_at_zero(self):
        state = ServerState()
        assert state.version == 0

    def test_mutations_bump_version(self):
        state = ServerState()
        start = state.version
        state.queue_add("a.mp3")
        state.paused = True
        state.dj_enabled = True
        assert state.version == start + 3

    def test_failed_queue_remove_does_not_bump(self):
        state = ServerState()
        start = state.version
        assert state.queue_remove(3) is False
        assert state.version == start

    def test_wait_for_change_times_out_unchanged(self):
        state = ServerState()
        assert state.wait_for_change(state.version, timeout=0.05) == state.version

    def test_wait_for_change_wakes_on_mutation(self):
        state = ServerState()
        seen = state.version
        threading.Timer(0.05, lambda: setattr(state, "paused", True)).start()
        assert state.wait_for_change(seen, timeout=2) != seen

    def test_notify_change_bumps_version(self):
        state = ServerState()
        start = state.version
        state.notify_change()
        assert state.version == start + 1


class TestBookMode:
    def test_starts_off(self):
        assert ServerState().book_mode is False

    def test_set_and_get(self):
        state = ServerState()
        state.book_mode = True
        assert state.book_mode is True


class TestLoopCurrent:
    def test_starts_off(self):
        assert ServerState().loop_current is False

    def test_set_and_get(self):
        state = ServerState()
        state.loop_current = True
        assert state.loop_current is True
