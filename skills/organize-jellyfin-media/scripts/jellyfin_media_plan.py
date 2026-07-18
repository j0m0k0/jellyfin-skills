#!/usr/bin/env python3
"""Plan and optionally apply conservative Jellyfin media renames.

The script is intentionally mapping-driven. It does not resolve metadata IDs.
It skips already-canonical top-level folders and aborts on destination
collisions before moving anything.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


VIDEO_EXTS = {
    ".mkv", ".mp4", ".avi", ".mov", ".m4v", ".wmv", ".flv", ".webm", ".ts", ".m2ts"
}
SUBTITLE_EXTS = {".srt", ".ass", ".ssa", ".vtt", ".sub", ".idx"}
AUDIO_EXTS = {".aac", ".ac3", ".dts", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav"}
CANONICAL_RE = re.compile(r"^.+ \(\d{4}\) \[imdbid-tt\d+\]$")
SEASON_DIR_RE = re.compile(r"^(?:Season|S|SE)\s*0*(\d+)$", re.IGNORECASE)
EPISODE_RE = re.compile(r"S(?P<s>\d{1,2})E(?P<e>\d{1,3})(?:[- ]?E?(?P<e2>\d{1,3}))?", re.IGNORECASE)
RESERVED_RE = re.compile(r'[<>:"/\\|?*]')


@dataclass(frozen=True)
class Move:
    src: Path
    dst: Path
    reason: str


def sanitize_title(title: str) -> str:
    title = RESERVED_RE.sub(" ", title)
    title = re.sub(r"\s+", " ", title).strip()
    return title.rstrip(".")


def imdbid(value: str) -> str:
    value = value.strip()
    if re.fullmatch(r"tt\d+", value):
        return value
    if re.fullmatch(r"imdbid-tt\d+", value):
        return value.removeprefix("imdbid-")
    raise ValueError(f"Invalid IMDb id: {value!r}")


def canonical_name(item: dict) -> str:
    title = sanitize_title(str(item["title"]))
    year = int(item["year"])
    return f"{title} ({year}) [imdbid-{imdbid(str(item['imdbid']))}]"


def is_canonical(name: str) -> bool:
    return bool(CANONICAL_RE.fullmatch(name))


def load_items(path: Path) -> list[dict]:
    raw = json.loads(path.read_text())
    items = raw.get("items", raw) if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        raise ValueError("Mapping must be a list or an object with an 'items' list")
    for item in items:
        for key in ("source", "type", "title", "year", "imdbid"):
            if key not in item:
                raise ValueError(f"Mapping item missing {key}: {item}")
    return items


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    raise FileExistsError(f"Destination already exists: {path}")


def is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def require_path_within_root(path: Path, root: Path, description: str) -> None:
    """Reject a path that resolves outside the resolved library root."""
    if not is_relative_to(path.resolve(strict=False), root):
        raise ValueError(f"{description} escapes the library root: {path}")


def resolve_top_level_source(root: Path, source: object) -> Path:
    """Validate a mapping source as a real, non-symlinked top-level entry."""
    if not isinstance(source, str):
        raise ValueError("Mapping source must be a string")
    source_path = Path(source)
    if source_path.is_absolute() or "/" in source or "\\" in source:
        raise ValueError(f"Mapping source must be relative to the library root: {source!r}")
    if len(source_path.parts) != 1 or source_path.parts[0] in {"", ".", ".."}:
        raise ValueError(f"Mapping source must be a single top-level name: {source!r}")

    candidate = root / source_path
    if candidate.is_symlink():
        raise ValueError(f"Mapping source must not be a symlink: {source!r}")
    require_path_within_root(candidate, root, "Mapping source")
    return candidate


def add_move(moves: list[Move], src: Path, dst: Path, reason: str) -> None:
    if src == dst:
        return
    moves.append(Move(src, dst, reason))


def direct_videos(folder: Path) -> list[Path]:
    return sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXTS)


def version_label(path: Path, index: int) -> str:
    stem = path.stem
    for pattern, label in [
        (r"2160p|4k|uhd", "2160p"),
        (r"1080p|fhd", "1080p"),
        (r"720p|hd", "720p"),
        (r"extended", "Extended"),
        (r"directors?.?cut", "Directors Cut"),
        (r"unrated", "Unrated"),
        (r"remaster", "Remastered"),
    ]:
        if re.search(pattern, stem, re.IGNORECASE):
            return label
    return f"Version {index}"


def plan_movie_internal(folder: Path, canonical: str, moves: list[Move]) -> None:
    videos = direct_videos(folder)
    old_to_new_stems: dict[str, str] = {}
    for idx, video in enumerate(videos, start=1):
        if len(videos) == 1:
            new_stem = canonical
        elif video.stem.startswith(canonical + " - "):
            new_stem = video.stem
        else:
            new_stem = f"{canonical} - {version_label(video, idx)}"
        old_to_new_stems[video.stem] = new_stem
        add_move(moves, video, folder / f"{new_stem}{video.suffix}", "movie video rename")

    for sidecar in sorted(folder.iterdir()):
        if not sidecar.is_file() or sidecar.suffix.lower() not in SUBTITLE_EXTS | AUDIO_EXTS:
            continue
        for old_stem, new_stem in old_to_new_stems.items():
            if sidecar.stem == old_stem or sidecar.stem.startswith(old_stem + "."):
                suffix = sidecar.name[len(old_stem):]
                add_move(moves, sidecar, folder / f"{new_stem}{suffix}", "movie sidecar rename")
                break

    for nfo in sorted(folder.glob("*.nfo")):
        add_move(moves, nfo, folder / "other" / "original-nfo" / nfo.name, "archive old nfo")


def episode_new_name(path: Path, canonical: str) -> tuple[str, int] | None:
    match = EPISODE_RE.search(path.stem)
    if not match:
        return None
    season = int(match.group("s"))
    token = match.group(0).upper()
    token = re.sub(r"S(\d{1})", lambda m: f"S0{m.group(1)}", token, count=1)
    token = re.sub(r"E(\d{1})(?!\d)", lambda m: f"E0{m.group(1)}", token, count=1)
    tail = path.stem[match.end():].strip(" ._-")
    new_stem = f"{canonical} {token}" + (f" {tail}" if tail else "")
    return new_stem + path.suffix, season


def plan_show_internal(folder: Path, canonical: str, moves: list[Move]) -> None:
    for child in sorted(folder.iterdir()):
        if not child.is_dir():
            continue
        match = SEASON_DIR_RE.fullmatch(child.name)
        if match:
            dst = folder / f"Season {int(match.group(1)):02d}"
            add_move(moves, child, dst, "season folder normalize")

    for video in sorted(p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in VIDEO_EXTS):
        rel_parts = video.relative_to(folder).parts
        if any(part.lower() in {"other", "extras", "featurettes", "trailers", "backdrops"} for part in rel_parts[:-1]):
            continue
        parsed = episode_new_name(video, canonical)
        if not parsed:
            continue
        new_name, season = parsed
        dst = folder / f"Season {season:02d}" / new_name
        add_move(moves, video, dst, "episode move/rename")

    for nfo in sorted(folder.glob("*.nfo")):
        add_move(moves, nfo, folder / "other" / "original-nfo" / nfo.name, "archive old nfo")


def plan(root: Path, items: list[dict], library_type: str) -> tuple[list[Move], list[str]]:
    root = root.resolve()
    moves: list[Move] = []
    skipped: list[str] = []
    for item in items:
        kind = str(item["type"]).lower()
        if library_type == "movies" and kind != "movie":
            raise ValueError(f"Expected movie item in movies library: {item}")
        if library_type == "shows" and kind != "show":
            raise ValueError(f"Expected show item in shows library: {item}")

        src = resolve_top_level_source(root, item["source"])
        if not src.exists():
            raise FileNotFoundError(f"Source not found: {src}")
        canonical = canonical_name(item)

        if src.is_dir() and is_canonical(src.name):
            if item.get("normalize"):
                if kind == "movie":
                    plan_movie_internal(src, canonical, moves)
                else:
                    raise ValueError("Automatic show internal normalization is intentionally disabled; inspect and move episodes manually.")
            else:
                skipped.append(f"{src.name} (already canonical)")
            continue

        dst_folder = root / canonical
        if src.is_file():
            if src.suffix.lower() not in VIDEO_EXTS:
                raise ValueError(f"Top-level source file is not a video: {src}")
            add_move(moves, src, dst_folder / f"{canonical}{src.suffix}", "top-level movie file")
            continue

        add_move(moves, src, dst_folder, "top-level folder canonicalize")

    return moves, skipped


def validate_moves(moves: Iterable[Move], root: Path) -> None:
    root = root.resolve()
    moves = list(moves)
    dests: dict[Path, Path] = {}
    for move in moves:
        require_path_within_root(move.src, root, "Move source")
        require_path_within_root(move.dst, root, "Move destination")
        if move.dst in dests and dests[move.dst] != move.src:
            raise FileExistsError(f"Two sources target {move.dst}: {dests[move.dst]} and {move.src}")
        dests[move.dst] = move.src
    srcs = {m.src for m in moves}
    for move in moves:
        if move.dst.exists() and move.dst not in srcs:
            raise FileExistsError(f"Destination exists: {move.dst}")


def apply_moves(moves: list[Move], root: Path) -> None:
    root = root.resolve()
    for move in sorted(moves, key=lambda m: len(m.src.parts), reverse=True):
        # Re-check immediately before mutation so a changed symlink cannot
        # redirect an already-reviewed plan outside the library root.
        require_path_within_root(move.src, root, "Move source")
        require_path_within_root(move.dst, root, "Move destination")
        move.dst.parent.mkdir(parents=True, exist_ok=True)
        require_path_within_root(move.dst, root, "Move destination")
        unique_path(move.dst)
        shutil.move(str(move.src), str(move.dst))


def inventory(root: Path) -> None:
    entries = sorted(p for p in root.iterdir() if not p.name.startswith("."))
    for entry in entries:
        status = "canonical" if entry.is_dir() and is_canonical(entry.name) else "needs review"
        print(f"{status}\t{entry.name}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--library-type", choices=["movies", "shows"], required=False)
    parser.add_argument("--mapping", type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()

    root = args.root.resolve()
    if not root.is_dir():
        raise SystemExit(f"Root is not a directory: {root}")
    if not args.mapping:
        inventory(root)
        return 0
    if not args.library_type:
        raise SystemExit("--library-type is required when --mapping is used")

    moves, skipped = plan(root, load_items(args.mapping), args.library_type)
    validate_moves(moves, root)

    for line in skipped:
        print(f"SKIP\t{line}")
    for move in moves:
        print(f"MOVE\t{move.reason}\t{move.src}\t->\t{move.dst}")
    print(f"planned_moves={len(moves)}")
    print(f"mode={'execute' if args.execute else 'dry-run'}")

    if args.execute:
        apply_moves(moves, root)
        print(f"applied_moves={len(moves)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
