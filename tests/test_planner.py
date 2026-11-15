import itertools
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from downloads_tidier import planner
from downloads_tidier.planner import (
    format_preview,
    is_inside,
    numbered_names,
    plan_moves,
    unique_name,
)
from tests.helpers import TempFolderTestCase, make_file, snapshot


def destination_names(plan):
    """Map each source file name to the 'Category/new-name' it would get."""
    return {
        move.source.name: "%s/%s" % (move.category, move.destination.name)
        for move in plan.moves
    }


class NamingTests(unittest.TestCase):
    def test_first_name_is_the_original(self):
        self.assertEqual(next(numbered_names("report.pdf")), "report.pdf")

    def test_numbered_variants_keep_the_extension(self):
        names = list(itertools.islice(numbered_names("report.pdf"), 4))
        self.assertEqual(
            names,
            ["report.pdf", "report (1).pdf", "report (2).pdf", "report (3).pdf"],
        )

    def test_name_without_extension(self):
        names = list(itertools.islice(numbered_names("README"), 3))
        self.assertEqual(names, ["README", "README (1)", "README (2)"])

    def test_dots_inside_the_name_are_kept(self):
        self.assertEqual(
            list(itertools.islice(numbered_names("v1.2.3 notes.pdf"), 2))[1],
            "v1.2.3 notes (1).pdf",
        )

    def test_tar_dot_gz_counts_as_one_extension(self):
        self.assertEqual(
            list(itertools.islice(numbered_names("backup.tar.gz"), 2))[1],
            "backup (1).tar.gz",
        )

    def test_unique_name_returns_the_original_when_free(self):
        self.assertEqual(unique_name("a.txt", set()), "a.txt")

    def test_unique_name_skips_taken_names(self):
        taken = {"a.txt", "a (1).txt"}
        self.assertEqual(unique_name("a.txt", taken), "a (2).txt")

    def test_unique_name_ignores_upper_and_lower_case(self):
        taken = {"a.txt"}  # names in `taken` are lowercase-folded
        self.assertEqual(unique_name("A.TXT", taken), "A (1).TXT")


class IsInsideTests(TempFolderTestCase):
    def test_a_child_is_inside(self):
        self.assertTrue(is_inside(self.folder, self.folder / "Images" / "a.jpg"))

    def test_the_folder_itself_is_not_inside_itself(self):
        self.assertFalse(is_inside(self.folder, self.folder))

    def test_a_sibling_with_the_same_text_prefix_is_not_inside(self):
        sibling = Path(str(self.folder) + "-sibling") / "x"
        self.assertFalse(is_inside(self.folder, sibling))

    def test_dot_dot_cannot_escape(self):
        sneaky = self.folder / "Images" / ".." / ".." / "elsewhere"
        self.assertFalse(is_inside(self.folder, sneaky))


class PlanBasicsTests(TempFolderTestCase):
    def test_each_file_goes_to_its_category_with_the_same_name(self):
        for name in ("photo.jpg", "report.pdf", "budget.xlsx", "bundle.zip",
                     "setup.exe", "clip.mp4", "song.mp3", "mystery.xyz123"):
            make_file(self.folder, name)
        plan = plan_moves(self.folder)
        self.assertEqual(destination_names(plan), {
            "photo.jpg": "Images/photo.jpg",
            "report.pdf": "Documents/report.pdf",
            "budget.xlsx": "Spreadsheets/budget.xlsx",
            "bundle.zip": "Archives/bundle.zip",
            "setup.exe": "Installers/setup.exe",
            "clip.mp4": "Video/clip.mp4",
            "song.mp3": "Audio/song.mp3",
            "mystery.xyz123": "Other/mystery.xyz123",
        })
        self.assertFalse(any(move.renamed for move in plan.moves))

    def test_skipped_items_carry_their_reasons_through(self):
        make_file(self.folder, "photo.jpg")
        make_file(self.folder, ".hidden")
        make_file(self.folder, "movie.mp4.part")
        plan = plan_moves(self.folder)
        self.assertEqual([m.source.name for m in plan.moves], ["photo.jpg"])
        self.assertEqual([p.name for p, _ in plan.skipped],
                         [".hidden", "movie.mp4.part"])

    def test_an_empty_folder_gives_an_empty_plan(self):
        plan = plan_moves(self.folder)
        self.assertEqual(plan.moves, [])
        self.assertEqual(plan.skipped, [])

    def test_a_missing_folder_raises_a_clear_error(self):
        with self.assertRaises(NotADirectoryError):
            plan_moves(self.folder / "nope")

    def test_every_move_stays_inside_the_chosen_folder(self):
        for name in ("a.jpg", "b.pdf", "c.zip", "d", "e.mp3"):
            make_file(self.folder, name)
        plan = plan_moves(self.folder)
        self.assertTrue(plan.moves)
        for move in plan.moves:
            with self.subTest(move=move.source.name):
                self.assertEqual(move.source.parent, self.folder)
                self.assertTrue(is_inside(self.folder, move.destination))
                self.assertEqual(move.destination.parent.parent, self.folder)


