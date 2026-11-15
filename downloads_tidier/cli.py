"""The command-line interface: preview, apply and undo.

    python -m downloads_tidier                 preview your Downloads folder
    python -m downloads_tidier apply           move the files
    python -m downloads_tidier undo            put back the last apply
    python -m downloads_tidier --folder PATH   use another folder

Exit codes: 0 = all fine, 1 = finished but some files could not be handled
(or the run stopped early), 2 = could not start (bad folder or bad options).
"""

import argparse
import os
import sys
from pathlib import Path

from . import __version__, mover, planner

# If someone moves their Downloads folder, Windows records the new location
# in the registry under this ID. The entry is usually absent when Downloads is
# in its normal place, so we then use ~/Downloads.
_DOWNLOADS_FOLDER_ID = "{374DE290-123F-4565-9164-ACA116FB0C86}"
_SHELL_FOLDERS_KEY = (r"Software\Microsoft\Windows\CurrentVersion"
                      r"\Explorer\User Shell Folders")

# Folders that must never be tidied (the folder itself or anything inside).
_PROTECTED_ENVIRONMENT_FOLDERS = (
    "WINDIR", "SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData",
)
_PROTECTED_UNIX_FOLDERS = (
    "/bin", "/boot", "/dev", "/etc", "/lib", "/proc", "/sbin", "/sys", "/usr",
    "/System", "/Library", "/Applications",
)


def default_downloads_folder():
    """Return the user's Downloads folder (it may not exist)."""
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _SHELL_FOLDERS_KEY) as key:
                value, _ = winreg.QueryValueEx(key, _DOWNLOADS_FOLDER_ID)
            candidate = Path(os.path.expandvars(value))
            if candidate.is_dir():
                return candidate
        except (ImportError, OSError):
            pass  # fall back to the usual place below
    return Path.home() / "Downloads"


def unsafe_folder_reason(folder):
    """Return why `folder` must not be tidied, or None if it's acceptable.

    `folder` should already be fully resolved (see resolve_folder).
    """
    folder = Path(folder)
    if folder.parent == folder:
        return "that is the very top of a drive"
    if folder == Path(os.path.realpath(Path.home())):
        return "that is your whole home folder; choose a folder inside it, like Downloads"

    protected = [os.environ[name] for name in _PROTECTED_ENVIRONMENT_FOLDERS
                 if os.environ.get(name)]
    protected += [p for p in _PROTECTED_UNIX_FOLDERS if os.path.isdir(p)]
    for base in protected:
        base = Path(os.path.realpath(base))
        if folder == base or base in folder.parents:
            return "that is a system folder"
    return None


def resolve_folder(folder_argument):
    """Work out which folder to use. Returns (folder, problem).

    Exactly one of the two is None. The folder is fully resolved, so every
    later check compares real locations.
    """
    if folder_argument is None:
        chosen = default_downloads_folder()
        if not chosen.is_dir():
            return None, ("could not find your Downloads folder (looked at %s). "
                          "Use --folder to choose a folder." % chosen)
    else:
        chosen = Path(folder_argument).expanduser()
        if not chosen.exists():
            return None, "folder not found: %s" % chosen
        if not chosen.is_dir():
            return None, "not a folder: %s" % chosen

    folder = Path(os.path.realpath(chosen))
    reason = unsafe_folder_reason(folder)
    if reason:
        return None, "will not tidy %s: %s" % (folder, reason)
    return folder, None


def _emit(text, stream):
    """Print `text`, replacing characters the console can't show with "?".

    Windows consoles often can't display emoji or unusual letters in file
    names; printing them would otherwise crash the whole program.
    """
    encoding = getattr(stream, "encoding", None) or "utf-8"
    safe = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
    stream.write(safe + "\n")
    stream.flush()


def build_parser():
    parser = argparse.ArgumentParser(
        prog="downloads-tidier",
        description="Sort the files in a messy folder (your Downloads, by default) "
                    "into subfolders by file type, safely.",
        epilog="Safe by design: nothing is ever deleted or overwritten, subfolders, "
               "shortcuts, hidden files, half-downloaded files and anything changed "
               "in the last 10 minutes are left alone, and 'undo' puts everything "
               "back. With no command, 'preview' runs.",
    )
    parser.add_argument("--version", action="version",
                        version="downloads-tidier " + __version__)
    parser.add_argument("--folder", metavar="PATH",
                        help="the folder to tidy (default: your Downloads folder)")

    commands = parser.add_subparsers(dest="command", metavar="{preview,apply,undo}")
    descriptions = {
        "preview": "show what would move, and change nothing (the default)",
        "apply": "move the files into subfolders by type",
        "undo": "put back everything from the last apply",
    }
    for name, text in descriptions.items():
        sub = commands.add_parser(name, help=text, description=text)
        # SUPPRESS: so "--folder X apply" and "apply --folder X" both work.
        sub.add_argument("--folder", metavar="PATH", default=argparse.SUPPRESS,
                         help="the folder to tidy (default: your Downloads folder)")
    return parser


def _preview(folder, out):
    plan = planner.plan_moves(folder)
    _emit("Previewing: %s\n" % folder, out)
    _emit(planner.format_preview(plan, folder), out)
    if plan.moves:
        _emit('\nTo move these files for real, run again with "apply".', out)
    return 0


def _apply(folder, out):
    plan = planner.plan_moves(folder)
    _emit("Tidying: %s\n" % folder, out)
    result = mover.apply_plan(folder, plan)
    _emit(mover.format_result(result, folder), out)
    if result.moved:
        _emit('\nTo put everything back, run "undo".', out)
    return 1 if (result.failed or result.error) else 0


def _undo(folder, out):
    _emit("Undoing the last tidy in: %s\n" % folder, out)
    result = mover.undo_last(folder)
    _emit(mover.format_undo_result(result), out)
    incomplete = result.skipped or result.failed or result.error
    return 1 if incomplete else 0


_COMMANDS = {"preview": _preview, "apply": _apply, "undo": _undo}


def main(argv=None, stdout=None, stderr=None):
    """Run the tool. Returns the exit code."""
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr

    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:  # argparse already printed its message
        return exc.code if isinstance(exc.code, int) else 0

    folder, problem = resolve_folder(args.folder)
    if problem:
        _emit("Error: %s" % problem, err)
        return 2

    command = _COMMANDS[args.command or "preview"]
    try:
        return command(folder, out)
    except KeyboardInterrupt:
        _emit("\nInterrupted. Anything already moved is recorded; "
              'run "undo" to put it back.', err)
        return 130
    except OSError as exc:
        _emit("Error: %s" % (exc.strerror or exc), err)
        return 1
