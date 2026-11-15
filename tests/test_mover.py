import json
import os
import subprocess
import tempfile
import time
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

from downloads_tidier import mover, rules
from downloads_tidier.planner import Move, Plan, plan_moves
from tests.helpers import TempFolderTestCase, make_file, snapshot


def tree(folder):
    """The set of every file path under `folder`, written with forward slashes."""
    folder = Path(folder)
    return {
        path.relative_to(folder).as_posix()
        for path in folder.rglob("*")
        if path.is_file()
    }


def file_contents(folder):
    """Count of each distinct file content under `folder`, ignoring the log."""
    return Counter(
        path.read_bytes()
        for path in Path(folder).rglob("*")
        if path.is_file() and path.name != rules.LOG_FILENAME
    )


def read_log(folder):
    text = (Path(folder) / rules.LOG_FILENAME).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines()]


class ApplyTestCase(TempFolderTestCase):
    def tidy(self):
        plan = plan_moves(self.folder)
        return plan, mover.apply_plan(self.folder, plan)

    def make_messy_folder(self):
        for name in ("photo.jpg", "report.pdf", "budget.xlsx", "setup.exe",
                     "notes", ".hidden", "Thumbs.db", "movie.mp4.crdownload",
                     "link.lnk"):
            make_file(self.folder, name, content=("content of " + name).encode())
        make_file(self.folder, "fresh.zip", content=b"fresh", age=5)
        (self.folder / "Old Stuff").mkdir()
        make_file(self.folder / "Old Stuff", "inside.pdf", content=b"inside")


