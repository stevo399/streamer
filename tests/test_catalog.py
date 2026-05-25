from pathlib import Path

from streamer.catalog import build_catalog
from streamer.scanner import Scanner


def _make_files(base, structure):
    """Create empty files from a nested dict. Keys are folder names, leaf lists are filenames."""
    for name, children in structure.items():
        path = base / name
        if isinstance(children, list):
            path.mkdir(parents=True, exist_ok=True)
            for fname in children:
                (path / fname).write_bytes(b"")
        else:
            _make_files(path, children)


class TestBuildCatalog:
    def test_entertainment_shows(self, tmp_path):
        _make_files(tmp_path / "entertainment", {
            "Test Show": {
                "season 01": ["01.mp3", "02.mp3", "03.mp3"],
                "season 02": ["01.mp3", "02.mp3"],
            },
        })
        scanner = Scanner(roots=[tmp_path / "entertainment"])
        text, lookup = build_catalog(scanner)
        assert "Test Show" in text
        assert "season 01" in text
        assert "3 total" in text
        assert "season 02" in text
        assert "2 total" in text

    def test_podcasts(self, tmp_path):
        _make_files(tmp_path / "podcasts", {
            "Test Podcast": ["ep01.mp3", "ep02.mp3"],
        })
        scanner = Scanner(roots=[tmp_path / "podcasts"])
        text, lookup = build_catalog(scanner)
        assert "Test Podcast" in text
        assert "ep01" in text
        assert "ep02" in text

    def test_includes_notes(self, tmp_path):
        _make_files(tmp_path / "entertainment", {
            "Test Show": {"season 01": ["01.mp3"]},
        })
        notes_dir = tmp_path / "notes"
        show_notes = notes_dir / "Test Show"
        show_notes.mkdir(parents=True)
        (show_notes / "show.md").write_text("A show about testing things.")
        scanner = Scanner(roots=[tmp_path / "entertainment"])
        text, lookup = build_catalog(scanner, notes_dir=str(notes_dir))
        assert "A show about testing things." in text

    def test_empty_roots(self, tmp_path):
        scanner = Scanner(roots=[tmp_path / "nonexistent"])
        text, lookup = build_catalog(scanner)
        assert text == ""
        assert lookup == {}

    def test_path_lookup_entertainment(self, tmp_path):
        _make_files(tmp_path / "entertainment", {
            "Test Show": {"season 01": ["01.mp3", "02.mp3"]},
        })
        scanner = Scanner(roots=[tmp_path / "entertainment"])
        text, lookup = build_catalog(scanner)
        key = "Test Show/season 01/01"
        assert key in lookup
        assert lookup[key] == str(tmp_path / "entertainment" / "Test Show" / "season 01" / "01.mp3")

    def test_path_lookup_podcast(self, tmp_path):
        _make_files(tmp_path / "podcasts", {
            "Test Podcast": ["ep01.mp3"],
        })
        scanner = Scanner(roots=[tmp_path / "podcasts"])
        text, lookup = build_catalog(scanner)
        key = "Test Podcast/ep01"
        assert key in lookup
        assert lookup[key] == str(tmp_path / "podcasts" / "Test Podcast" / "ep01.mp3")

    def test_excludes_non_audio(self, tmp_path):
        _make_files(tmp_path / "entertainment", {
            "Test Show": {"season 01": ["01.mp3", "notes.txt"]},
        })
        scanner = Scanner(roots=[tmp_path / "entertainment"])
        text, lookup = build_catalog(scanner)
        assert "notes.txt" not in text
        assert all("notes" not in k for k in lookup)
