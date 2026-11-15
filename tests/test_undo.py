import json
import os
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from downloads_tidier import mover, rules
from downloads_tidier.planner import plan_moves
from tests.helpers import (
    TempFolderTestCase,
    file_contents,
    make_file,
    read_log,
    snapshot,
    tree,
)


def files_only(snap):
    """Keep just the file entries of a snapshot, without the undo log."""
    return {key: value for key, value in snap.items()
            if value[0] == "file" and key != rules.LOG_FILENAME}


class UndoTestCase(TempFolderTestCase):
    def tidy(self):
        return mover.apply_plan(self.folder, plan_moves(self.folder))

    def make_messy_folder(self):
        (self.folder / "Images").mkdir()
        make_file(self.folder / "Images", "photo.jpg", content=b"was already here")
        for name in ("photo.jpg", "report.pdf", "budget.xlsx", "setup.exe",
                     "notes", ".hidden", "Thumbs.db", "movie.mp4.crdownload",
                     "link.lnk"):
            make_file(self.folder, name, content=("content of " + name).encode())
        make_file(self.folder, "fresh.zip", content=b"fresh", age=5)
        (self.folder / "Old Stuff").mkdir()
        make_file(self.folder / "Old Stuff", "inside.pdf", content=b"inside")


class UndoRestoresTests(UndoTestCase):
    def test_apply_then_undo_restores_everything_exactly(self):
        self.make_messy_folder()
        before = files_only(snapshot(self.folder))

        applied = self.tidy()
        self.assertTrue(applied.moved)
        result = mover.undo_last(self.folder)

        self.assertIsNone(result.error)
        self.assertEqual(result.skipped + result.failed + result.missing, [])
        # Same names, sizes, modified times and contents as before apply.
        self.assertEqual(files_only(snapshot(self.folder)), before)

    def test_the_result_lists_what_was_put_back(self):
        make_file(self.folder, "report.pdf")
        make_file(self.folder, "photo.jpg")
        applied = self.tidy()
        result = mover.undo_last(self.folder)
        self.assertEqual(result.run_id, applied.run_id)
        self.assertEqual(sorted(result.restored), [
            ("photo.jpg", "Images/photo.jpg"),
            ("report.pdf", "Documents/report.pdf"),
        ])

    def test_empty_category_folders_are_left_in_place(self):
        make_file(self.folder, "report.pdf")
        make_file(self.folder, "photo.jpg")
        self.tidy()
        mover.undo_last(self.folder)
        for category in ("Documents", "Images"):
            with self.subTest(category=category):
                self.assertTrue((self.folder / category).is_dir())
                self.assertEqual(os.listdir(self.folder / category), [])

    def test_undo_never_deletes_anything(self):
        self.make_messy_folder()
        before = file_contents(self.folder)
        self.tidy()
        mover.undo_last(self.folder)
        self.assertEqual(file_contents(self.folder), before)

    def test_undo_only_touches_the_chosen_folder(self):
        target = self.folder / "tidy-me"
        bystander = self.folder / "bystander"
        target.mkdir()
        bystander.mkdir()
        make_file(bystander, "report.pdf", content=b"not yours")
        make_file(self.folder, "report.pdf", content=b"not yours either")
        make_file(target, "report.pdf", content=b"mine")
        before_bystander = snapshot(bystander)
        before_top = (self.folder / "report.pdf").read_bytes()

        mover.apply_plan(target, plan_moves(target))
        mover.undo_last(target)

        self.assertEqual(snapshot(bystander), before_bystander)
        self.assertEqual((self.folder / "report.pdf").read_bytes(), before_top)
        self.assertEqual((target / "report.pdf").read_bytes(), b"mine")
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()),
                         ["bystander", "report.pdf", "tidy-me"])

    def test_with_no_log_there_is_nothing_to_undo(self):
        make_file(self.folder, "report.pdf")
        before = snapshot(self.folder)
        result = mover.undo_last(self.folder)
        self.assertIsNone(result.run_id)
        self.assertIsNone(result.error)
        self.assertEqual(snapshot(self.folder), before)
        self.assertFalse((self.folder / rules.LOG_FILENAME).exists())

    def test_undoing_a_single_apply_twice_does_nothing_the_second_time(self):
        make_file(self.folder, "report.pdf")
        self.tidy()
        mover.undo_last(self.folder)
        before = snapshot(self.folder)
        log_before = (self.folder / rules.LOG_FILENAME).read_bytes()

        result = mover.undo_last(self.folder)

        self.assertIsNone(result.run_id)
        self.assertEqual(snapshot(self.folder), before)
        self.assertEqual((self.folder / rules.LOG_FILENAME).read_bytes(), log_before)


