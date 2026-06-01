import random
from pathlib import Path

from streamer.config import MEDIA_ROOTS

AUDIO_EXTENSIONS = frozenset({
    ".mp3", ".ogg", ".wav", ".flac",
    ".m4a", ".wma", ".aac", ".opus", ".m4r",
})


class Scanner:
    def __init__(self, roots: list[Path] | None = None):
        self.roots = roots if roots is not None else MEDIA_ROOTS

    def scan(self) -> list[Path]:
        files = []
        for root in self.roots:
            if not root.exists():
                continue
            for f in root.rglob("*"):
                if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS:
                    files.append(f)
        return files

    def _get_folder(self, file_path: Path) -> Path | None:
        for root in self.roots:
            try:
                file_path.relative_to(root)
                return file_path.parent
            except ValueError:
                continue
        return None

    def _get_show(self, file_path: Path) -> Path | None:
        for root in self.roots:
            try:
                rel = file_path.relative_to(root)
                if rel.parts:
                    return root / rel.parts[0]
                return root
            except ValueError:
                continue
        return None

    def scan_by_folder(self) -> dict[Path, list[Path]]:
        folders: dict[Path, list[Path]] = {}
        for root in self.roots:
            if not root.exists():
                continue
            for f in root.rglob("*"):
                if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS:
                    folder = self._get_folder(f)
                    if folder:
                        folders.setdefault(folder, []).append(f)
        return folders

    def pick_random(
        self,
        recent: list[str] | None = None,
        last_track: str | None = None,
    ) -> Path:
        folders = self.scan_by_folder()
        if not folders:
            raise RuntimeError("No audio files found in media folders")

        shows: dict[Path, list[Path]] = {}
        for folder in folders:
            show = self._get_show(folder)
            if show:
                shows.setdefault(show, []).append(folder)

        recent_set = set(recent[-30:]) if recent else set()
        last_folder = self._get_folder(Path(last_track)) if last_track else None
        last_show = self._get_show(Path(last_track)) if last_track else None
        show_list = list(shows.keys())

        for _ in range(200):
            if last_show and last_show in show_list and len(show_list) > 1:
                if random.random() > 0.15:
                    show_candidates = [s for s in show_list if s != last_show]
                else:
                    show_candidates = show_list
            else:
                show_candidates = show_list

            show = random.choice(show_candidates)
            subfolders = shows[show]

            if last_folder and last_folder in subfolders and len(subfolders) > 1:
                if random.random() > 0.15:
                    subfolder_candidates = [f for f in subfolders if f != last_folder]
                else:
                    subfolder_candidates = subfolders
            else:
                subfolder_candidates = subfolders

            folder = random.choice(subfolder_candidates)
            picked = random.choice(folders[folder])

            if str(picked) not in recent_set:
                return picked

        return picked

    def resolve_browse_path(self, browse_path: str) -> Path | None:
        parts = Path(browse_path).parts
        if not parts:
            return None

        root_name = parts[0]
        for root in self.roots:
            if root.name == root_name:
                result = root
                for part in parts[1:]:
                    if part == "..":
                        return None
                    result = result / part
                try:
                    result.resolve().relative_to(root.resolve())
                except ValueError:
                    return None
                return result if result.exists() else None
        return None

    def list_directory(self, path: Path) -> tuple[list[str], list[str]]:
        dirs = sorted(d.name for d in path.iterdir() if d.is_dir())
        files = sorted(
            f.name for f in path.iterdir()
            if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS
        )
        return dirs, files
