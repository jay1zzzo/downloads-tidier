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


class _Log:
    """Append-only log. Each record is flushed to disk as soon as it's written."""

    def __init__(self, folder):
        try:
            self._file = open(log_path(folder), "a", encoding="utf-8", newline="\n")
        except OSError as exc:
            raise LogError(
                "could not open the undo log (%s), so nothing was moved"
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