class SeveralRunsTests(UndoTestCase):
    def test_each_undo_reverses_one_apply_newest_first(self):
        make_file(self.folder, "first.pdf", content=b"1")
        first = self.tidy()
        make_file(self.folder, "second.pdf", content=b"2")
        second = self.tidy()

        undo_two = mover.undo_last(self.folder)
        self.assertEqual(undo_two.run_id, second.run_id)
        self.assertEqual(tree(self.folder) - {rules.LOG_FILENAME},
                         {"second.pdf", "Documents/first.pdf"})

        undo_one = mover.undo_last(self.folder)
        self.assertEqual(undo_one.run_id, first.run_id)
        self.assertEqual(tree(self.folder) - {rules.LOG_FILENAME},
                         {"second.pdf", "first.pdf"})

        self.assertIsNone(mover.undo_last(self.folder).run_id)

    def test_an_apply_that_moved_nothing_is_stepped_over(self):
        make_file(self.folder, "first.pdf")
        first = self.tidy()
        busy = make_file(self.folder, "busy.pdf")
        plan = plan_moves(self.folder)
        now = time.time()
        os.utime(busy, (now, now))  # changed after the preview, so apply skips it
        empty_run = mover.apply_plan(self.folder, plan)
        self.assertEqual(empty_run.moved, [])
        self.assertIsNotNone(empty_run.run_id)

        result = mover.undo_last(self.folder)

        self.assertEqual(result.run_id, first.run_id)
        self.assertTrue((self.folder / "first.pdf").exists())