class PreviewChangesNothingTests(TempFolderTestCase):
    def test_planning_and_previewing_leave_the_folder_exactly_as_it_was(self):
        for name in ("photo.jpg", "report.pdf", "setup.exe", ".hidden",
                     "movie.mp4.part", "README"):
            make_file(self.folder, name, content=name.encode())
        (self.folder / "Old Stuff").mkdir()
        make_file(self.folder / "Old Stuff", "inside.txt")

        before = snapshot(self.folder)
        plan = plan_moves(self.folder)
        format_preview(plan, self.folder)
        after = snapshot(self.folder)

        self.assertEqual(after, before)

    def test_planning_does_not_create_category_folders(self):
        make_file(self.folder, "photo.jpg")
        plan_moves(self.folder)
        self.assertEqual([p.name for p in self.folder.iterdir()], ["photo.jpg"])


class ClashTests(TempFolderTestCase):
    def test_a_name_already_in_the_category_folder_gets_a_number(self):
        (self.folder / "Images").mkdir()
        make_file(self.folder / "Images", "photo.jpg", content=b"already here")
        make_file(self.folder, "photo.jpg", content=b"new one")

        before = snapshot(self.folder)
        plan = plan_moves(self.folder)

        (move,) = plan.moves
        self.assertEqual(move.destination, self.folder / "Images" / "photo (1).jpg")
        self.assertTrue(move.renamed)
        self.assertEqual(snapshot(self.folder), before)  # existing file untouched

    def test_numbers_keep_counting_past_names_that_are_taken(self):
        (self.folder / "Images").mkdir()
        make_file(self.folder / "Images", "photo.jpg")
        make_file(self.folder / "Images", "photo (1).jpg")
        make_file(self.folder, "photo.jpg")
        (move,) = plan_moves(self.folder).moves
        self.assertEqual(move.destination.name, "photo (2).jpg")

    def test_two_files_in_one_run_never_get_the_same_destination(self):
        # "photo.jpg" clashes with the file already in Images, so it wants
        # "photo (1).jpg" - but another file being moved already owns that name.
        (self.folder / "Images").mkdir()
        make_file(self.folder / "Images", "photo.jpg")
        make_file(self.folder, "photo.jpg")
        make_file(self.folder, "photo (1).jpg")

        plan = plan_moves(self.folder)

        self.assertEqual(destination_names(plan), {
            "photo (1).jpg": "Images/photo (1).jpg",
            "photo.jpg": "Images/photo (2).jpg",
        })
        destinations = [str(m.destination).casefold() for m in plan.moves]
        self.assertEqual(len(destinations), len(set(destinations)))

    def test_a_name_differing_only_in_case_counts_as_a_clash(self):
        # On Windows and macOS these are the same file, so we must not
        # treat them as different. We play it safe on every system.
        (self.folder / "Images").mkdir()
        make_file(self.folder / "Images", "IMG.JPG")
        make_file(self.folder, "img.jpg")
        (move,) = plan_moves(self.folder).moves
        self.assertEqual(move.destination.name, "img (1).jpg")

    def test_no_destination_ever_equals_an_existing_path(self):
        (self.folder / "Documents").mkdir()
        for name in ("a.pdf", "a (1).pdf", "b.pdf"):
            make_file(self.folder / "Documents", name)
            make_file(self.folder, name)
        plan = plan_moves(self.folder)
        self.assertEqual(len(plan.moves), 3)
        for move in plan.moves:
            with self.subTest(move=move.source.name):
                self.assertFalse(os.path.lexists(move.destination))

    def test_a_folder_in_the_way_also_counts_as_taken(self):
        (self.folder / "Documents" / "report.pdf").mkdir(parents=True)
        make_file(self.folder, "report.pdf")
        (move,) = plan_moves(self.folder).moves
        self.assertEqual(move.destination.name, "report (1).pdf")


