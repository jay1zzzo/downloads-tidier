"""Carry out a plan: move the files, and record every move in a log.

How a single file is moved safely
---------------------------------
1. RESERVE the new name by creating an empty file there. The operating system
   only allows this if nothing exists at that name (an "exclusive create"),
   so we can never take over someone else's file. If the name is taken we try
   "name (1).ext", "name (2).ext", and so on.
2. RENAME the real file onto our own empty placeholder with os.replace. A
   rename inside the same folder tree is instant: the data is never copied.
   (os.replace is used because on Windows a plain rename refuses to land on an
   existing name, even our own placeholder.)
3. If the rename fails (for example the file is open in another program on
   Windows), remove the empty placeholder WE created, leave the real file
   exactly where it was, and report it. The placeholder is the only thing this
   tool ever removes, and only while it is still empty.

The tool never uses shutil.move, because that silently falls back to
copy-then-delete when a rename isn't possible.

The undo log
------------
Every move is appended to a hidden file in the chosen folder, one JSON record
per line, and flushed to disk straight away. The file is only ever added to,
so a crash half way through still leaves a usable record. No move is made
until the log has been opened successfully.

Undo
----
Undo reads the log, picks the newest apply that still has files to put back,
and moves each one back to its original name using the same reserve-then-
rename method, so it can never overwrite anything either. A file whose
original name is taken again is left where it is and reported. Each restored
file is recorded in the log, so running undo again continues with whatever is
left, and then with the apply before it. Log entries are never trusted
blindly: anything that doesn't look like "Category/name" inside the chosen
folder is refused.
"""

import itertools
import json
import os
import stat
import uuid
from collections import namedtuple
from datetime import datetime, timezone
from pathlib import Path

from . import rules
from .planner import Move, inspect_category_folder, numbered_names
from .scanner import skip_reason

# run_id: identifies this apply in the log (None if nothing was attempted).
# moved: list of Move (with the real final names).
# skipped: list of (Path, reason) left alone on purpose.
# failed: list of (Path, reason) that we tried to move but could not.
# error: None, or a message if the run had to stop early.
ApplyResult = namedtuple("ApplyResult", ["run_id", "moved", "skipped", "failed", "error"])

# Give up looking for a free name after this many tries.
MAX_NAME_ATTEMPTS = 10000


class LogError(Exception):
    """The undo log could not be opened, so nothing was moved."""


def log_path(folder):
    return Path(folder) / rules.LOG_FILENAME


def _utc_timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _lacks_final_newline(path):
    """True if the file exists, has content, and doesn't end with a newline."""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                return False
            handle.seek(-1, os.SEEK_END)
            return handle.read(1) != b"\n"
    except FileNotFoundError:
        return False


class _Log:
    """Append-only log. Each record is flushed to disk as soon as it's written."""

    def __init__(self, folder):
        path = log_path(folder)
        try:
            needs_newline = _lacks_final_newline(path)
            self._file = open(path, "a", encoding="utf-8", newline="\n")
            if needs_newline:
                # A crash can leave a half-written last line. Start on a fresh
                # line so the new records don't get glued onto the damaged one.
                self._file.write("\n")
        except OSError as exc:
            raise LogError(
                "could not open the undo log (%s), so nothing was changed"
                % (exc.strerror or exc.__class__.__name__)
            )

    def write(self, record):
        self._file.write(json.dumps(record, sort_keys=True) + "\n")
        self._file.flush()
        os.fsync(self._file.fileno())

    def close(self):
        self._file.close()


def _is_plain_name(name):
    """True for a bare file name with no folder part ("a.txt", not "x/a.txt")."""
    return bool(name) and name not in (".", "..") and Path(name).name == name


