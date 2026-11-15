"""Look at a folder and decide, for each item, whether it is safe to move.

The scanner only READS. It never moves, renames or changes anything. It looks
at the top level of the chosen folder only (it never goes into subfolders),
so everything it reports is a direct child of that folder.

Every item is either:
  * a candidate to sort (safe to move), or
  * skipped, with a short human-readable reason.
"""

import os
import stat
import time
from collections import namedtuple
from pathlib import Path

from . import rules

# The reasons a file can be skipped. These are shown to the user.
SUBFOLDER = "subfolder"
SYMLINK = "symbolic link"
NOT_REGULAR = "not a regular file"
UNREADABLE = "could not be read"
OWN_LOG = "the tool's own undo log"
SYSTEM = "system file"
HIDDEN = "hidden file"
SHORTCUT = "shortcut"
PARTIAL = "partial download"
RECENT = "changed in the last 10 minutes"

# candidates: list of Paths that are safe to sort.
# skipped: list of (Path, reason) pairs for everything else.
ScanResult = namedtuple("ScanResult", ["candidates", "skipped"])


def _extension(name):
    return Path(name).suffix.lstrip(".").lower()


def skip_reason(path, now=None):
    """Return why this path must be skipped, or None if it is safe to move.

    `now` is the current time in seconds (tests pass a fixed value).
    """
    path = Path(path)
    name = path.name

    # Symlinks first, before anything follows them. A link to a folder or to
    # a file outside the chosen folder must never be touched.
    if os.path.islink(path):
        return SYMLINK

    try:
        info = path.lstat()  # lstat: describe the item itself, never a target
    except OSError:
        return UNREADABLE

    if stat.S_ISDIR(info.st_mode):
        return SUBFOLDER
    if not stat.S_ISREG(info.st_mode):
        return NOT_REGULAR  # sockets, devices, and similar oddities

    if name == rules.LOG_FILENAME:
        return OWN_LOG
    if name.lower() in rules.SKIP_FILE_NAMES:
        return SYSTEM

    # Windows marks files as hidden/system with attribute flags. On other
    # systems this attribute doesn't exist, so it reads as 0.
    attributes = getattr(info, "st_file_attributes", 0)
    if attributes & stat.FILE_ATTRIBUTE_SYSTEM:
        return SYSTEM
    if name.startswith(".") or attributes & stat.FILE_ATTRIBUTE_HIDDEN:
        return HIDDEN

    extension = _extension(name)
    if extension in rules.SHORTCUT_EXTENSIONS:
        return SHORTCUT
    if extension in rules.PARTIAL_DOWNLOAD_EXTENSIONS:
        return PARTIAL

    if now is None:
        now = time.time()
    # A modification time in the future (clock skew) counts as recent too,
    # because the age is then negative.
    if now - info.st_mtime < rules.RECENT_SECONDS:
        return RECENT

    return None


def scan_folder(folder, now=None):
    """Sort the direct contents of `folder` into candidates and skipped.

    Raises NotADirectoryError if `folder` isn't an existing folder.
    Results are ordered by name so output is the same every time.
    """
    folder = Path(folder)
    if not folder.is_dir():
        raise NotADirectoryError("Not a folder: %s" % folder)

    candidates = []
    skipped = []
    for path in sorted(folder.iterdir(), key=lambda p: p.name):
        reason = skip_reason(path, now=now)
        if reason is None:
            candidates.append(path)
        else:
            skipped.append((path, reason))
    return ScanResult(candidates, skipped)