class UndoProblemTests(UndoTestCase):
    def test_a_taken_original_name_is_skipped_and_retried_later(self):
        make_file(self.folder, "report.pdf", content=b"original report")
        make_file(self.folder, "photo.jpg", content=b"photo")
        self.tidy()
        newcomer = make_file(self.folder, "report.pdf", content=b"a brand new file")

        result = mover.undo_last(self.folder)

        self.assertEqual([name for name, _ in result.restored], ["photo.jpg"])
        self.assertEqual([path for path, _ in result.skipped],
                         ["Documents/report.pdf"])
        self.assertIn("already taken", result.skipped[0][1])
        self.assertEqual(newcomer.read_bytes(), b"a brand new file")  # not overwritten
        self.assertEqual((self.folder / "Documents" / "report.pdf").read_bytes(),
                         b"original report")  # not lost

        newcomer.unlink()  # the user sorts the clash out themselves...
        retry = mover.undo_last(self.folder)  # ...and runs undo again

        self.assertEqual(retry.restored, [("report.pdf", "Documents/report.pdf")])
        self.assertEqual((self.folder / "report.pdf").read_bytes(), b"original report")
        self.assertIsNone(mover.undo_last(self.folder).run_id)

    def test_a_folder_sitting_on_the_original_name_is_not_overwritten(self):
        make_file(self.folder, "report.pdf", content=b"original")
        self.tidy()
        (self.folder / "report.pdf").mkdir()

        result = mover.undo_last(self.folder)

        self.assertEqual(result.restored, [])
        self.assertIn("already taken", result.skipped[0][1])
        self.assertTrue((self.folder / "report.pdf").is_dir())
        self.assertEqual((self.folder / "Documents" / "report.pdf").read_bytes(),
                         b"original")

    def test_a_file_that_is_already_gone_is_reported_not_an_error(self):
        make_file(self.folder, "report.pdf")
        make_file(self.folder, "photo.jpg", content=b"photo")
        self.tidy()
        os.remove(self.folder / "Documents" / "report.pdf")  # the user's own doing

        result = mover.undo_last(self.folder)

        self.assertIsNone(result.error)
        self.assertEqual(result.missing, ["Documents/report.pdf"])
        self.assertEqual([name for name, _ in result.restored], ["photo.jpg"])
        self.assertTrue((self.folder / "photo.jpg").exists())
        self.assertIsNone(mover.undo_last(self.folder).run_id)  # run is closed

    def test_a_file_the_os_refuses_to_move_is_reported_and_can_be_retried(self):
        make_file(self.folder, "report.pdf", content=b"report data")
        make_file(self.folder, "photo.jpg", content=b"photo data")
        self.tidy()
        real_replace = os.replace

        def fake_replace(source, destination, *args, **kwargs):
            if Path(source).name == "report.pdf":
                raise PermissionError(13, "Permission denied")
            return real_replace(source, destination, *args, **kwargs)

        with mock.patch("downloads_tidier.mover.os.replace", side_effect=fake_replace):
            result = mover.undo_last(self.folder)

        self.assertEqual([path for path, _ in result.failed], ["Documents/report.pdf"])
        self.assertEqual([n for n, _ in result.restored], ["photo.jpg"])
        self.assertEqual((self.folder / "Documents" / "report.pdf").read_bytes(),
                         b"report data")
        self.assertFalse((self.folder / "report.pdf").exists())  # no placeholder left

        retry = mover.undo_last(self.folder)
        self.assertEqual([n for n, _ in retry.restored], ["report.pdf"])

    @unittest.skipUnless(os.name == "nt", "Windows file locking")
    def test_a_file_open_in_another_program_on_windows_is_left_alone(self):
        make_file(self.folder, "open-in-word.pdf", content=b"being edited")
        self.tidy()
        held = self.folder / "Documents" / "open-in-word.pdf"

        with open(held, "rb"):
            result = mover.undo_last(self.folder)

        self.assertEqual([p for p, _ in result.failed], ["Documents/open-in-word.pdf"])
        self.assertEqual(held.read_bytes(), b"being edited")
        self.assertFalse((self.folder / "open-in-word.pdf").exists())

        retry = mover.undo_last(self.folder)  # once it's closed, undo works
        self.assertEqual(len(retry.restored), 1)

    def test_a_category_folder_that_became_a_symlink_is_not_used(self):
        make_file(self.folder, "report.pdf", content=b"x")
        self.tidy()
        with mock.patch(
            "downloads_tidier.mover.os.path.islink",
            side_effect=lambda p: os.path.basename(str(p)) == "Documents",
        ):
            result = mover.undo_last(self.folder)
        self.assertEqual(result.restored, [])
        self.assertIn("not a normal folder", result.skipped[0][1])
        self.assertTrue((self.folder / "Documents" / "report.pdf").exists())

    @unittest.skipUnless(os.name == "nt", "Windows junctions")
    def test_a_category_folder_swapped_for_a_junction_never_pulls_in_outside_files(self):
        outside = tempfile.TemporaryDirectory(prefix="tidier-outside-")
        self.addCleanup(outside.cleanup)
        make_file(Path(outside.name), "report.pdf", content=b"someone else's file")
        make_file(self.folder, "report.pdf", content=b"mine")
        self.tidy()
        os.rename(self.folder / "Documents", self.folder / "Documents-moved-away")
        link = self.folder / "Documents"
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), outside.name],
                       check=True, capture_output=True)
        self.addCleanup(os.rmdir, str(link))

        result = mover.undo_last(self.folder)

        self.assertEqual(result.restored, [])
        self.assertIn("not a normal folder", result.skipped[0][1])
        self.assertFalse((self.folder / "report.pdf").exists())
        self.assertEqual(os.listdir(outside.name), ["report.pdf"])
        self.assertEqual((Path(outside.name) / "report.pdf").read_bytes(),
                         b"someone else's file")


