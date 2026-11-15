"""Shared helpers for the tests. Every test builds its own temporary files,
inside a throwaway folder that is deleted afterwards."""

import hashlib
import json
import os
import tempfile
import time
import unittest
from collections import Counter
from pathlib import Path

from downloads_tidier import rules

# Windows file attribute flags (used only to test Windows-specific behaviour).
_WIN_HIDDEN = 0x2
_WIN_SYSTEM = 0x4


class TempFolderTestCase(unittest.TestCase):
    """Gives each test an empty temporary folder as `self.folder`."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="tidier-test-")
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name).resolve()


def make_file(folder, name, content=b"example", age=3600, now=None):
    """Create a file whose modification time is `age` seconds before `now`.

    The default age is one hour, so it counts as "not recently changed".
    Returns the Path.
    """
    path = Path(folder) / name
    path.write_bytes(content)
    if now is None:
        now = time.time()
    stamp = int(now - age)
    os.utime(path, (stamp, stamp))
    return path


def snapshot(folder):
    """Describe everything under `folder`, so two snapshots can be compared.

    Maps each relative path to (kind, size, modified-time, content hash).
    If nothing changed, two snapshots are exactly equal.
    """
    folder = Path(folder)
    result = {}
    for path in sorted(folder.rglob("*")):
        relative = path.relative_to(folder).as_posix()
        if path.is_symlink():
            result[relative] = ("symlink", os.readlink(path))
        elif path.is_dir():
            result[relative] = ("dir", path.stat().st_mtime_ns)
        else:
            info = path.stat()
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            result[relative] = ("file", info.st_size, info.st_mtime_ns, digest)
    return result


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
    """Every record in the undo log, as a list of dicts."""
    text = (Path(folder) / rules.LOG_FILENAME).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines()]


def set_windows_attributes(path, hidden=False, system=False):
    """Mark a file hidden and/or system. Windows only."""
    import ctypes

    flags = (_WIN_HIDDEN if hidden else 0) | (_WIN_SYSTEM if system else 0)
    ok = ctypes.windll.kernel32.SetFileAttributesW(str(path), flags or 0x80)
    if not ok:
        raise OSError("could not set file attributes on %s" % path)