class ApplyMovesFilesTests(ApplyTestCase):
    def test_apply_moves_files_into_category_folders_and_leaves_the_rest(self):
        self.make_messy_folder()
        self.tidy()
        self.assertEqual(tree(self.folder), {
            "Images/photo.jpg",
            "Documents/report.pdf",
            "Spreadsheets/budget.xlsx",
            "Installers/setup.exe",
            "Other/notes",
            # skipped, so untouched:
            ".hidden", "Thumbs.db", "movie.mp4.crdownload", "link.lnk",
            "fresh.zip", "Old Stuff/inside.pdf",
            rules.LOG_FILENAME,
        })

    def test_the_result_reports_what_happened(self):
        self.make_messy_folder()
        plan, result = self.tidy()
        self.assertIsNone(result.error)
        self.assertEqual(result.failed, [])
        self.assertEqual(
            sorted(m.source.name for m in result.moved),
            ["budget.xlsx", "notes", "photo.jpg", "report.pdf", "setup.exe"],
        )
        self.assertEqual(
            sorted(p.name for p, _ in result.skipped),
            sorted(p.name for p, _ in plan.skipped),
        )

    def test_moved_files_keep_their_contents_timestamps_and_identity(self):
        source = make_file(self.folder, "report.pdf", content=b"precious")
        before = source.stat()
        self.tidy()
        after = (self.folder / "Documents" / "report.pdf").stat()

        self.assertEqual(after.st_size, before.st_size)
        self.assertEqual(after.st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(
            (self.folder / "Documents" / "report.pdf").read_bytes(), b"precious")
        if before.st_ino:
            # Same file index = the file was renamed in place, never copied.
            self.assertEqual((after.st_dev, after.st_ino),
                             (before.st_dev, before.st_ino))

    def test_nothing_is_deleted(self):
        self.make_messy_folder()
        before = file_contents(self.folder)
        self.tidy()
        self.assertEqual(file_contents(self.folder), before)

    def test_no_empty_placeholder_files_are_left_behind(self):
        self.make_messy_folder()
        self.tidy()
        for path in self.folder.rglob("*"):
            if path.is_file():
                with self.subTest(path=path.name):
                    self.assertGreater(path.stat().st_size, 0)

    def test_applying_twice_changes_nothing_the_second_time(self):
        self.make_messy_folder()
        self.tidy()
        before = snapshot(self.folder)
        plan, result = self.tidy()
        self.assertEqual(plan.moves, [])
        self.assertEqual(result.moved, [])
        self.assertEqual(snapshot(self.folder), before)

    def test_an_empty_plan_creates_no_log_and_no_folders(self):
        make_file(self.folder, ".hidden")
        before = snapshot(self.folder)
        plan, result = self.tidy()
        self.assertEqual(result.moved, [])
        self.assertIsNone(result.run_id)
        self.assertEqual(snapshot(self.folder), before)
        self.assertFalse((self.folder / rules.LOG_FILENAME).exists())

    def test_the_module_never_uses_shutil(self):
        source = Path(mover.__file__).read_text(encoding="utf-8")
        self.assertNotIn("import shutil", source)


class NeverOverwriteTests(ApplyTestCase):
    def test_an_existing_file_with_the_same_name_is_never_overwritten(self):
        (self.folder / "Images").mkdir()
        make_file(self.folder / "Images", "photo.jpg", content=b"already here")
        make_file(self.folder, "photo.jpg", content=b"newcomer")

        self.tidy()

        self.assertEqual((self.folder / "Images" / "photo.jpg").read_bytes(),
                         b"already here")
        self.assertEqual((self.folder / "Images" / "photo (1).jpg").read_bytes(),
                         b"newcomer")

    def test_a_name_taken_after_the_preview_is_still_not_overwritten(self):
        make_file(self.folder, "report.pdf", content=b"original")
        plan = plan_moves(self.folder)  # preview says: Documents/report.pdf
        (self.folder / "Documents").mkdir()  # then, before apply, someone else...
        make_file(self.folder / "Documents", "report.pdf", content=b"race winner")

        result = mover.apply_plan(self.folder, plan)

        self.assertEqual((self.folder / "Documents" / "report.pdf").read_bytes(),
                         b"race winner")
        self.assertEqual((self.folder / "Documents" / "report (1).pdf").read_bytes(),
                         b"original")
        self.assertTrue(result.moved[0].renamed)

    def test_a_folder_that_appears_on_the_name_after_the_preview_is_stepped_over(self):
        make_file(self.folder, "report.pdf", content=b"original")
        plan = plan_moves(self.folder)
        (self.folder / "Documents" / "report.pdf").mkdir(parents=True)

        mover.apply_plan(self.folder, plan)

        self.assertTrue((self.folder / "Documents" / "report.pdf").is_dir())
        self.assertEqual((self.folder / "Documents" / "report (1).pdf").read_bytes(),
                         b"original")

    def test_a_chain_of_clashes_keeps_every_file(self):
        (self.folder / "Images").mkdir()
        make_file(self.folder / "Images", "photo.jpg", content=b"A")
        make_file(self.folder / "Images", "photo (1).jpg", content=b"B")
        make_file(self.folder, "photo.jpg", content=b"C")
        make_file(self.folder, "photo (1).jpg", content=b"D")

        self.tidy()

        in_images = {p.name: p.read_bytes()
                     for p in (self.folder / "Images").iterdir()}
        self.assertEqual(len(in_images), 4)
        self.assertEqual(sorted(in_images.values()), [b"A", b"B", b"C", b"D"])
        self.assertEqual(in_images["photo.jpg"], b"A")  # originals untouched
        self.assertEqual(in_images["photo (1).jpg"], b"B")


class ReserveAndPlaceholderTests(TempFolderTestCase):
    def test_reserve_creates_an_empty_file_when_the_name_is_free(self):
        target = self.folder / "new.txt"
        self.assertTrue(mover._reserve(target))
        self.assertEqual(target.read_bytes(), b"")

    def test_reserve_refuses_an_existing_file_without_touching_it(self):
        target = make_file(self.folder, "taken.txt", content=b"mine")
        self.assertFalse(mover._reserve(target))
        self.assertEqual(target.read_bytes(), b"mine")

    def test_reserve_refuses_an_existing_folder(self):
        (self.folder / "taken").mkdir()
        self.assertFalse(mover._reserve(self.folder / "taken"))

    def test_only_an_empty_regular_file_is_ever_removed(self):
        empty = self.folder / "empty"
        empty.write_bytes(b"")
        full = make_file(self.folder, "full", content=b"data")
        folder = self.folder / "a-folder"
        folder.mkdir()

        self.assertFalse(mover._remove_placeholder(full))
        self.assertFalse(mover._remove_placeholder(folder))
        self.assertFalse(mover._remove_placeholder(self.folder / "missing"))
        self.assertTrue(full.exists() and folder.exists())

        self.assertTrue(mover._remove_placeholder(empty))
        self.assertFalse(empty.exists())


class FailureHandlingTests(ApplyTestCase):
    def test_a_file_that_cannot_be_moved_is_reported_and_the_rest_still_move(self):
        make_file(self.folder, "locked.pdf", content=b"locked data")
        make_file(self.folder, "fine.pdf", content=b"fine data")
        make_file(self.folder, "photo.jpg", content=b"photo data")
        real_replace = os.replace

        def fake_replace(source, destination, *args, **kwargs):
            if Path(source).name == "locked.pdf":
                raise PermissionError(13, "Permission denied")
            return real_replace(source, destination, *args, **kwargs)

        plan = plan_moves(self.folder)
        with mock.patch("downloads_tidier.mover.os.replace",
                        side_effect=fake_replace):
            result = mover.apply_plan(self.folder, plan)

        self.assertIsNone(result.error)
        self.assertEqual([p.name for p, _ in result.failed], ["locked.pdf"])
        self.assertIn("another program", result.failed[0][1])
        self.assertEqual(sorted(m.source.name for m in result.moved),
                         ["fine.pdf", "photo.jpg"])
        # The locked file is exactly where it was, and no placeholder is left.
        self.assertEqual((self.folder / "locked.pdf").read_bytes(), b"locked data")
        self.assertEqual(os.listdir(self.folder / "Documents"), ["fine.pdf"])
        # The log only records what really moved.
        moves = [r for r in read_log(self.folder) if r["event"] == "move"]
        self.assertEqual({m["from"] for m in moves}, {"fine.pdf", "photo.jpg"})
        end = read_log(self.folder)[-1]
        self.assertEqual((end["event"], end["moved"], end["failed"]),
                         ("run_end", 2, 1))

    @unittest.skipUnless(os.name == "nt", "Windows file locking")
    def test_a_file_open_in_another_program_on_windows_is_skipped_not_a_crash(self):
        locked = make_file(self.folder, "open-in-word.pdf", content=b"being edited")
        make_file(self.folder, "fine.pdf", content=b"fine")
        plan = plan_moves(self.folder)

        with open(locked, "rb"):  # holding it open, like another program would
            result = mover.apply_plan(self.folder, plan)

        self.assertEqual([p.name for p, _ in result.failed], ["open-in-word.pdf"])
        self.assertEqual(locked.read_bytes(), b"being edited")
        self.assertEqual(os.listdir(self.folder / "Documents"), ["fine.pdf"])

    def test_a_file_that_changed_since_the_preview_is_skipped(self):
        source = make_file(self.folder, "report.pdf", content=b"x")
        plan = plan_moves(self.folder)
        now = time.time()
        os.utime(source, (now, now))  # someone just edited it

        result = mover.apply_plan(self.folder, plan)

        self.assertEqual(result.moved, [])
        self.assertEqual(result.skipped[0][1],
                         "no longer safe to move (changed in the last 10 minutes)")
        self.assertTrue(source.exists())

    def test_a_file_that_vanished_since_the_preview_is_skipped(self):
        source = make_file(self.folder, "report.pdf")
        make_file(self.folder, "photo.jpg")
        plan = plan_moves(self.folder)
        source.unlink()  # the test removes its own temp file

        result = mover.apply_plan(self.folder, plan)

        self.assertEqual([m.source.name for m in result.moved], ["photo.jpg"])
        self.assertEqual([p.name for p, _ in result.skipped], ["report.pdf"])


class StaysInsideTheFolderTests(ApplyTestCase):
    def test_nothing_outside_the_chosen_folder_is_touched(self):
        target = self.folder / "tidy-me"
        bystander = self.folder / "bystander"
        target.mkdir()
        bystander.mkdir()
        make_file(bystander, "keep.pdf", content=b"not yours")
        make_file(self.folder, "top-level.pdf", content=b"not yours either")
        for name in ("a.pdf", "b.jpg", "c.zip", "d"):
            make_file(target, name)
        before_bystander = snapshot(bystander)
        before_top = (self.folder / "top-level.pdf").read_bytes()

        mover.apply_plan(target, plan_moves(target))

        self.assertEqual(snapshot(bystander), before_bystander)
        self.assertEqual((self.folder / "top-level.pdf").read_bytes(), before_top)
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()),
                         ["bystander", "tidy-me", "top-level.pdf"])

    def test_a_crafted_plan_cannot_move_a_file_from_outside_the_folder(self):
        outside = tempfile.TemporaryDirectory(prefix="tidier-outside-")
        self.addCleanup(outside.cleanup)
        stray = make_file(Path(outside.name), "stray.pdf", content=b"elsewhere")
        plan = Plan([Move(stray, self.folder / "Documents" / "stray.pdf",
                          "Documents", False)], [])

        result = mover.apply_plan(self.folder, plan)

        self.assertEqual(result.moved, [])
        self.assertIn("refused", result.skipped[0][1])
        self.assertEqual(stray.read_bytes(), b"elsewhere")
        self.assertFalse((self.folder / "Documents").exists())

    def test_a_crafted_plan_cannot_send_a_file_outside_the_folder(self):
        outside = tempfile.TemporaryDirectory(prefix="tidier-outside-")
        self.addCleanup(outside.cleanup)
        source = make_file(self.folder, "a.pdf", content=b"stay home")
        plan = Plan([Move(source, Path(outside.name) / "a.pdf", "Documents", False)], [])

        mover.apply_plan(self.folder, plan)

        self.assertEqual(os.listdir(outside.name), [])
        self.assertEqual((self.folder / "Documents" / "a.pdf").read_bytes(),
                         b"stay home")

    def test_a_crafted_category_that_climbs_out_is_refused(self):
        sub = self.folder / "inner"
        sub.mkdir()
        source = make_file(sub, "a.pdf", content=b"x")
        plan = Plan([Move(source, sub / ".." / "evil" / "a.pdf", "../evil", False)], [])

        result = mover.apply_plan(sub, plan)

        self.assertEqual(result.moved, [])
        self.assertFalse((self.folder / "evil").exists())
        self.assertTrue(source.exists())

    def test_a_category_folder_replaced_by_a_file_after_the_preview_is_not_used(self):
        source = make_file(self.folder, "photo.jpg", content=b"x")
        plan = plan_moves(self.folder)
        (self.folder / "Images").write_bytes(b"I am a file now")

        result = mover.apply_plan(self.folder, plan)

        self.assertEqual(result.moved, [])
        self.assertIn("not a normal folder", result.skipped[0][1])
        self.assertTrue(source.exists())

    def test_a_category_folder_that_becomes_a_symlink_is_not_used(self):
        source = make_file(self.folder, "photo.jpg", content=b"x")
        (self.folder / "Images").mkdir()
        plan = plan_moves(self.folder)
        # Pretend the OS now reports "Images" as a link (works on every system).
        with mock.patch(
            "downloads_tidier.mover.os.path.islink",
            side_effect=lambda p: os.path.basename(str(p)) == "Images",
        ):
            result = mover.apply_plan(self.folder, plan)
        self.assertEqual(result.moved, [])
        self.assertTrue(source.exists())

    @unittest.skipUnless(os.name == "nt", "Windows junctions")
    def test_a_junction_created_after_the_preview_is_not_followed(self):
        outside = tempfile.TemporaryDirectory(prefix="tidier-outside-")
        self.addCleanup(outside.cleanup)
        source = make_file(self.folder, "photo.jpg", content=b"x")
        plan = plan_moves(self.folder)  # "Images" doesn't exist yet
        link = self.folder / "Images"
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), outside.name],
                       check=True, capture_output=True)
        self.addCleanup(os.rmdir, str(link))

        result = mover.apply_plan(self.folder, plan)

        self.assertEqual(result.moved, [])
        self.assertTrue(source.exists())
        self.assertEqual(os.listdir(outside.name), [])


