import subprocess
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor

from streamer.curator import Curator

BYTES_PER_SECOND = 44100 * 2 * 2


def _parse_ogg_pages(data: bytes) -> list[tuple[int, int]]:
    """Return (start, end) byte offsets for each complete OGG page found at the start of data."""
    pages = []
    pos = 0
    while pos + 27 <= len(data):
        if data[pos:pos + 4] != b'OggS':
            break
        n_segs = data[pos + 26]
        seg_end = pos + 27 + n_segs
        if seg_end > len(data):
            break
        page_end = seg_end + sum(data[pos + 27:seg_end])
        if page_end > len(data):
            break
        pages.append((pos, page_end))
        pos = page_end
    return pages


class RingBuffer:
    def __init__(self, size: int = 512 * 1024):
        self._buffer = bytearray(size)
        self._size = size
        self._write_pos = 0
        self._lock = threading.Lock()
        self._headers = b""

    def write(self, data: bytes) -> None:
        with self._lock:
            dlen = len(data)
            start = self._write_pos % self._size
            end = start + dlen
            if end <= self._size:
                self._buffer[start:end] = data
            else:
                first = self._size - start
                self._buffer[start:self._size] = data[:first]
                self._buffer[0:dlen - first] = data[first:]
            self._write_pos += dlen

    def read(self, read_pos: int, max_bytes: int = 65536) -> tuple[bytes | None, int]:
        with self._lock:
            earliest = max(0, self._write_pos - self._size)
            if read_pos < earliest:
                return None, earliest

            available = self._write_pos - read_pos
            if available <= 0:
                return b"", read_pos

            to_read = min(available, max_bytes)
            start = read_pos % self._size
            end = start + to_read
            if end <= self._size:
                result = bytes(self._buffer[start:end])
            else:
                first = self._size - start
                result = bytes(self._buffer[start:self._size]) + bytes(self._buffer[0:to_read - first])
            return result, read_pos + to_read

    def get_current_position(self) -> int:
        with self._lock:
            return self._write_pos

    def set_headers(self, headers: bytes) -> None:
        with self._lock:
            self._headers = headers

    def get_headers(self) -> bytes:
        with self._lock:
            return self._headers


