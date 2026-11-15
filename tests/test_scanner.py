import os
import time
import unittest
from unittest import mock

from downloads_tidier import rules, scanner
from downloads_tidier.scanner import scan_folder, skip_reason
from tests.helpers import (
    TempFolderTestCase,
    make_file,
    set_windows_attributes,
    snapshot,
)


class OrdinaryFileTests(TempFolderTestCase):
    def test_an_old_regular_file_is_safe_to_move(self):
        path = make_file(self.folder, "report.pdf")
        self.assertIsNone(skip_reason(path))

    def test_files_with_no_extension_or_odd_names_are_safe_to_move(self):
        for name in ("README", "my notes (final).txt", "v1.2.3.bin"):
            with self.subTest(name=name):
                path = make_file(self.folder, name)
                self.assertIsNone(skip_reason(path))


class FolderAndLinkTests(TempFolderTestCase):
    def test_subfolders_are_skipped(self):
        (self.folder / "Photos").mkdir()
        self.assertEqual(skip_reason(self.folder / "Photos"), scanner.SUBFOLDER)

    def test_a_folder_with_a_file_like_name_is_still_a_subfolder(self):
        (self.folder / "looks-like-a-file.pdf").mkdir()
        self.assertEqual(
            skip_reason(self.folder / "looks-like-a-file.pdf"), scanner.SUBFOLDER
        )

    def test_real_file_symlink_is_skipped(self):
        target = make_file(self.folder, "target.txt")
        link = self.folder / "link.txt"
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError):
            self.skipTest("this system won't let tests create symlinks")
        self.assertEqual(skip_reason(link), scanner.SYMLINK)

    def test_real_folder_symlink_is_skipped(self):
        target = self.folder / "real-folder"
        target.mkdir()
        link = self.folder / "link-to-folder"
        try:
            os.symlink(target, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("this system won't let tests create symlinks")
        self.assertEqual(skip_reason(link), scanner.SYMLINK)

    def test_symlink_check_happens_first_even_for_an_ordinary_file(self):
        # Works on every system, including ones that forbid real symlinks:
        # pretend the OS says "this is a link" and check we skip it.
        path = make_file(self.folder, "pretend-link.txt")
        with mock.patch("downloads_tidier.scanner.os.path.islink",
                        return_value=True):
            self.assertEqual(skip_reason(path), scanner.SYMLINK)

    def test_a_missing_path_is_skipped_not_a_crash(self):
        self.assertEqual(
            skip_reason(self.folder / "does-not-exist.txt"), scanner.UNREADABLE
        )


class ShortcutTests(TempFolderTestCase):
    def test_shortcut_files_are_skipped(self):
        for name in ("App.lnk", "Site.url", "Link.webloc", "Launcher.desktop"):
            with self.subTest(name=name):
                path = make_file(self.folder, name)
                self.assertEqual(skip_reason(path), scanner.SHORTCUT)

    def test_shortcut_matching_ignores_case(self):
        path = make_file(self.folder, "APP.LNK")
        self.assertEqual(skip_reason(path), scanner.SHORTCUT)


class HiddenAndSystemTests(TempFolderTestCase):
    def test_files_starting_with_a_dot_are_hidden(self):
        for name in (".hidden", ".gitignore", ".env"):
            with self.subTest(name=name):
                path = make_file(self.folder, name)
                self.assertEqual(skip_reason(path), scanner.HIDDEN)

    def test_known_system_files_are_skipped_whatever_their_case(self):
        for name in ("desktop.ini", "Desktop.ini", "Thumbs.db", "thumbs.db",
                     ".DS_Store", "ehthumbs.db"):
            with self.subTest(name=name):
                path = make_file(self.folder, name)
                self.assertEqual(skip_reason(path), scanner.SYSTEM)

    def test_the_tools_own_log_is_skipped(self):
        path = make_file(self.folder, rules.LOG_FILENAME)
        self.assertEqual(skip_reason(path), scanner.OWN_LOG)

    @unittest.skipUnless(os.name == "nt", "Windows hidden attribute")
    def test_windows_hidden_attribute_is_respected(self):
        path = make_file(self.folder, "plain-name.txt")
        set_windows_attributes(path, hidden=True)
        self.assertEqual(skip_reason(path), scanner.HIDDEN)

    @unittest.skipUnless(os.name == "nt", "Windows system attribute")
    def test_windows_system_attribute_is_respected(self):
        path = make_file(self.folder, "plain-name.txt")
        set_windows_attributes(path, system=True)
        self.assertEqual(skip_reason(path), scanner.SYSTEM)


class PartialDownloadTests(TempFolderTestCase):
    def test_partial_download_files_are_skipped(self):
        for name in ("movie.mp4.crdownload", "big.iso.part",
                     "file.zip.download", "scratch.tmp"):
            with self.subTest(name=name):
                path = make_file(self.folder, name)
                self.assertEqual(skip_reason(path), scanner.PARTIAL)

    def test_partial_matching_ignores_case(self):
        path = make_file(self.folder, "Movie.MP4.CRDOWNLOAD")
        self.assertEqual(skip_reason(path), scanner.PARTIAL)

    def test_a_name_that_merely_contains_part_is_not_partial(self):
        path = make_file(self.folder, "part-one.pdf")
        self.assertIsNone(skip_reason(path))


class RecentFileTests(TempFolderTestCase):
    def test_a_file_changed_a_minute_ago_is_skipped(self):
        now = time.time()
        path = make_file(self.folder, "fresh.pdf", age=60, now=now)
        self.assertEqual(skip_reason(path, now=now), scanner.RECENT)

    def test_just_inside_and_just_outside_the_ten_minute_limit(self):
        stamp = 1_700_000_000
        path = make_file(self.folder, "edge.pdf", age=0, now=stamp)
        self.assertEqual(skip_reason(path, now=stamp + 599), scanner.RECENT)
        self.assertIsNone(skip_reason(path, now=stamp + 601))

    def test_a_file_with_a_modified_time_in_the_future_is_skipped(self):
        now = time.time()
        path = make_file(self.folder, "future.pdf", age=-3600, now=now)
        self.assertEqual(skip_reason(path, now=now), scanner.RECENT)

    def test_an_hour_old_file_is_fine(self):
        now = time.time()
        path = make_file(self.folder, "old.pdf", age=3600, now=now)
        self.assertIsNone(skip_reason(path, now=now))


class ScanFolderTests(TempFolderTestCase):
    def build_messy_folder(self):
        make_file(self.folder, "photo.jpg")
        make_file(self.folder, "report.pdf")
        make_file(self.folder, "README")
        make_file(self.folder, ".hidden")
        make_file(self.folder, "Thumbs.db")
        make_file(self.folder, "setup.exe.crdownload")
        make_file(self.folder, "shortcut.lnk")
        make_file(self.folder, "fresh.txt", age=5)
        (self.folder / "Old Stuff").mkdir()
        make_file(self.folder / "Old Stuff", "inside.pdf")

    def test_splits_a_messy_folder_into_candidates_and_skipped(self):
        self.build_messy_folder()
        result = scan_folder(self.folder)

        self.assertEqual(
            [p.name for p in result.candidates],
            ["README", "photo.jpg", "report.pdf"],
        )
        reasons = {p.name: reason for p, reason in result.skipped}
        self.assertEqual(reasons, {
            ".hidden": scanner.HIDDEN,
            "Old Stuff": scanner.SUBFOLDER,
            "Thumbs.db": scanner.SYSTEM,
            "fresh.txt": scanner.RECENT,
            "setup.exe.crdownload": scanner.PARTIAL,
            "shortcut.lnk": scanner.SHORTCUT,
        })

    def test_every_item_is_either_a_candidate_or_skipped_never_both(self):
        self.build_messy_folder()
        result = scan_folder(self.folder)
        candidate_names = {p.name for p in result.candidates}
        skipped_names = {p.name for p, _ in result.skipped}
        self.assertFalse(candidate_names & skipped_names)
        top_level = {p.name for p in self.folder.iterdir()}
        self.assertEqual(candidate_names | skipped_names, top_level)

    def test_never_looks_inside_subfolders(self):
        self.build_messy_folder()
        result = scan_folder(self.folder)
        everything = [p for p in result.candidates] + [p for p, _ in result.skipped]
        for path in everything:
            self.assertEqual(path.parent, self.folder)
        self.assertNotIn("inside.pdf", [p.name for p in everything])

    def test_scanning_changes_nothing(self):
        self.build_messy_folder()
        before = snapshot(self.folder)
        scan_folder(self.folder)
        self.assertEqual(snapshot(self.folder), before)

    def test_an_empty_folder_gives_empty_results(self):
        result = scan_folder(self.folder)
        self.assertEqual(result.candidates, [])
        self.assertEqual(result.skipped, [])

    def test_results_come_in_a_stable_order(self):
        for name in ("c.txt", "a.txt", "b.txt"):
            make_file(self.folder, name)
        names = [p.name for p in scan_folder(self.folder).candidates]
        self.assertEqual(names, ["a.txt", "b.txt", "c.txt"])

    def test_a_missing_folder_raises_a_clear_error(self):
        with self.assertRaises(NotADirectoryError):
            scan_folder(self.folder / "nope")

    def test_a_file_given_instead_of_a_folder_raises_a_clear_error(self):
        path = make_file(self.folder, "file.txt")
        with self.assertRaises(NotADirectoryError):
            scan_folder(path)


if __name__ == "__main__":
    unittest.main()