class LogTests(ApplyTestCase):
    def test_the_log_records_the_run_and_every_move(self):
        make_file(self.folder, "report.pdf")
        make_file(self.folder, "photo.jpg")
        (self.folder / "Images").mkdir()
        make_file(self.folder / "Images", "photo.jpg")

        plan, result = self.tidy()
        records = read_log(self.folder)

        self.assertEqual([r["event"] for r in records],
                         ["run_start", "move", "move", "run_end"])
        self.assertEqual({r["run"] for r in records}, {result.run_id})
        moves = {r["from"]: r["to"] for r in records if r["event"] == "move"}
        self.assertEqual(moves, {
            "report.pdf": "Documents/report.pdf",
            "photo.jpg": "Images/photo (1).jpg",
        })
        self.assertEqual((records[-1]["moved"], records[-1]["failed"]), (2, 0))

    def test_a_second_apply_adds_to_the_log_without_changing_what_was_there(self):
        make_file(self.folder, "one.pdf")
        self.tidy()
        first = (self.folder / rules.LOG_FILENAME).read_bytes()
        make_file(self.folder, "two.pdf")
        self.tidy()
        second = (self.folder / rules.LOG_FILENAME).read_bytes()

        self.assertTrue(second.startswith(first))
        self.assertGreater(len(second), len(first))
        runs = {r["run"] for r in read_log(self.folder)}
        self.assertEqual(len(runs), 2)

    def test_the_log_holds_no_absolute_paths_or_personal_details(self):
        make_file(self.folder, "report.pdf")
        self.tidy()
        text = (self.folder / rules.LOG_FILENAME).read_text(encoding="utf-8")
        self.assertNotIn(str(self.folder), text)
        self.assertNotIn(os.path.expanduser("~"), text)

    def test_the_log_is_never_moved_by_a_later_run(self):
        make_file(self.folder, "report.pdf")
        self.tidy()
        plan = plan_moves(self.folder)
        self.assertIn(rules.LOG_FILENAME, [p.name for p, _ in plan.skipped])

    def test_if_the_log_cannot_be_opened_nothing_is_moved(self):
        (self.folder / rules.LOG_FILENAME).mkdir()  # a folder in the log's way
        make_file(self.folder, "report.pdf")
        make_file(self.folder, "photo.jpg")
        before = snapshot(self.folder)

        plan, result = self.tidy()

        self.assertIn("nothing was moved", result.error)
        self.assertEqual(result.moved, [])
        self.assertEqual(snapshot(self.folder), before)

    def test_if_the_log_fails_mid_run_it_stops_before_moving_more(self):
        for name in ("a.pdf", "b.pdf", "c.pdf"):
            make_file(self.folder, name, content=name.encode())
        real_write = mover._Log.write
        calls = []

        def flaky_write(self_, record):
            calls.append(record["event"])
            if len(calls) == 3:  # run_start, move a.pdf, then fail on b.pdf
                raise OSError(28, "No space left on device")
            return real_write(self_, record)

        plan = plan_moves(self.folder)
        with mock.patch.object(mover._Log, "write", autospec=True,
                               side_effect=flaky_write):
            result = mover.apply_plan(self.folder, plan)

        self.assertIn("b.pdf", result.error)
        self.assertIn("no further files were moved", result.error)
        self.assertTrue((self.folder / "c.pdf").exists())  # never touched
        self.assertEqual(sorted(m.source.name for m in result.moved),
                         ["a.pdf", "b.pdf"])  # b really did move; we say so


