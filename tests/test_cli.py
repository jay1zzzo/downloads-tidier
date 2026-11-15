import contextlib
import io
import os
import subprocess
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

from downloads_tidier import __version__, cli, mover, rules
from downloads_tidier.cli import default_downloads_folder, main, unsafe_folder_reason
from tests.helpers import TempFolderTestCase, make_file, snapshot, tree

REPO_ROOT = Path(__file__).resolve().parents[1]


def files_only(snap):
    return {key: value for key, value in snap.items()
            if value[0] == "file" and key != rules.LOG_FILENAME}


class AsciiStream(io.StringIO):
    """Pretends to be a console that can only show plain ASCII letters."""
    encoding = "ascii"


class CliTestCase(TempFolderTestCase):
    def setUp(self):
        super().setUp()
        # SAFETY NET: if a test forgets --folder, fail loudly instead of ever
        # looking at the real Downloads folder.
        guard = mock.patch(
            "downloads_tidier.cli.default_downloads_folder",
            side_effect=AssertionError("a test forgot --folder"),
        )
        guard.start()
        self.addCleanup(guard.stop)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        code = main(list(argv), stdout=out, stderr=err)
        return code, out.getvalue(), err.getvalue()

    def make_messy_folder(self):
        for name in ("photo.jpg", "report.pdf", "setup.exe", "notes",
                     ".hidden", "movie.mp4.part"):
            make_file(self.folder, name, content=("content of " + name).encode())
        make_file(self.folder, "fresh.zip", content=b"fresh", age=5)
        (self.folder / "Old Stuff").mkdir()


class ArgumentTests(CliTestCase):
    def test_no_command_means_preview(self):
        self.make_messy_folder()
        before = snapshot(self.folder)
        code, out, _ = self.run_cli("--folder", str(self.folder))
        self.assertEqual(code, 0)
        self.assertIn("Would move", out)
        self.assertEqual(snapshot(self.folder), before)

    def test_folder_can_come_before_or_after_the_command(self):
        self.make_messy_folder()
        results = {
            self.run_cli("--folder", str(self.folder), "preview")[1],
            self.run_cli("preview", "--folder", str(self.folder))[1],
            self.run_cli("--folder", str(self.folder))[1],
        }
        self.assertEqual(len(results), 1)

    def test_apply_accepts_folder_in_either_position(self):
        for position in ("before", "after"):
            with self.subTest(position=position):
                folder = self.folder / position
                folder.mkdir()
                make_file(folder, "report.pdf")
                argv = (["--folder", str(folder), "apply"] if position == "before"
                        else ["apply", "--folder", str(folder)])
                code, _, _ = self.run_cli(*argv)
                self.assertEqual(code, 0)
                self.assertTrue((folder / "Documents" / "report.pdf").exists())

    def test_an_unknown_command_is_a_usage_error(self):
        with contextlib.redirect_stderr(io.StringIO()):
            code = main(["frobnicate", "--folder", str(self.folder)])
        self.assertEqual(code, 2)

    def test_an_unexpected_extra_word_is_a_usage_error(self):
        with contextlib.redirect_stderr(io.StringIO()):
            code = main(["apply", "surprise", "--folder", str(self.folder)])
        self.assertEqual(code, 2)

    def test_help_and_version_work(self):
        with contextlib.redirect_stdout(io.StringIO()) as shown:
            self.assertEqual(main(["--help"]), 0)
        self.assertIn("preview", shown.getvalue())
        self.assertIn("undo", shown.getvalue())
        with contextlib.redirect_stdout(io.StringIO()) as shown:
            self.assertEqual(main(["--version"]), 0)
        self.assertIn(__version__, shown.getvalue())


class PreviewCommandTests(CliTestCase):
    def test_preview_shows_the_plan_and_changes_nothing(self):
        self.make_messy_folder()
        before = snapshot(self.folder)

        code, out, err = self.run_cli("preview", "--folder", str(self.folder))

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("Previewing: %s" % self.folder, out)
        self.assertIn("Would move 4 files:", out)
        self.assertIn("Would skip 4 items:", out)
        self.assertIn("This was a preview. Nothing was changed.", out)
        self.assertIn('run again with "apply"', out)
        self.assertEqual(snapshot(self.folder), before)
        self.assertFalse((self.folder / rules.LOG_FILENAME).exists())

    def test_preview_of_a_folder_with_nothing_to_do(self):
        code, out, _ = self.run_cli("preview", "--folder", str(self.folder))
        self.assertEqual(code, 0)
        self.assertIn("Nothing to move.", out)
        self.assertNotIn('"apply"', out)