class TamperedLogTests(TempFolderTestCase):
    def test_log_entries_that_point_outside_the_folder_are_refused(self):
        inner = self.folder / "inner"
        (inner / "Documents").mkdir(parents=True)
        (inner / "NotACategory").mkdir()
        for path in (inner / "Documents" / "a.pdf",
                     inner / "Documents" / "e.pdf",
                     inner / "NotACategory" / "d.pdf"):
            path.write_bytes(b"data")
        victim = make_file(self.folder, "outside.pdf", content=b"outside the folder")
        absolute = str(self.folder / "escaped.pdf")

        entries = [
            ("../escaped.pdf", "Documents/a.pdf"),             # restore outside
            ("..", "Documents/a.pdf"),
            ("b.pdf", "../outside.pdf"),                       # pull in from outside
            ("c.pdf", "Documents/../../outside.pdf"),
            ("c.pdf", "/outside.pdf"),
            ("d.pdf", "NotACategory/d.pdf"),                   # not our folder
            ("sub/e.pdf", "Documents/e.pdf"),                  # folder part in name
            (absolute, "Documents/a.pdf"),                     # absolute path
        ]
        with open(inner / rules.LOG_FILENAME, "w", encoding="utf-8") as log:
            for original, moved in entries:
                log.write(json.dumps({"event": "move", "run": "evil",
                                      "from": original, "to": moved}) + "\n")
        before = snapshot(self.folder)
        before.pop("inner/" + rules.LOG_FILENAME)

        result = mover.undo_last(inner)

        self.assertEqual(result.restored, [])
        self.assertEqual(result.missing, [])
        self.assertEqual(len(result.skipped), len(entries))
        for _, reason in result.skipped:
            self.assertIn("refused", reason)
        after = snapshot(self.folder)
        after.pop("inner/" + rules.LOG_FILENAME)
        self.assertEqual(after, before)
        self.assertEqual(victim.read_bytes(), b"outside the folder")
        self.assertFalse((self.folder / "escaped.pdf").exists())

    def test_garbage_lines_in_the_log_are_ignored(self):
        make_file(self.folder, "report.pdf", content=b"data")
        mover.apply_plan(self.folder, plan_moves(self.folder))
        with open(self.folder / rules.LOG_FILENAME, "a", encoding="utf-8") as log:
            log.write("this is not json\n[1, 2, 3]\n\"just a string\"\n{\"event\": 5}\n")

        result = mover.undo_last(self.folder)

        self.assertEqual(result.restored, [("report.pdf", "Documents/report.pdf")])


