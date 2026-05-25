from pathlib import Path

from streamer.scanner import AUDIO_EXTENSIONS


def build_catalog(scanner, notes_dir: str | None = None) -> tuple[str, dict[str, str]]:
    lines: list[str] = []
    path_lookup: dict[str, str] = {}

    for root in scanner.roots:
        if not root.exists():
            continue

        root_name = root.name.lower()
        is_podcast = "podcast" in root_name

        if is_podcast:
            _catalog_podcasts(root, lines, path_lookup, notes_dir)
        else:
            _catalog_entertainment(root, lines, path_lookup, notes_dir)

    return "\n".join(lines).strip(), path_lookup


def _catalog_entertainment(root, lines, path_lookup, notes_dir):
    lines.append("[Entertainment]")
    shows = sorted(d for d in root.iterdir() if d.is_dir())

    for show_dir in shows:
        show_name = show_dir.name
        seasons: dict[str, list[Path]] = {}

        for item in sorted(show_dir.iterdir()):
            if item.is_dir():
                eps = sorted(
                    f for f in item.iterdir()
                    if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS
                )
                if eps:
                    seasons[item.name] = eps

        if not seasons:
            continue

        total_eps = sum(len(eps) for eps in seasons.values())
        season_list = sorted(seasons.keys())
        lines.append(f"{show_name} ({len(season_list)} seasons, {total_eps} episodes, seasons: {', '.join(season_list)})")

        for season_name in season_list:
            for ep in seasons[season_name]:
                path_lookup[f"{show_name}/{season_name}/{ep.stem}"] = str(ep)

        _add_show_notes(show_name, notes_dir, lines)

    lines.append("")


def _catalog_podcasts(root, lines, path_lookup, notes_dir):
    lines.append("[Podcasts]")
    shows = sorted(d for d in root.iterdir() if d.is_dir())

    for show_dir in shows:
        show_name = show_dir.name
        all_eps = sorted(
            f for f in show_dir.rglob("*")
            if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS
        )
        if not all_eps:
            continue

        subfolders = set()
        for ep in all_eps:
            rel = ep.relative_to(show_dir)
            if len(rel.parts) > 1:
                path_lookup[f"{show_name}/{'/'.join(rel.parent.parts)}/{ep.stem}"] = str(ep)
                subfolders.add(rel.parts[0])
            else:
                path_lookup[f"{show_name}/{ep.stem}"] = str(ep)

        if subfolders:
            folder_list = ", ".join(sorted(subfolders))
            lines.append(f"{show_name} ({len(all_eps)} episodes, folders: {folder_list})")
        else:
            lines.append(f"{show_name} ({len(all_eps)} episodes)")

        _add_show_notes(show_name, notes_dir, lines)

    lines.append("")


def _add_show_notes(show_name, notes_dir, lines):
    if not notes_dir:
        return
    from streamer.context import _normalize
    notes_path = Path(notes_dir)
    if not notes_path.is_dir():
        return
    for d in notes_path.iterdir():
        if d.is_dir() and _normalize(d.name) == _normalize(show_name):
            show_note = d / "show.md"
            if show_note.is_file():
                content = show_note.read_text(encoding="utf-8").strip()
                first_line = content.split("\n")[0].strip()
                lines.append(f"  [Show notes: {first_line}]")
            return