class AudioPipeline:
    def __init__(self, state, scanner):
        self.state = state
        self.scanner = scanner
        self.pcm_buffer = RingBuffer(size=2 * 1024 * 1024)
        self.ogg_buffer = RingBuffer(size=512 * 1024)
        self._running = False
        self._current_decoder = None
        self._ogg_encoder: subprocess.Popen | None = None
        self._pending_action: tuple[str, str | None] | None = None
        self._action_lock = threading.Lock()
        self._dj_cancel = threading.Event()
        self._last_track: str | None = None
        self._curator = Curator(state, scanner)

        # Background clip generation: one worker generates clips while tracks play.
        self._clip_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dj-clip")
        self._prefetch_future: Future | None = None
        self._prefetch_for: str | None = None    # next track the prefetched clip is keyed to
        self._pre_selected_random: str | None = None  # random pick stored for reuse

    def start(self):
        self._running = True
        self._ogg_encoder = subprocess.Popen(
            [
                "ffmpeg", "-v", "error",
                "-f", "s16le", "-ar", "44100", "-ac", "2", "-i", "pipe:0",
                "-f", "ogg", "-acodec", "libvorbis", "-b:a", "128k",
                "-flush_packets", "1",
                "pipe:1",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        threading.Thread(target=self._run, daemon=True).start()
        threading.Thread(target=self._run_ogg_stdout, daemon=True).start()
        self._curator.start()

    def stop(self):
        self._running = False
        self._curator.stop()
        if self._current_decoder:
            self._current_decoder.kill()
            self._current_decoder.wait()
        if self._ogg_encoder:
            try:
                self._ogg_encoder.stdin.close()
            except OSError:
                pass
            self._ogg_encoder.kill()
            self._ogg_encoder.wait()
        self._clip_executor.shutdown(wait=False, cancel_futures=True)

    def request_next(self):
        with self._action_lock:
            self._pending_action = ("next", None)
        self._dj_cancel.set()
        if self._current_decoder:
            self._current_decoder.kill()

    def request_previous(self) -> bool:
        if not self.state.history:
            return False
        with self._action_lock:
            self._pending_action = ("previous", None)
        self._dj_cancel.set()
        if self._current_decoder:
            self._current_decoder.kill()
        return True

    def request_play(self, path: str):
        with self._action_lock:
            self._pending_action = ("play", path)
        self._dj_cancel.set()
        if self._current_decoder:
            self._current_decoder.kill()

    def _consume_action(self) -> tuple[str, str | None] | None:
        with self._action_lock:
            action = self._pending_action
            self._pending_action = None
            return action

    def _get_next_track(self) -> str:
        action = self._consume_action()

        if action:
            kind, target = action
            if kind == "previous":
                # Explicit track choice — discard the pre-selected random.
                self._pre_selected_random = None
                track = self.state.go_previous()
                if track:
                    return track
            elif kind == "play":
                self._pre_selected_random = None
                self.state.play_now(target)
                return target
            # "next": fall through and use the pre-selected track so the
            # clip that was generated during the previous track still matches.

        track = self.state.advance()
        if track:
            # A queued track was popped — pre-selection is no longer relevant.
            self._pre_selected_random = None
            return track

        # Reuse the random pick that _start_clip_prefetch already made so
        # the generated clip is keyed to the same track we are about to play.
        if self._pre_selected_random is not None:
            track = self._pre_selected_random
            self._pre_selected_random = None
            self.state.current_track = track
            return track

        picked = self.scanner.pick_random(
            recent=self.state.history,
            last_track=self._last_track,
        )
        self.state.current_track = str(picked)
        return str(picked)

    # ── DJ clip prefetch ──────────────────────────────────────────────────────

    def _start_clip_prefetch(self, current_track: str) -> None:
        """Kick off clip generation for (current_track → next) in the background.

        For random playback, also stores the chosen track in _pre_selected_random
        so _get_next_track returns the same file — guaranteeing the clip key matches
        the track that actually plays next.
        """
        self._prefetch_future = None
        self._prefetch_for = None
        self._pre_selected_random = None

        q = self.state.queue
        if q:
            # Queue-based: _get_next_track will call state.advance() → same track.
            next_hint = q[0]
        else:
            try:
                picked = self.scanner.pick_random(
                    recent=self.state.history,
                    last_track=current_track,
                )
                next_hint = str(picked)
                # Store so _get_next_track reuses this pick instead of drawing again.
                self._pre_selected_random = next_hint
            except Exception:
                return

        self._prefetch_for = next_hint
        self._prefetch_future = self._clip_executor.submit(
            self._generate_clip_bg, current_track, next_hint,
        )

    def _generate_clip_bg(self, prev_track: str, next_track: str) -> bytes | None:
        try:
            from streamer.dj import generate_dj_clip
            return generate_dj_clip(prev_track, next_track)
        except Exception:
            return None

    def _consume_prefetch(self, actual_next: str) -> bytes | None:
        """Return the pre-generated clip if it was keyed to actual_next, else None."""
        future = self._prefetch_future
        keyed_to = self._prefetch_for
        self._prefetch_future = None
        self._prefetch_for = None

        if future is None or keyed_to != actual_next:
            return None
        try:
            # Non-blocking: if the clip is ready return it, otherwise skip.
            # For normal track ends the future has been running for minutes and
            # is already done. For early skips we don't stall — no clip is fine.
            return future.result(timeout=0)
        except Exception:
            return None

    # ── Playback ──────────────────────────────────────────────────────────────

    def _run(self):
        while self._running:
            track = self._get_next_track()

            # Play the pre-generated DJ clip for this transition if it's ready.
            # _dj_cancel may be set from killing the previous decoder — clear it
            # so _play_clip_pcm can be interrupted by the NEXT skip, not this one.
            if self.state.dj_enabled and self._last_track:
                self._dj_cancel.clear()
                pcm = self._consume_prefetch(track)
                if pcm:
                    self._play_clip_pcm(pcm)

            decoder = self._start_decoder(track)
            self._current_decoder = decoder

            # While this track plays, generate the clip for the next transition.
            if self.state.dj_enabled:
                self._start_clip_prefetch(track)

            track_start = time.monotonic()
            bytes_written = 0

            while self._running:
                chunk = decoder.stdout.read(4096)
                if not chunk:
                    break
                self.pcm_buffer.write(chunk)
                self._write_to_ogg_encoder(chunk)
                bytes_written += len(chunk)

                expected = bytes_written / BYTES_PER_SECOND
                elapsed = time.monotonic() - track_start
                ahead = expected - elapsed
                if ahead > 0.01:
                    time.sleep(ahead)

            decoder.wait()
            self._current_decoder = None
            self._last_track = track

    def _play_clip_pcm(self, pcm: bytes) -> None:
        start = time.monotonic()
        written = 0
        for i in range(0, len(pcm), 4096):
            if self._dj_cancel.is_set():
                return
            chunk = pcm[i:i + 4096]
            self.pcm_buffer.write(chunk)
            self._write_to_ogg_encoder(chunk)
            written += len(chunk)
            expected = written / BYTES_PER_SECOND
            elapsed = time.monotonic() - start
            ahead = expected - elapsed
            if ahead > 0.01:
                time.sleep(ahead)

    def _start_decoder(self, path: str) -> subprocess.Popen:
        return subprocess.Popen(
            [
                "ffmpeg", "-v", "error", "-i", path,
                "-f", "s16le", "-acodec", "pcm_s16le",
                "-ar", "44100", "-ac", "2", "pipe:1",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

    def _write_to_ogg_encoder(self, data: bytes) -> None:
        if self._ogg_encoder:
            try:
                self._ogg_encoder.stdin.write(data)
                self._ogg_encoder.stdin.flush()
            except (BrokenPipeError, OSError):
                pass

    def _run_ogg_stdout(self):
        encoder = self._ogg_encoder
        header_buf = b""
        headers_captured = False
        while self._running:
            chunk = encoder.stdout.read1(4096)
            if not chunk:
                break
            if not headers_captured:
                header_buf += chunk
                pages = _parse_ogg_pages(header_buf)
                if len(pages) >= 2:
                    # Capture all consecutive header pages (odd Vorbis packet type).
                    # Vorbis identification=0x01, comment=0x03, setup=0x05 are all odd.
                    # Audio packets are even (0x00). Stop at the first audio page.
                    header_end = 0
                    for start, end in pages:
                        n_segs = header_buf[start + 26]
                        ds = start + 27 + n_segs
                        if ds < end and header_buf[ds] % 2 != 0:
                            header_end = end
                        else:
                            break
                    if header_end > 0:
                        self.ogg_buffer.set_headers(header_buf[:header_end])
                        headers_captured = True
                        self.ogg_buffer.write(header_buf)
                        header_buf = b""
            else:
                self.ogg_buffer.write(chunk)
