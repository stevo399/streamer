import time

from streamer.pipeline import AudioPipeline, RingBuffer, _parse_ogg_pages
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


class TestAudioPipeline:
    def test_pipeline_produces_pcm_data(self, test_media_dir):
        state = ServerState()
        scanner = Scanner(roots=[
            test_media_dir / "entertainment",
            test_media_dir / "Podcast",
        ])
        pipeline = AudioPipeline(state, scanner)
        try:
            pipeline.start()
            time.sleep(2)

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