class FormatResultTests(ApplyTestCase):
    def test_report_lists_moves_renames_failures_and_skips(self):
        (self.folder / "Images").mkdir()
        make_file(self.folder / "Images", "photo.jpg")
        make_file(self.folder, "photo.jpg")
        make_file(self.folder, "report.pdf")
        make_file(self.folder, ".hidden")
        plan, result = self.tidy()
        failed = result._replace(failed=[(self.folder / "stuck.pdf", "in use")])

        text = mover.format_result(failed, self.folder)

        self.assertIn("Moved 2 files:", text)
        self.assertIn("report.pdf -> %s" % Path("Documents", "report.pdf"), text)
        self.assertIn("(renamed: that name was already taken)", text)
        self.assertIn("Could not move 1 file (left where they were):", text)
        self.assertIn("stuck.pdf  (in use)", text)
        self.assertIn(".hidden  (hidden file)", text)

    def test_report_for_nothing_moved(self):
        text = mover.format_result(self.tidy()[1], self.folder)
        self.assertEqual(text, "Nothing was moved.")

    def test_report_shows_a_stop_message(self):
        result = mover.ApplyResult(None, [], [], [], "the log broke")
        self.assertIn("STOPPED: the log broke",
                      mover.format_result(result, self.folder))


if __name__ == "__main__":
    unittest.main()
