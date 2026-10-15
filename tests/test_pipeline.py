import subprocess
import time
from unittest.mock import MagicMock, patch

from streamer.pipeline import AudioPipeline, BYTES_PER_SECOND, RingBuffer, _parse_ogg_pages
from streamer.scanner import Scanner
from streamer.state import ServerState


class TestRingBuffer:
    def test_write_and_read(self):
        buf = RingBuffer(size=1024)
        buf.write(b"hello")
        pos = 0
        data, new_pos = buf.read(pos)
        assert data == b"hello"
        assert new_pos == 5

    def test_read_at_current_position_returns_empty(self):
        buf = RingBuffer(size=1024)
        buf.write(b"hello")
        data, pos = buf.read(5)
        assert data == b""
        assert pos == 5

    def test_read_with_max_bytes(self):
        buf = RingBuffer(size=1024)
        buf.write(b"hello world")
        data, pos = buf.read(0, max_bytes=5)
        assert data == b"hello"
        assert pos == 5

    def test_wraparound_write(self):
        buf = RingBuffer(size=16)
        buf.write(b"A" * 12)
        buf.write(b"B" * 8)
        data, pos = buf.read(4)
        assert len(data) == 16
        assert data == b"A" * 8 + b"B" * 8

    def test_lapped_reader_returns_none(self):
        buf = RingBuffer(size=16)
        buf.write(b"A" * 20)
        data, pos = buf.read(0)
        assert data is None

    def test_multiple_readers(self):
        buf = RingBuffer(size=1024)
        buf.write(b"hello")
        data1, pos1 = buf.read(0)
        data2, pos2 = buf.read(0)
        assert data1 == data2 == b"hello"
        assert pos1 == pos2 == 5

    def test_get_current_position(self):
        buf = RingBuffer(size=1024)
        assert buf.get_current_position() == 0
        buf.write(b"hello")
        assert buf.get_current_position() == 5

    def test_headers(self):
        buf = RingBuffer(size=1024)
        assert buf.get_headers() == b""
        buf.set_headers(b"OggS_header_data")
        assert buf.get_headers() == b"OggS_header_data"


class TestParseOggPages:
    def _make_page(self, payload: bytes) -> bytes:
        n_segs = (len(payload) + 254) // 255
        seg_table = bytes([255] * (n_segs - 1) + [len(payload) % 255 or 255])
        seg_table = seg_table[:n_segs]
        header = (
            b'OggS'          # capture
            + b'\x00'        # version
            + b'\x00'        # header_type
            + b'\x00' * 8   # granule_position
            + b'\x00' * 4   # serial
            + b'\x00' * 4   # sequence
            + b'\x00' * 4   # CRC
            + bytes([n_segs])
            + seg_table
        )
        return header + payload

    def test_empty_data(self):
        assert _parse_ogg_pages(b"") == []

    def test_single_complete_page(self):
        page = self._make_page(b"hello")
        pages = _parse_ogg_pages(page)
        assert len(pages) == 1
        assert pages[0] == (0, len(page))

    def test_two_complete_pages(self):
        p1 = self._make_page(b"first")
        p2 = self._make_page(b"second")
        pages = _parse_ogg_pages(p1 + p2)
        assert len(pages) == 2
        assert pages[1][0] == len(p1)

    def test_incomplete_page_not_returned(self):
        page = self._make_page(b"hello")
        pages = _parse_ogg_pages(page[:-1])
        assert pages == []

    def test_non_oggs_start_returns_empty(self):
        assert _parse_ogg_pages(b"garbage data") == []