class LogRecordTests(UndoTestCase):
    def test_undo_is_recorded_and_earlier_log_lines_are_untouched(self):
        make_file(self.folder, "report.pdf")
        make_file(self.folder, "photo.jpg")
        self.tidy()
        log_file = self.folder / rules.LOG_FILENAME
        before = log_file.read_bytes()

        mover.undo_last(self.folder)

        self.assertTrue(log_file.read_bytes().startswith(before))
        records = read_log(self.folder)
        events = [r["event"] for r in records]
        self.assertEqual(events, ["run_start", "move", "move", "run_end",
                                  "undo_start", "undone", "undone", "undo_end"])
        undone = [r for r in records if r["event"] == "undone"]
        self.assertEqual({r["status"] for r in undone}, {"restored"})
        self.assertEqual(records[-1]["restored"], 2)
        self.assertEqual(len({r["run"] for r in records}), 1)

    def test_the_log_holds_no_absolute_paths_after_undo(self):
        make_file(self.folder, "report.pdf")
        self.tidy()
        mover.undo_last(self.folder)
        text = (self.folder / rules.LOG_FILENAME).read_text(encoding="utf-8")
        self.assertNotIn(str(self.folder), text)
        self.assertNotIn(os.path.expanduser("~"), text)

    def test_if_the_log_is_read_only_nothing_is_restored(self):
        make_file(self.folder, "report.pdf", content=b"data")
        self.tidy()
        log_file = self.folder / rules.LOG_FILENAME
        os.chmod(log_file, stat.S_IREAD)
        self.addCleanup(os.chmod, log_file, stat.S_IREAD | stat.S_IWRITE)
        if os.access(log_file, os.W_OK):
            self.skipTest("this account can write to read-only files")
        before = snapshot(self.folder)

        result = mover.undo_last(self.folder)

        self.assertIn("nothing was changed", result.error)
        self.assertEqual(result.restored, [])
        self.assertEqual(snapshot(self.folder), before)

    def test_if_the_log_fails_mid_undo_it_stops_before_touching_more(self):
        for name in ("a.pdf", "b.pdf", "c.pdf"):
            make_file(self.folder, name, content=name.encode())
        self.tidy()
        real_write = mover._Log.write
        calls = []

        def flaky_write(self_, record):
            calls.append(record["event"])
            if len(calls) == 3:  # undo_start, first file, then fail on the second
                raise OSError(28, "No space left on device")
            return real_write(self_, record)

        with mock.patch.object(mover._Log, "write", autospec=True,
                               side_effect=flaky_write):
            result = mover.undo_last(self.folder)

        self.assertIn("b.pdf", result.error)
        self.assertIn("no further files were touched", result.error)
        self.assertTrue((self.folder / "Documents" / "a.pdf").exists())  # untouched
        self.assertEqual(len(result.restored), 2)  # c.pdf, then b.pdf

    def test_a_log_cut_off_by_a_crash_still_works(self):
        make_file(self.folder, "report.pdf", content=b"data")
        self.tidy()
        log_file = self.folder / rules.LOG_FILENAME
        log_file.write_bytes(log_file.read_bytes()[:-10])  # cut the last line short

        result = mover.undo_last(self.folder)

        self.assertEqual(result.restored, [("report.pdf", "Documents/report.pdf")])
        lines = log_file.read_text(encoding="utf-8").split("\n")
        bad, good = [], []
        for line in lines:
            if line:
                try:
                    good.append(json.loads(line))
                except ValueError:
                    bad.append(line)
        self.assertEqual(len(bad), 1)  # only the damaged line itself
        # The records written by undo each landed on their own, readable line.
        events = [record["event"] for record in good]
        self.assertIn("undo_start", events)
        self.assertIn("undone", events)

    def test_apply_after_a_cut_off_log_does_not_corrupt_new_records(self):
        make_file(self.folder, "one.pdf", content=b"1")
        self.tidy()
        log_file = self.folder / rules.LOG_FILENAME
        log_file.write_bytes(log_file.read_bytes()[:-10])
        make_file(self.folder, "two.pdf", content=b"2")
        second = self.tidy()

        second_run = [json.loads(line)
                      for line in log_file.read_text(encoding="utf-8").split("\n")
                      if line.startswith("{") and second.run_id in line
                      and line.endswith("}")]
        self.assertEqual([r["event"] for r in second_run],
                         ["run_start", "move", "run_end"])
        self.assertEqual(mover.undo_last(self.folder).run_id, second.run_id)
        self.assertEqual(len(mover.undo_last(self.folder).restored), 1)


class FormatUndoResultTests(UndoTestCase):
    def test_report_when_there_is_nothing_to_undo(self):
        self.assertEqual(mover.format_undo_result(mover.undo_last(self.folder)),
                         "Nothing to undo.")

    def test_report_lists_restored_missing_and_left_files(self):
        make_file(self.folder, "report.pdf")
        make_file(self.folder, "photo.jpg")
        make_file(self.folder, "song.mp3")
        self.tidy()
        os.remove(self.folder / "Audio" / "song.mp3")
        make_file(self.folder, "report.pdf", content=b"newcomer")

        text = mover.format_undo_result(mover.undo_last(self.folder))

        self.assertIn("Put back 1 file:", text)
        self.assertIn("%s -> photo.jpg" % Path("Images", "photo.jpg"), text)
        self.assertIn("Already gone, so nothing to put back (1):", text)
        self.assertIn("Could not put back 1 file (left where they are):", text)
        self.assertIn("(the original name 'report.pdf' is already taken)", text)
        self.assertIn("run undo again to retry", text)

    def test_report_shows_a_stop_message(self):
        result = mover.UndoResult("abc", [], [], [], [], "the log broke")
        self.assertIn("STOPPED: the log broke", mover.format_undo_result(result))


if __name__ == "__main__":
    unittest.main()
