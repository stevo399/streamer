import json
import subprocess
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor

from streamer.curator import Curator

BYTES_PER_SECOND = 44100 * 2 * 2
BYTES_PER_FRAME = 2 * 2


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
        self._pending_action: tuple[str, str | float | None] | None = None
        self._action_lock = threading.Lock()
        self._dj_cancel = threading.Event()
        self._last_track: str | None = None
        self._curator = Curator(state, scanner)

        # Background clip generation: one worker generates clips while tracks play.
        self._clip_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dj-clip")
        self._prefetch_future: Future | None = None
        self._prefetch_for: str | None = None    # next track the prefetched clip is keyed to
        self._pre_selected_random: str | None = None  # random pick stored for reuse
        self._track_duration: float | None = None
        self._track_chapters: list[dict] = []
        self._track_offset_bytes: int = 0
        self._track_bytes_written: int = 0

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
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
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

    def request_seek(self, position: float) -> bool:
        if position < 0:
            return False
        if not self.state.current_track:
            return False

        with self._action_lock:
            self._pending_action = ("seek", position)
        self._dj_cancel.set()
        if self._current_decoder:
            self._current_decoder.kill()
        return True

    def request_pause(self) -> bool:
        if not self.state.current_track:
            return False
        self.state.paused = True
        return True

    def request_resume(self) -> bool:
        if not self.state.current_track:
            return False
        self.state.paused = False
        return True

    def _consume_action(self) -> tuple[str, str | float | None] | None:
        with self._action_lock:
            action = self._pending_action
            self._pending_action = None
            return action

    def _get_next_track(self) -> tuple[str, float | None, bool]:
        action = self._consume_action()

        if action is None and self.state.loop_current and self.state.current_track:
            # Track ended on its own: replay it. Skip/previous/play still move on.
            return self.state.current_track, None, True

        if action:
            kind, target = action
            if kind == "previous":
                # Explicit track choice — discard the pre-selected random.
                self._pre_selected_random = None
                track = self.state.go_previous()
                if track:
                    return track, None, False
            elif kind == "play":
                self._pre_selected_random = None
                self.state.play_now(target)
                return target, None, False
            elif kind == "seek":
                current = self.state.current_track
                if current:
                    return current, float(target), True
            # "next": fall through and use the pre-selected track so the
            # clip that was generated during the previous track still matches.

        finished = self.state.current_track
        track = self.state.advance()
        if track:
            # A queued track was popped — pre-selection is no longer relevant.
            self._pre_selected_random = None
            return track, None, False

        if self.state.book_mode and finished:
            # Book mode: continue with the next file in the folder instead of shuffling.
            following = self.scanner.next_in_folder(finished)
            if following:
                self._pre_selected_random = None
                self.state.current_track = str(following)
                return str(following), None, False
            # End of the book: fall back to shuffle.
            self.state.book_mode = False

        # Reuse the random pick that _start_clip_prefetch already made so
        # the generated clip is keyed to the same track we are about to play.
        if self._pre_selected_random is not None:
            track = self._pre_selected_random
            self._pre_selected_random = None
            self.state.current_track = track
            return track, None, False

        picked = self.scanner.pick_random(
            recent=self.state.history,
            last_track=self._last_track,
        )
        self.state.current_track = str(picked)
        return str(picked), None, False

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
        elif self.state.book_mode:
            following = self.scanner.next_in_folder(current_track)
            if following is None:
                return
            next_hint = str(following)
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

    # ── Playback info ────────────────────────────────────────────────────────

    def _probe_duration(self, path: str) -> float | None:
        try:
            proc = subprocess.run(
                [
                    "ffprobe", "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "csv=p=0",
                    path,
                ],
                capture_output=True,
                text=True,
                timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if proc.returncode == 0 and proc.stdout.strip():
                return float(proc.stdout.strip())
        except Exception:
            pass
        return None

    def _probe_chapters(self, path: str) -> list[dict]:
        """Return [{"title", "start"}] for files with embedded chapters (e.g. M4B)."""
        try:
            proc = subprocess.run(
                [
                    "ffprobe", "-v", "error",
                    "-show_chapters", "-of", "json",
                    path,
                ],
                capture_output=True,
                text=True,
                timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if proc.returncode != 0:
                return []
            chapters = json.loads(proc.stdout).get("chapters", [])
            return [
                {
                    "title": c.get("tags", {}).get("title") or f"Chapter {i + 1}",
                    "start": float(c["start_time"]),
                }
                for i, c in enumerate(chapters)
            ]
        except Exception:
            return []

    def _probe_duration_async(self, path: str) -> None:
        duration = self._probe_duration(path)
        if duration is None:
            return
        if self._current_decoder is None:
            return
        if self.state.current_track != path:
            return
        self._track_duration = duration
        self.state.notify_change()
        chapters = self._probe_chapters(path)
        if chapters and self.state.current_track == path:
            self._track_chapters = chapters
            self.state.notify_change()

    def get_chapters(self) -> list[dict]:
        return list(self._track_chapters)

    def _current_chapter(self, elapsed: float) -> tuple[int, str] | None:
        current = None
        for i, chapter in enumerate(self._track_chapters):
            if chapter["start"] <= elapsed:
                current = (i, chapter["title"])
        return current

    def get_playback_info(self) -> dict:
        elapsed = (self._track_offset_bytes + self._track_bytes_written) / BYTES_PER_SECOND
        duration = self._track_duration
        chapter = self._current_chapter(elapsed)
        return {
            "elapsed": round(elapsed, 1),
            "duration": round(duration, 1) if duration else None,
            "remaining": round(duration - elapsed, 1) if duration else None,
            "paused": self.state.paused,
            "chapter": chapter[1] if chapter else None,
            "chapter_index": chapter[0] if chapter else None,
        }

    # ── Playback ──────────────────────────────────────────────────────────────

    def _run(self):
        while self._running:
            track, seek_position, resume_same_track = self._get_next_track()

            if resume_same_track:
                # Seek within the current track. Do not play transition audio,
                # but clear the cancel flag so future DJ clips can run again.
                self._dj_cancel.clear()
            else:
                # Play the pre-generated DJ clip for this transition if it's ready.
                # _dj_cancel may be set from killing the previous decoder — clear it
                # so _play_clip_pcm can be interrupted by the NEXT skip, not this one.
                if self.state.dj_enabled and self._last_track:
                    self._dj_cancel.clear()
                    pcm = self._consume_prefetch(track)
                    if pcm:
                        self._play_clip_pcm(pcm)

                announcement = self.state.consume_curator_announcement()
                if announcement:
                    self._play_curator_announcement(announcement)

            decoder = self._start_decoder(track, seek_position=seek_position)
            self._current_decoder = decoder
            self._track_offset_bytes = int((seek_position or 0.0) * BYTES_PER_SECOND)
            self._track_bytes_written = 0
            self._track_duration = None
            if not resume_same_track:
                self._track_chapters = []
            self.state.notify_change()
            threading.Thread(
                target=self._probe_duration_async,
                args=(track,),
                daemon=True,
            ).start()

            # While this track plays, generate the clip for the next transition.
            if self.state.dj_enabled and not resume_same_track:
                self._start_clip_prefetch(track)

            track_start = time.monotonic()

            while self._running:
                if self.state.paused:
                    time.sleep(0.1)
                    track_start += 0.1
                    continue

                chunk = decoder.stdout.read(4096)
                if not chunk:
                    break
                # Keep PCM frame-aligned so raw-PCM listeners never swap channels.
                chunk = chunk[:len(chunk) - len(chunk) % BYTES_PER_FRAME]
                if not chunk:
                    continue
                self.pcm_buffer.write(chunk)
                self._write_to_ogg_encoder(chunk)
                self._track_bytes_written += len(chunk)

                expected = self._track_bytes_written / BYTES_PER_SECOND
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

    def _play_curator_announcement(self, text: str) -> None:
        try:
            from streamer.dj import decode_to_pcm, get_tts_engine
            tts = get_tts_engine()
            audio = tts.synthesize(text)
            if not audio:
                return
            pcm = decode_to_pcm(audio)
            if pcm:
                self._dj_cancel.clear()
                self._play_clip_pcm(pcm)
        except Exception:
            pass

    def _start_decoder(self, path: str, seek_position: float | None = None) -> subprocess.Popen:
        cmd = ["ffmpeg", "-v", "error"]
        if seek_position is not None and seek_position > 0:
            cmd.extend(["-ss", str(seek_position)])
        cmd.extend([
            "-i", path,
            "-f", "s16le", "-acodec", "pcm_s16le",
            "-ar", "44100", "-ac", "2", "pipe:1",
        ])
        return subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
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