class TestPlaybackInfo:
    def test_get_playback_info_initial(self):
        state = ServerState()
        scanner = MagicMock()
        pipeline = AudioPipeline(state, scanner)
        info = pipeline.get_playback_info()
        assert info["elapsed"] == 0.0
        assert info["duration"] is None
        assert info["remaining"] is None

    def test_get_playback_info_with_duration(self):
        state = ServerState()
        scanner = MagicMock()
        pipeline = AudioPipeline(state, scanner)
        pipeline._track_duration = 180.0
        pipeline._track_bytes_written = BYTES_PER_SECOND * 30
        info = pipeline.get_playback_info()
        assert info["elapsed"] == 30.0
        assert info["duration"] == 180.0
        assert info["remaining"] == 150.0

    def test_get_playback_info_with_seek_offset(self):
        state = ServerState()
        scanner = MagicMock()
        pipeline = AudioPipeline(state, scanner)
        pipeline._track_duration = 120.0
        pipeline._track_offset_bytes = BYTES_PER_SECOND * 12
        pipeline._track_bytes_written = BYTES_PER_SECOND * 5
        info = pipeline.get_playback_info()
        assert info["elapsed"] == 17.0
        assert info["duration"] == 120.0
        assert info["remaining"] == 103.0

    def test_probe_duration_returns_float(self, test_media_dir):
        state = ServerState()
        scanner = MagicMock()
        pipeline = AudioPipeline(state, scanner)
        path = str(
            test_media_dir / "entertainment" / "Test Show" / "season 01" / "01.mp3"
        )
        duration = pipeline._probe_duration(path)
        assert duration is not None
        assert isinstance(duration, float)
        assert duration > 0

    def test_probe_duration_returns_none_for_bad_file(self):
        state = ServerState()
        scanner = MagicMock()
        pipeline = AudioPipeline(state, scanner)
        duration = pipeline._probe_duration("/nonexistent/file.mp3")
        assert duration is None


class TestAudioPipeline:
    def test_start_decoder_uses_seek_flag(self):
        state = ServerState()
        scanner = MagicMock()
        pipeline = AudioPipeline(state, scanner)

        with patch("streamer.pipeline.subprocess.Popen") as mock_popen:
            pipeline._start_decoder("track.mp3", seek_position=12.5)

        cmd = mock_popen.call_args.args[0]
        assert "-ss" in cmd
        assert cmd[cmd.index("-ss") + 1] == "12.5"

    def test_pipeline_produces_pcm_data(self, test_media_dir):
        state = ServerState()
        scanner = Scanner(roots=[
            test_media_dir / "entertainment",
            test_media_dir / "Podcast",
        ])
        pipeline = AudioPipeline(state, scanner)
        try:
            pipeline.start()

            for _ in range(12):
                if pipeline.ogg_buffer.get_current_position() > 0:
                    break
                time.sleep(0.5)

            assert state.current_track is not None
            assert pipeline.pcm_buffer.get_current_position() > 0
            assert pipeline.ogg_buffer.get_current_position() > 0
            assert pipeline.ogg_buffer.get_headers()[:4] == b'OggS'
        finally:
            pipeline.stop()

    def test_pipeline_request_next(self, test_media_dir):
        state = ServerState()
        scanner = Scanner(roots=[
            test_media_dir / "entertainment",
            test_media_dir / "Podcast",
        ])
        pipeline = AudioPipeline(state, scanner)
        try:
            pipeline.start()
            time.sleep(1)

            first_track = state.current_track
            pipeline.request_next()
            time.sleep(0.5)
            assert state.current_track is not None
            assert any(first_track == h for h in state.history)
        finally:
            pipeline.stop()

    def test_request_pause_and_resume(self):
        state = ServerState()
        scanner = MagicMock()
        pipeline = AudioPipeline(state, scanner)

        assert pipeline.request_pause() is False
        assert pipeline.request_resume() is False

        state.current_track = "track.mp3"
        assert pipeline.request_pause() is True
        assert state.paused is True

        assert pipeline.request_resume() is True
        assert state.paused is False

    def test_duration_probe_notifies_state_change(self):
        state = ServerState()
        pipeline = AudioPipeline(state, MagicMock())
        state.current_track = "track.mp3"
        pipeline._current_decoder = MagicMock()
        version = state.version

        with patch.object(pipeline, "_probe_duration", return_value=42.0):
            pipeline._probe_duration_async("track.mp3")

        assert pipeline._track_duration == 42.0
        assert state.version != version