def _reserve(path):
    """Atomically create an empty file at `path`.

    Returns True if we created it, or False if something already exists there.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        return False
    except PermissionError:
        # Windows can report "access denied" when a folder sits on that name.
        if os.path.lexists(path):
            return False
        raise
    os.close(descriptor)
    return True


def _remove_placeholder(path):
    """Remove the empty placeholder file we created. Returns True if removed.

    Refuses to remove anything that isn't a regular, completely empty file.
    """
    try:
        info = os.lstat(path)
        if stat.S_ISREG(info.st_mode) and info.st_size == 0:
            os.remove(path)
            return True
    except OSError:
        pass
    return False


def _prepare_category_folder(folder, category, cache):
    """Make sure the category folder exists and is safe. Returns a problem or None."""
    if category in cache:
        return cache[category]

    problem, _ = inspect_category_folder(folder, category)
    destination = Path(folder) / category
    if problem is None and not os.path.lexists(destination):
        try:
            os.mkdir(destination)
        except FileExistsError:
            pass
        except OSError as exc:
            problem = "could not create the '%s' folder (%s)" % (
                category, exc.strerror or exc.__class__.__name__)
        if problem is None:
            # Look again now it exists: it must be a normal folder in here.
            problem, _ = inspect_category_folder(folder, category)
    cache[category] = problem
    return problem


def _move_one(folder, move, categories, now):
    """Try to move one file. Returns (outcome, detail).

    outcome is "moved" (detail: the final name), "skipped" or "failed"
    (detail: the reason).
    """
    source = Path(move.source)
    planned_name = Path(move.destination).name

    # Refuse anything that is not exactly "a file directly inside the chosen
    # folder going into one of our own category folders".
    if (move.category not in rules.ALL_CATEGORIES
            or not _is_plain_name(planned_name)
            or not _is_plain_name(source.name)
            or Path(os.path.realpath(source.parent)) != Path(os.path.realpath(folder))):
        return "skipped", "refused: not a file directly inside the chosen folder"

    # The folder may have changed since the preview. Check the file again.
    reason = skip_reason(source, now=now)
    if reason is not None:
        return "skipped", "no longer safe to move (%s)" % reason

    problem = _prepare_category_folder(folder, move.category, categories)
    if problem is not None:
        return "skipped", problem

    # Reserve a free name. Prefer the name shown in the preview.
    destination_folder = Path(folder) / move.category
    candidates = itertools.chain([planned_name], numbered_names(source.name))
    final_name = None
    try:
        for attempt, candidate in enumerate(candidates):
            if attempt >= MAX_NAME_ATTEMPTS:
                return "failed", "could not find a free name"
            if _reserve(destination_folder / candidate):
                final_name = candidate
                break
    except OSError as exc:
        return "failed", "could not reserve a name (%s)" % (
            exc.strerror or exc.__class__.__name__)

    destination = destination_folder / final_name
    try:
        os.replace(source, destination)
    except OSError as exc:
        removed = _remove_placeholder(destination)
        reason = "could not be moved, probably in use by another program (%s)" % (
            exc.strerror or exc.__class__.__name__)
        if not removed:
            reason += "; an empty placeholder file was left at %s" % destination
        return "failed", reason

    return "moved", final_name


def apply_plan(folder, plan, now=None):
    """Carry out `plan` inside `folder`. Returns an ApplyResult.

    Files that can't be moved are reported, never fatal. The run only stops
    early if the undo log can't be written.
    """
    folder = Path(folder)
    moved = []
    skipped = list(plan.skipped)
    failed = []
    error = None
    log = None
    run_id = None
    categories = {}

    try:
        for move in plan.moves:
            if log is None:
                # Open the log just before the first move, never after it.
                try:
                    log = _Log(folder)
                    run_id = uuid.uuid4().hex[:8]
                    log.write({"event": "run_start", "run": run_id,
                               "time": _utc_timestamp()})
                except LogError as exc:
                    error = str(exc)
                    break
                except OSError as exc:
                    error = "could not write the undo log (%s), so nothing was moved" % (
                        exc.strerror or exc.__class__.__name__)
                    break

            outcome, detail = _move_one(folder, move, categories, now)
            if outcome == "skipped":
                skipped.append((move.source, detail))
            elif outcome == "failed":
                failed.append((move.source, detail))
            else:
                done = Move(move.source, Path(folder) / move.category / detail,
                            move.category, detail != Path(move.source).name)
                moved.append(done)
                try:
                    log.write({
                        "event": "move", "run": run_id,
                        "from": Path(move.source).name,
                        "to": "%s/%s" % (move.category, detail),
                    })
                except OSError as exc:
                    # The file HAS moved but we couldn't record it. Stop now
                    # rather than move more files we can't undo.
                    error = ("stopped: %s was moved but could not be written to "
                             "the undo log (%s); no further files were moved" % (
                                 Path(move.source).name,
                                 exc.strerror or exc.__class__.__name__))
                    break

        if log is not None and error is None:
            try:
                log.write({"event": "run_end", "run": run_id,
                           "moved": len(moved), "failed": len(failed)})
            except OSError:
                pass  # the moves themselves are already recorded
    finally:
        if log is not None:
            log.close()

    skipped.sort(key=lambda item: Path(item[0]).name)
    return ApplyResult(run_id, moved, skipped, failed, error)


def _count(number, noun):
    return "%d %s%s" % (number, noun, "" if number == 1 else "s")


# ---------------------------------------------------------------------------
# Undo
# ---------------------------------------------------------------------------

# run_id: the apply being undone (None if there was nothing to undo).
# restored: list of (original_name, "Category/name" it was moved to).
# missing: list of "Category/name" that was no longer there (nothing to restore).
# skipped: list of ("Category/name", reason): left where it is on purpose.
# failed: list of ("Category/name", reason): tried, but the OS refused.
# error: None, or a message if undo had to stop early.
UndoResult = namedtuple("UndoResult",
                        ["run_id", "restored", "missing", "skipped", "failed", "error"])


class _Run:
    """One apply as read back from the log."""

    def __init__(self, run_id):
        self.run_id = run_id
        self.moves = []        # (original_name, "Category/name"), in order
        self.resolved = set()  # moves already undone (or found already gone)

    def outstanding(self):
        return [move for move in self.moves if move not in self.resolved]


def read_runs(folder):
    """Read the undo log. Returns the runs, oldest first.

    Lines that can't be understood (for example one cut off by a crash) are
    ignored, so one damaged line never blocks undoing everything else.
    """
    try:
        with open(log_path(folder), "r", encoding="utf-8", errors="replace",
                  newline="") as handle:
            text = handle.read()
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise LogError("could not read the undo log (%s)"
                       % (exc.strerror or exc.__class__.__name__))

    runs = {}
    for line in text.split("\n"):
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        run_id, event = record.get("run"), record.get("event")
        if not isinstance(run_id, str):
            continue
        run = runs.setdefault(run_id, _Run(run_id))
        original, moved = record.get("from"), record.get("to")
        if not (isinstance(original, str) and isinstance(moved, str)):
            continue
        if event == "move":
            run.moves.append((original, moved))
        elif event == "undone":
            run.resolved.add((original, moved))
    return list(runs.values())


def _parse_moved_path(moved_path):
    """Split "Category/name" into (category, name), or None if it looks wrong."""
    parts = moved_path.split("/")
    if len(parts) != 2:
        return None
    category, name = parts
    if category not in rules.ALL_CATEGORIES or not _is_plain_name(name):
        return None
    return category, name


def _restore_one(folder, original_name, moved_path, categories):
    """Put one file back. Returns (outcome, detail).

    outcome is "restored", "missing", "skipped" or "failed".
    """
    parsed = _parse_moved_path(moved_path)
    if parsed is None or not _is_plain_name(original_name):
        return "skipped", "refused: the log entry does not look right"
    category, name = parsed

    category_folder = Path(folder) / category
    if not os.path.lexists(category_folder):
        return "missing", None
    if category not in categories:
        categories[category] = inspect_category_folder(folder, category)[0]
    if categories[category] is not None:
        return "skipped", categories[category]

    source = category_folder / name
    if os.path.islink(source):
        return "skipped", "it is now a link, not the file that was moved"
    try:
        info = os.lstat(source)
    except FileNotFoundError:
        return "missing", None
    except OSError as exc:
        return "skipped", "could not be read (%s)" % (
            exc.strerror or exc.__class__.__name__)
    if not stat.S_ISREG(info.st_mode):
        return "skipped", "it is no longer a regular file"

    # Same trick as apply: reserve the original name first, so we can never
    # land on top of a file that has appeared there since.
    target = Path(folder) / original_name
    try:
        if not _reserve(target):
            return "skipped", "the original name '%s' is already taken" % original_name
    except OSError as exc:
        return "failed", "could not reserve the original name (%s)" % (
            exc.strerror or exc.__class__.__name__)
    try:
        os.replace(source, target)
    except OSError as exc:
        removed = _remove_placeholder(target)
        reason = "could not be moved back, probably in use by another program (%s)" % (
            exc.strerror or exc.__class__.__name__)
        if not removed:
            reason += "; an empty placeholder file was left at %s" % target
        return "failed", reason
    return "restored", None


def undo_last(folder):
    """Undo the newest apply that still has files to put back.

    Files whose original name is taken, or that can't be moved, stay where
    they are and are reported; run undo again after fixing the problem to
    retry just those. Never overwrites, deletes, or leaves the folder.
    """
    folder = Path(folder)
    restored, missing, skipped, failed = [], [], [], []

    def result(run_id=None, error=None):
        return UndoResult(run_id, restored, missing, skipped, failed, error)

    try:
        runs = read_runs(folder)
    except LogError as exc:
        return result(error=str(exc))
    pending = [run for run in runs if run.outstanding()]
    if not pending:
        return result()
    run = pending[-1]

    try:
        log = _Log(folder)
    except LogError as exc:
        return result(run.run_id, str(exc))

    error = None
    categories = {}
    try:
        try:
            log.write({"event": "undo_start", "run": run.run_id,
                       "time": _utc_timestamp()})
        except OSError as exc:
            return result(run.run_id, "could not write the undo log (%s), so "
                          "nothing was changed" % (exc.strerror or exc.__class__.__name__))

        for original, moved_path in reversed(run.outstanding()):
            outcome, detail = _restore_one(folder, original, moved_path, categories)
            if outcome == "skipped":
                skipped.append((moved_path, detail))
                continue
            if outcome == "failed":
                failed.append((moved_path, detail))
                continue
            if outcome == "restored":
                restored.append((original, moved_path))
            else:
                missing.append(moved_path)
            try:
                log.write({"event": "undone", "run": run.run_id, "from": original,
                           "to": moved_path, "status": outcome})
            except OSError as exc:
                error = ("stopped: %s was handled but could not be written to the "
                         "undo log (%s); no further files were touched" % (
                             moved_path, exc.strerror or exc.__class__.__name__))
                break

        if error is None:
            try:
                log.write({"event": "undo_end", "run": run.run_id,
                           "restored": len(restored), "left": len(skipped) + len(failed)})
            except OSError:
                pass
    finally:
        log.close()
    return result(run.run_id, error)


def format_result(result, folder):
    """Return a plain-text report of what apply did."""
    folder = Path(folder)
    lines = []

    if result.moved:
        lines.append("Moved %s:" % _count(len(result.moved), "file"))
        for move in result.moved:
            note = "  (renamed: that name was already taken)" if move.renamed else ""
            lines.append("  %s -> %s%s" % (
                move.source.name, move.destination.relative_to(folder), note))
    else:
        lines.append("Nothing was moved.")

    if result.failed:
        lines.append("")
        lines.append("Could not move %s (left where they were):" % _count(
            len(result.failed), "file"))
        for path, reason in result.failed:
            lines.append("  %s  (%s)" % (Path(path).name, reason))

    if result.skipped:
        lines.append("")
        lines.append("Skipped %s:" % _count(len(result.skipped), "item"))
        for path, reason in result.skipped:
            lines.append("  %s  (%s)" % (Path(path).name, reason))

    if result.error:
        lines.append("")
        lines.append("STOPPED: %s" % result.error)
    return "\n".join(lines)


def format_undo_result(result):
    """Return a plain-text report of what undo did."""
    if result.run_id is None and not result.error:
        return "Nothing to undo."

    lines = []
    if result.restored:
        lines.append("Put back %s:" % _count(len(result.restored), "file"))
        for original, moved_path in result.restored:
            lines.append("  %s -> %s" % (Path(*moved_path.split("/")), original))
    else:
        lines.append("Nothing was put back.")

    if result.missing:
        lines.append("")
        lines.append("Already gone, so nothing to put back (%d):" % len(result.missing))
        for moved_path in result.missing:
            lines.append("  %s" % Path(*moved_path.split("/")))

    left = list(result.skipped) + list(result.failed)
    if left:
        lines.append("")
        lines.append("Could not put back %s (left where they are):" % _count(
            len(left), "file"))
        for moved_path, reason in left:
            lines.append("  %s  (%s)" % (moved_path.replace("/", os.sep), reason))
        lines.append("Fix the problem, then run undo again to retry these.")

    if result.error:
        lines.append("")
        lines.append("STOPPED: %s" % result.error)
    return "\n".join(lines)
