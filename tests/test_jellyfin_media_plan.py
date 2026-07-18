"""Regression tests for the Jellyfin media planner's path boundaries."""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = (
    Path(__file__).parents[1]
    / "skills"
    / "organize-jellyfin-media"
    / "scripts"
    / "jellyfin_media_plan.py"
)
SPEC = importlib.util.spec_from_file_location("jellyfin_media_plan", SCRIPT)
assert SPEC and SPEC.loader
planner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = planner
SPEC.loader.exec_module(planner)


def movie_item(source: object) -> dict[str, object]:
    return {
        "source": source,
        "type": "movie",
        "title": "Example Movie",
        "year": 2024,
        "imdbid": "tt1234567",
    }


class JellyfinMediaPlanPathTests(unittest.TestCase):
    def test_rejects_absolute_and_nested_mapping_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for source in ("/etc/passwd", "../outside.mkv", "nested/movie.mkv", r"C:\\Windows"):
                with self.subTest(source=source):
                    with self.assertRaises(ValueError):
                        planner.plan(root, [movie_item(source)], "movies")

    def test_rejects_a_symlinked_mapping_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "library"
            root.mkdir()
            outside = base / "secret.mkv"
            outside.touch()
            (root / "movie.mkv").symlink_to(outside)

            with self.assertRaisesRegex(ValueError, "must not be a symlink"):
                planner.plan(root.resolve(), [movie_item("movie.mkv")], "movies")

    def test_rejects_a_destination_redirected_by_a_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "library"
            root.mkdir()
            source = root / "old.nfo"
            source.touch()
            outside = base / "outside"
            outside.mkdir()
            (root / "other").symlink_to(outside, target_is_directory=True)
            moves = [planner.Move(source, root / "other" / "original-nfo" / "old.nfo", "test")]

            with self.assertRaisesRegex(ValueError, "destination escapes"):
                planner.validate_moves(moves, root.resolve())

    def test_apply_rechecks_destination_after_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "library"
            root.mkdir()
            source = root / "old-release.mkv"
            source.touch()
            moves, _ = planner.plan(root, [movie_item(source.name)], "movies")
            planner.validate_moves(moves, root)

            outside = base / "outside"
            outside.mkdir()
            moves[0].dst.parent.symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "destination escapes"):
                planner.apply_moves(moves, root)
            self.assertTrue(source.exists())
            self.assertEqual(list(outside.iterdir()), [])

    def test_plans_a_normal_top_level_movie(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "old-release.mkv").touch()

            moves, skipped = planner.plan(root, [movie_item("old-release.mkv")], "movies")
            planner.validate_moves(moves, root)

            self.assertEqual(skipped, [])
            self.assertEqual(len(moves), 1)
            self.assertEqual(moves[0].src, root / "old-release.mkv")
            self.assertTrue(moves[0].dst.is_relative_to(root))


if __name__ == "__main__":
    unittest.main()