class UnsafeCategoryFolderTests(TempFolderTestCase):
    def test_a_plain_file_named_like_a_category_blocks_that_category(self):
        make_file(self.folder, "Images", content=b"I am a file, not a folder")
        make_file(self.folder, "photo.jpg")
        plan = plan_moves(self.folder)

        self.assertEqual(destination_names(plan), {"Images": "Other/Images"})
        (skipped,) = [item for item in plan.skipped if item[0].name == "photo.jpg"]
        self.assertIn("not a normal folder", skipped[1])

    def test_a_category_folder_that_is_a_symlink_is_never_used(self):
        # Pretend the OS reports "Images" as a link, so this runs everywhere.
        (self.folder / "Images").mkdir()
        make_file(self.folder, "photo.jpg")
        with mock.patch(
            "downloads_tidier.planner.os.path.islink",
            side_effect=lambda p: os.path.basename(str(p)) == "Images",
        ):
            plan = plan_moves(self.folder)
        self.assertEqual(plan.moves, [])
        self.assertIn("photo.jpg", [p.name for p, _ in plan.skipped])

    def test_a_real_symlinked_category_folder_pointing_outside_is_never_used(self):
        outside = tempfile.TemporaryDirectory(prefix="tidier-outside-")
        self.addCleanup(outside.cleanup)
        link = self.folder / "Images"
        try:
            os.symlink(outside.name, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("this system won't let tests create symlinks")
        make_file(self.folder, "photo.jpg")

        plan = plan_moves(self.folder)

        self.assertEqual(plan.moves, [])
        self.assertEqual(os.listdir(outside.name), [])

    @unittest.skipUnless(os.name == "nt", "Windows junctions")
    def test_a_windows_junction_category_folder_pointing_outside_is_never_used(self):
        # A junction is a Windows folder-link that needs no admin rights and
        # that Python does not report as a symlink - the sneaky case.
        outside = tempfile.TemporaryDirectory(prefix="tidier-outside-")
        self.addCleanup(outside.cleanup)
        link = self.folder / "Images"
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), outside.name],
            check=True, capture_output=True,
        )
        self.addCleanup(os.rmdir, str(link))  # removes only the link itself
        self.assertFalse(os.path.islink(link))  # confirms it's the sneaky kind
        make_file(self.folder, "photo.jpg")

        plan = plan_moves(self.folder)

        self.assertEqual(plan.moves, [])
        self.assertIn("photo.jpg", [p.name for p, _ in plan.skipped])
        self.assertEqual(os.listdir(outside.name), [])


class FormatPreviewTests(TempFolderTestCase):
    def test_preview_lists_moves_renames_skips_and_says_nothing_changed(self):
        (self.folder / "Images").mkdir()
        make_file(self.folder / "Images", "photo.jpg")
        make_file(self.folder, "photo.jpg")
        make_file(self.folder, "report.pdf")
        make_file(self.folder, ".hidden")

        text = format_preview(plan_moves(self.folder), self.folder)

        self.assertIn("Would move 2 files:", text)
        self.assertIn("report.pdf -> %s" % Path("Documents", "report.pdf"), text)
        self.assertIn(
            "photo.jpg -> %s  (renamed: that name was already taken)"
            % Path("Images", "photo (1).jpg"),
            text,
        )
        self.assertIn("Would skip 2 items:", text)
        self.assertIn(".hidden  (hidden file)", text)
        self.assertIn("Images  (subfolder)", text)
        self.assertTrue(text.endswith("This was a preview. Nothing was changed."))

    def test_preview_of_an_empty_folder(self):
        text = format_preview(plan_moves(self.folder), self.folder)
        self.assertIn("Nothing to move.", text)
        self.assertNotIn("Would skip", text)

    def test_singular_wording(self):
        make_file(self.folder, "report.pdf")
        text = format_preview(plan_moves(self.folder), self.folder)
        self.assertIn("Would move 1 file:", text)


if __name__ == "__main__":
    unittest.main()