class ApplyAndUndoCommandTests(CliTestCase):
    def test_apply_moves_the_files_and_says_so(self):
        self.make_messy_folder()
        code, out, err = self.run_cli("apply", "--folder", str(self.folder))
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("Moved 4 files:", out)
        self.assertIn('run "undo"', out)
        self.assertEqual(tree(self.folder) - {rules.LOG_FILENAME}, {
            "Images/photo.jpg", "Documents/report.pdf", "Installers/setup.exe",
            "Other/notes", ".hidden", "movie.mp4.part", "fresh.zip",
        })

    def test_apply_then_undo_restores_the_folder_exactly(self):
        self.make_messy_folder()
        before = files_only(snapshot(self.folder))
        self.assertEqual(self.run_cli("apply", "--folder", str(self.folder))[0], 0)

        code, out, _ = self.run_cli("undo", "--folder", str(self.folder))

        self.assertEqual(code, 0)
        self.assertIn("Put back 4 files:", out)
        self.assertEqual(files_only(snapshot(self.folder)), before)

    def test_undo_with_nothing_to_undo(self):
        make_file(self.folder, "report.pdf")
        before = snapshot(self.folder)
        code, out, _ = self.run_cli("undo", "--folder", str(self.folder))
        self.assertEqual(code, 0)
        self.assertIn("Nothing to undo.", out)
        self.assertEqual(snapshot(self.folder), before)

    def test_undo_that_cannot_finish_exits_with_1(self):
        make_file(self.folder, "report.pdf", content=b"old")
        self.run_cli("apply", "--folder", str(self.folder))
        make_file(self.folder, "report.pdf", content=b"new download")

        code, out, _ = self.run_cli("undo", "--folder", str(self.folder))

        self.assertEqual(code, 1)
        self.assertIn("already taken", out)
        self.assertEqual((self.folder / "report.pdf").read_bytes(), b"new download")

    def test_a_file_that_cannot_be_moved_is_reported_and_exits_with_1(self):
        make_file(self.folder, "locked.pdf", content=b"locked")
        make_file(self.folder, "fine.pdf", content=b"fine")
        real_replace = os.replace

        def fake_replace(source, destination, *args, **kwargs):
            if Path(source).name == "locked.pdf":
                raise PermissionError(13, "Permission denied")
            return real_replace(source, destination, *args, **kwargs)

        with mock.patch("downloads_tidier.mover.os.replace", side_effect=fake_replace):
            code, out, _ = self.run_cli("apply", "--folder", str(self.folder))

        self.assertEqual(code, 1)
        self.assertIn("Moved 1 file:", out)
        self.assertIn("Could not move 1 file", out)
        self.assertIn("locked.pdf", out)
        self.assertEqual((self.folder / "locked.pdf").read_bytes(), b"locked")

    def test_ctrl_c_is_handled_politely(self):
        make_file(self.folder, "report.pdf")
        with mock.patch("downloads_tidier.cli.mover.apply_plan",
                        side_effect=KeyboardInterrupt):
            code, _, err = self.run_cli("apply", "--folder", str(self.folder))
        self.assertEqual(code, 130)
        self.assertIn("undo", err)

    def test_an_unreadable_folder_gives_a_clean_error(self):
        with mock.patch("downloads_tidier.cli.planner.plan_moves",
                        side_effect=PermissionError(13, "Permission denied")):
            code, out, err = self.run_cli("preview", "--folder", str(self.folder))
        self.assertEqual(code, 1)
        self.assertIn("Error: Permission denied", err)


