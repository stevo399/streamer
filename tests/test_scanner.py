from pathlib import Path

import pytest

from streamer.scanner import AUDIO_EXTENSIONS, Scanner


class TestScan:
    def test_finds_audio_files(self, test_media_dir):
        scanner = Scanner(roots=[
            test_media_dir / "entertainment",
            test_media_dir / "Podcast",
        ])
        files = scanner.scan()
        assert len(files) == 4
        assert all(f.suffix.lower() in AUDIO_EXTENSIONS for f in files)

    def test_excludes_non_audio(self, test_media_dir):
        scanner = Scanner(roots=[
            test_media_dir / "entertainment",
            test_media_dir / "Podcast",
        ])
        files = scanner.scan()
        names = [f.name for f in files]
        assert "notes.txt" not in names

    def test_handles_missing_root(self, tmp_path):
        scanner = Scanner(roots=[tmp_path / "nonexistent"])
        files = scanner.scan()
        assert files == []

    def test_handles_empty_root(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        scanner = Scanner(roots=[empty])
        files = scanner.scan()
        assert files == []


class TestPickRandom:
    def test_returns_audio_file(self, test_media_dir):
        scanner = Scanner(roots=[
            test_media_dir / "entertainment",
            test_media_dir / "Podcast",
        ])
        picked = scanner.pick_random()
        assert picked.exists()
        assert picked.suffix.lower() in AUDIO_EXTENSIONS

    def test_raises_on_no_files(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        scanner = Scanner(roots=[empty])
        with pytest.raises(RuntimeError):
            scanner.pick_random()

    def test_avoids_recent_tracks(self, test_media_dir):
        scanner = Scanner(roots=[
            test_media_dir / "entertainment",
            test_media_dir / "Podcast",
        ])
        all_files = scanner.scan()
        target = all_files[0]
        recent = [str(f) for f in all_files if f != target]
        for _ in range(20):
            picked = scanner.pick_random(recent=recent)
            assert picked == target

    def test_prefers_different_folder(self, test_media_dir):
        scanner = Scanner(roots=[
            test_media_dir / "entertainment",
            test_media_dir / "Podcast",
        ])
        last = str(test_media_dir / "entertainment" / "Test Show" / "season 01" / "01.mp3")
        same_folder = 0
        trials = 200
        for _ in range(trials):
            picked = scanner.pick_random(last_track=last)
            folder = scanner._get_folder(picked)
            last_folder = scanner._get_folder(Path(last))
            if folder == last_folder:
                same_folder += 1
        assert same_folder < trials * 0.35


class TestResolveBrowsePath:
    def test_resolves_root_folder(self, test_media_dir):
        scanner = Scanner(roots=[
            test_media_dir / "entertainment",
            test_media_dir / "Podcast",
        ])
        resolved = scanner.resolve_browse_path("entertainment")
        assert resolved == test_media_dir / "entertainment"

    def test_resolves_subfolder(self, test_media_dir):
        scanner = Scanner(roots=[
            test_media_dir / "entertainment",
            test_media_dir / "Podcast",
        ])
        resolved = scanner.resolve_browse_path("entertainment/Test Show/season 01")
        assert resolved == test_media_dir / "entertainment" / "Test Show" / "season 01"

    def test_resolves_file(self, test_media_dir):
        scanner = Scanner(roots=[
            test_media_dir / "entertainment",
            test_media_dir / "Podcast",
        ])
        resolved = scanner.resolve_browse_path("entertainment/Test Show/season 01/01.mp3")
        assert resolved is not None
        assert resolved.is_file()

    def test_rejects_unknown_root(self, test_media_dir):
        scanner = Scanner(roots=[test_media_dir / "entertainment"])
        assert scanner.resolve_browse_path("unknown/folder") is None

    def test_rejects_path_traversal(self, test_media_dir):
        scanner = Scanner(roots=[test_media_dir / "entertainment"])
        assert scanner.resolve_browse_path("entertainment/../../etc") is None

    def test_rejects_nonexistent_path(self, test_media_dir):
        scanner = Scanner(roots=[test_media_dir / "entertainment"])
        assert scanner.resolve_browse_path("entertainment/no_such_folder") is None


class TestListDirectory:
    def test_lists_dirs_and_audio_files(self, test_media_dir):
        scanner = Scanner(roots=[
            test_media_dir / "entertainment",
            test_media_dir / "Podcast",
        ])
        path = test_media_dir / "entertainment" / "Test Show" / "season 01"
        dirs, files = scanner.list_directory(path)
        assert dirs == []
        assert "01.mp3" in files
        assert "02.mp3" in files
        assert "03.mp3" in files
        assert "notes.txt" not in files

    def test_lists_subdirectories(self, test_media_dir):
        scanner = Scanner(roots=[
            test_media_dir / "entertainment",
        ])
        path = test_media_dir / "entertainment"
        dirs, files = scanner.list_directory(path)
        assert "Test Show" in dirs
        assert files == []


class TestBookOrdering:
    def _make_book(self, tmp_path):
        book = tmp_path / "Book"
        book.mkdir()
        for name in ["chapter 10.mp3", "chapter 2.mp3", "chapter 1.mp3", "notes.txt"]:
            (book / name).write_bytes(b"")
        (book / "extra.m4b").write_bytes(b"")
        return book

    def test_m4b_is_an_audio_extension(self):
        assert ".m4b" in AUDIO_EXTENSIONS

    def test_list_directory_uses_natural_order(self, tmp_path):
        book = self._make_book(tmp_path)
        _, files = Scanner(roots=[tmp_path]).list_directory(book)
        assert files == ["chapter 1.mp3", "chapter 2.mp3", "chapter 10.mp3", "extra.m4b"]

    def test_next_in_folder_follows_natural_order(self, tmp_path):
        book = self._make_book(tmp_path)
        scanner = Scanner(roots=[tmp_path])
        assert scanner.next_in_folder(str(book / "chapter 2.mp3")) == book / "chapter 10.mp3"

    def test_next_in_folder_returns_none_at_end(self, tmp_path):
        book = self._make_book(tmp_path)
        scanner = Scanner(roots=[tmp_path])
        assert scanner.next_in_folder(str(book / "extra.m4b")) is None

    def test_next_in_folder_returns_none_for_unknown_file(self, tmp_path):
        scanner = Scanner(roots=[tmp_path])
        assert scanner.next_in_folder(str(tmp_path / "missing.mp3")) is None