class TestBookMode:
    def _book(self, tmp_path):
        book = tmp_path / "Book"
        book.mkdir()
        for name in ["chapter 1.mp3", "chapter 2.mp3", "chapter 10.mp3"]:
            (book / name).write_bytes(b"")
        return book

    def test_continues_through_folder_in_order(self, tmp_path):
        book = self._book(tmp_path)
        state = ServerState()
        state.book_mode = True
        state.current_track = str(book / "chapter 2.mp3")
        pipeline = AudioPipeline(state, Scanner(roots=[tmp_path]))

        track, seek, resume = pipeline._get_next_track()

        assert track == str(book / "chapter 10.mp3")
        assert (seek, resume) == (None, False)
        assert state.current_track == track

    def test_queue_takes_priority_over_book_order(self, tmp_path):
        book = self._book(tmp_path)
        state = ServerState()
        state.book_mode = True
        state.current_track = str(book / "chapter 1.mp3")
        state.queue_add("queued.mp3")
        pipeline = AudioPipeline(state, Scanner(roots=[tmp_path]))

        assert pipeline._get_next_track()[0] == "queued.mp3"

    def test_end_of_book_falls_back_to_shuffle(self, tmp_path):
        book = self._book(tmp_path)
        other = tmp_path / "Music"
        other.mkdir()
        (other / "song.mp3").write_bytes(b"")
        state = ServerState()
        state.book_mode = True
        state.current_track = str(book / "chapter 10.mp3")
        pipeline = AudioPipeline(state, Scanner(roots=[tmp_path]))

        track, _, _ = pipeline._get_next_track()

        assert state.book_mode is False
        assert track.endswith(".mp3")

    def test_shuffle_when_book_mode_off(self, tmp_path):
        book = self._book(tmp_path)
        state = ServerState()
        state.current_track = str(book / "chapter 1.mp3")
        scanner = MagicMock()
        scanner.pick_random.return_value = book / "chapter 10.mp3"
        pipeline = AudioPipeline(state, scanner)

        pipeline._get_next_track()

        scanner.pick_random.assert_called_once()
        scanner.next_in_folder.assert_not_called()


class TestChapters:
    CHAPTERS = [
        {"title": "Intro", "start": 0.0},
        {"title": "Part One", "start": 60.0},
        {"title": "Part Two", "start": 120.0},
    ]

    def test_probe_chapters_parses_ffprobe_json(self):
        pipeline = AudioPipeline(ServerState(), MagicMock())
        output = (
            '{"chapters": [{"start_time": "0.000000", "tags": {"title": "Intro"}},'
            ' {"start_time": "61.5"}]}'
        )
        with patch("streamer.pipeline.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=output)
            chapters = pipeline._probe_chapters("book.m4b")
        assert chapters == [
            {"title": "Intro", "start": 0.0},
            {"title": "Chapter 2", "start": 61.5},
        ]

    def test_probe_chapters_empty_on_failure(self):
        pipeline = AudioPipeline(ServerState(), MagicMock())
        with patch("streamer.pipeline.subprocess.run", side_effect=OSError):
            assert pipeline._probe_chapters("book.m4b") == []

    def test_playback_info_reports_current_chapter(self):
        pipeline = AudioPipeline(ServerState(), MagicMock())
        pipeline._track_chapters = self.CHAPTERS
        pipeline._track_bytes_written = int(75 * BYTES_PER_SECOND)
        info = pipeline.get_playback_info()
        assert info["chapter"] == "Part One"
        assert info["chapter_index"] == 1

    def test_playback_info_without_chapters(self):
        info = AudioPipeline(ServerState(), MagicMock()).get_playback_info()
        assert info["chapter"] is None
        assert info["chapter_index"] is None

    def test_probe_picks_up_real_chapters(self, tmp_path):
        meta = tmp_path / "meta.txt"
        meta.write_text(
            ";FFMETADATA1\n"
            "[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=1000\ntitle=One\n"
            "[CHAPTER]\nTIMEBASE=1/1000\nSTART=1000\nEND=2000\ntitle=Two\n"
        )
        book = tmp_path / "book.m4b"
        subprocess.run(
            [
                "ffmpeg", "-y", "-f", "lavfi", "-i", "sine=duration=2",
                "-i", str(meta), "-map_metadata", "1", "-map_chapters", "1",
                "-c:a", "aac", str(book),
            ],
            capture_output=True, check=True,
        )
        chapters = AudioPipeline(ServerState(), MagicMock())._probe_chapters(str(book))
        assert [c["title"] for c in chapters] == ["One", "Two"]


class TestLoopCurrent:
    def test_replays_track_when_it_ends(self):
        state = ServerState()
        state.current_track = "a.mp3"
        state.queue_add("b.mp3")
        state.loop_current = True
        pipeline = AudioPipeline(state, MagicMock())

        assert pipeline._get_next_track() == ("a.mp3", None, True)
        assert state.queue == ["b.mp3"]

    def test_next_still_advances_while_looping(self):
        state = ServerState()
        state.current_track = "a.mp3"
        state.queue_add("b.mp3")
        state.loop_current = True
        pipeline = AudioPipeline(state, MagicMock())
        pipeline._pending_action = ("next", None)

        assert pipeline._get_next_track()[0] == "b.mp3"