class ChosenFolderTests(CliTestCase):
    def test_only_the_chosen_folder_is_touched(self):
        chosen = self.folder / "chosen"
        other = self.folder / "other"
        chosen.mkdir()
        other.mkdir()
        make_file(chosen, "report.pdf")
        make_file(other, "report.pdf", content=b"leave me")
        make_file(self.folder, "top.pdf", content=b"leave me too")
        before_other = snapshot(other)

        self.run_cli("apply", "--folder", str(chosen))

        self.assertTrue((chosen / "Documents" / "report.pdf").exists())
        self.assertEqual(snapshot(other), before_other)
        self.assertEqual((self.folder / "top.pdf").read_bytes(), b"leave me too")

    def test_the_default_folder_is_used_when_no_folder_is_given(self):
        downloads = self.folder / "Downloads"
        downloads.mkdir()
        make_file(downloads, "report.pdf")
        with mock.patch("downloads_tidier.cli.default_downloads_folder",
                        return_value=downloads):
            code, out, _ = self.run_cli("apply")
        self.assertEqual(code, 0)
        self.assertTrue((downloads / "Documents" / "report.pdf").exists())

    def test_a_missing_default_folder_asks_for_folder(self):
        with mock.patch("downloads_tidier.cli.default_downloads_folder",
                        return_value=self.folder / "no-such-downloads"):
            code, out, err = self.run_cli("apply")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("--folder", err)

    def test_a_missing_folder_is_an_error(self):
        code, out, err = self.run_cli("apply", "--folder", str(self.folder / "nope"))
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("folder not found", err)

    def test_a_file_instead_of_a_folder_is_an_error(self):
        path = make_file(self.folder, "file.txt")
        code, _, err = self.run_cli("apply", "--folder", str(path))
        self.assertEqual(code, 2)
        self.assertIn("not a folder", err)
        self.assertTrue(path.exists())

    def test_a_relative_folder_path_works(self):
        sub = self.folder / "rel"
        sub.mkdir()
        make_file(sub, "report.pdf")
        old = os.getcwd()
        os.chdir(self.folder)
        self.addCleanup(os.chdir, old)
        code, _, _ = self.run_cli("apply", "--folder", "rel")
        self.assertEqual(code, 0)
        self.assertTrue((sub / "Documents" / "report.pdf").exists())


class RefusedFolderTests(CliTestCase):
    def test_the_top_of_a_drive_is_refused(self):
        # Preview only: even if this guard broke, a preview never changes anything.
        code, out, err = self.run_cli("preview", "--folder", self.folder.anchor)
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("very top of a drive", err)

    def test_the_whole_home_folder_is_refused_but_folders_inside_it_are_fine(self):
        make_file(self.folder, "report.pdf")
        with mock.patch("pathlib.Path.home", return_value=self.folder):
            code, _, err = self.run_cli("apply", "--folder", str(self.folder))
            self.assertEqual(code, 2)
            self.assertIn("home folder", err)
            self.assertTrue((self.folder / "report.pdf").exists())

            inside = self.folder / "Downloads"
            inside.mkdir()
            make_file(inside, "report.pdf")
            code, _, _ = self.run_cli("apply", "--folder", str(inside))
            self.assertEqual(code, 0)

    def test_system_folders_are_refused(self):
        fake_system = self.folder / "pretend-windows"
        (fake_system / "System32").mkdir(parents=True)
        make_file(fake_system, "kept.pdf")
        make_file(fake_system / "System32", "kept.pdf")
        with mock.patch.dict(os.environ, {"WINDIR": str(fake_system)}):
            for target in (fake_system, fake_system / "System32"):
                with self.subTest(target=target.name):
                    code, _, err = self.run_cli("apply", "--folder", str(target))
                    self.assertEqual(code, 2)
                    self.assertIn("system folder", err)
        self.assertEqual(tree(fake_system), {"kept.pdf", "System32/kept.pdf"})

    def test_a_folder_that_merely_has_a_similar_name_is_allowed(self):
        (self.folder / "win").mkdir()
        winery = self.folder / "winery"
        winery.mkdir()
        with mock.patch.dict(os.environ, {"WINDIR": str(self.folder / "win")}):
            self.assertIsNone(unsafe_folder_reason(Path(os.path.realpath(winery))))

    def test_an_ordinary_folder_is_allowed(self):
        self.assertIsNone(unsafe_folder_reason(self.folder))


class ConsoleSafetyTests(CliTestCase):
    def test_unprintable_file_names_do_not_crash_and_files_still_move(self):
        names = ["résumé \U0001F600.pdf", "日本語.txt"]
        for name in names:
            make_file(self.folder, name, content=name.encode("utf-8"))
        out, err = AsciiStream(), AsciiStream()

        code = main(["apply", "--folder", str(self.folder)], stdout=out, stderr=err)

        self.assertEqual(code, 0)
        self.assertIn("r?sum?", out.getvalue())  # shown with ? where it can't print
        for name in names:
            with self.subTest(name=name):
                moved = self.folder / "Documents" / name
                self.assertEqual(moved.read_bytes(), name.encode("utf-8"))

    def test_preview_with_unprintable_names_does_not_crash(self):
        make_file(self.folder, "日本語.pdf")
        out = AsciiStream()
        code = main(["preview", "--folder", str(self.folder)], stdout=out,
                    stderr=AsciiStream())
        self.assertEqual(code, 0)
        self.assertIn("Would move 1 file:", out.getvalue())


class DefaultDownloadsFolderTests(TempFolderTestCase):
    """These call the real lookup function with the registry and home faked."""

    def fake_winreg(self, value, names_seen=None, error=None):
        class Key:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

        def open_key(root, path):
            if error:
                raise error
            return Key()

        def query(key, name):
            if names_seen is not None:
                names_seen.append(name)
            return value, 2

        return types.SimpleNamespace(HKEY_CURRENT_USER=1, OpenKey=open_key,
                                     QueryValueEx=query)

    def lookup(self, fake, platform="win32"):
        home = self.folder / "home"
        with mock.patch.dict(sys.modules, {"winreg": fake}), \
                mock.patch("downloads_tidier.cli.sys.platform", platform), \
                mock.patch("pathlib.Path.home", return_value=home):
            return default_downloads_folder(), home

    def test_windows_uses_the_location_from_the_registry(self):
        moved = self.folder / "D-drive-downloads"
        moved.mkdir()
        seen = []
        found, _ = self.lookup(self.fake_winreg(str(moved), seen))
        self.assertEqual(found, moved)
        # Windows' fixed ID for the Downloads folder (written out in full here,
        # so a typo in the program can't hide by being copied into the test).
        self.assertEqual(seen, ["{374DE290-123F-4565-9164-ACA116FB0C86}"])

    @unittest.skipUnless(os.name == "nt", "expands Windows-style %VARIABLES%")
    def test_windows_expands_variables_in_the_registry_value(self):
        (self.folder / "Dl").mkdir()
        with mock.patch.dict(os.environ, {"TIDIER_TEST_BASE": str(self.folder)}):
            found, _ = self.lookup(self.fake_winreg("%TIDIER_TEST_BASE%\\Dl"))
        self.assertEqual(found, self.folder / "Dl")

    def test_windows_falls_back_when_the_registry_folder_does_not_exist(self):
        found, home = self.lookup(self.fake_winreg(str(self.folder / "gone")))
        self.assertEqual(found, home / "Downloads")

    def test_windows_falls_back_when_the_registry_cannot_be_read(self):
        found, home = self.lookup(self.fake_winreg("x", error=OSError("no key")))
        self.assertEqual(found, home / "Downloads")

    def test_windows_falls_back_when_winreg_is_unavailable(self):
        found, home = self.lookup(None)  # sys.modules["winreg"] = None -> ImportError
        self.assertEqual(found, home / "Downloads")

    def test_macos_and_linux_use_downloads_in_the_home_folder(self):
        found, home = self.lookup(self.fake_winreg("never used"), platform="darwin")
        self.assertEqual(found, home / "Downloads")
        found, home = self.lookup(self.fake_winreg("never used"), platform="linux")
        self.assertEqual(found, home / "Downloads")

    @unittest.skipUnless(sys.platform == "win32", "reads this PC's real registry")
    def test_the_real_registry_lookup_works_on_this_windows_pc(self):
        self.assertIsInstance(default_downloads_folder(), Path)


class EndToEndTests(TempFolderTestCase):
    """Run the real program as a separate process, like a user would."""

    def run_program(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "downloads_tidier", *args],
            cwd=str(REPO_ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120,
        )

    def test_preview_then_apply_then_undo_as_a_real_command(self):
        for name in ("photo.jpg", "report.pdf", "notes"):
            make_file(self.folder, name, content=name.encode())
        before = files_only(snapshot(self.folder))

        preview = self.run_program("--folder", str(self.folder))
        self.assertEqual(preview.returncode, 0, preview.stderr)
        self.assertIn("Would move 3 files:", preview.stdout)
        self.assertEqual(files_only(snapshot(self.folder)), before)

        applied = self.run_program("apply", "--folder", str(self.folder))
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertIn("Moved 3 files:", applied.stdout)
        self.assertTrue((self.folder / "Images" / "photo.jpg").exists())

        undone = self.run_program("undo", "--folder", str(self.folder))
        self.assertEqual(undone.returncode, 0, undone.stderr)
        self.assertEqual(files_only(snapshot(self.folder)), before)

    def test_a_bad_folder_exits_with_2_and_a_message(self):
        result = self.run_program("apply", "--folder", str(self.folder / "nope"))
        self.assertEqual(result.returncode, 2)
        self.assertIn("folder not found", result.stderr)

    def test_version_flag(self):
        result = self.run_program("--version")
        self.assertEqual(result.returncode, 0)
        self.assertIn(__version__, result.stdout)


if __name__ == "__main__":
    unittest.main()
