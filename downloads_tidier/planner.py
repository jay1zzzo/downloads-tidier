"""Work out what the tool WOULD do, without doing any of it.

The planner takes the scanner's list of safe files and decides, for each one,
which category folder it goes into and what it will be called there. If the
name is already taken, it picks "name (1).ext", "name (2).ext", and so on.

Planning only reads the disk. It never moves, renames, creates or changes
anything, and that is what makes "preview" safe.
"""

import os
from collections import namedtuple
from pathlib import Path

from .rules import category_for
from .scanner import scan_folder

# source: the file as it is now.  destination: where it would go.
# category: the folder name.  renamed: True if the name had to change
# because the original name was already taken.
Move = namedtuple("Move", ["source", "destination", "category", "renamed"])

# moves: list of Move.  skipped: list of (Path, reason) for everything left alone.
Plan = namedtuple("Plan", ["moves", "skipped"])


def is_inside(folder, path):
    """True if `path` is strictly inside `folder`, after following any links.

    Comparing whole path parts (not text prefixes) means "/tmp/abcd/x" is
    correctly NOT inside "/tmp/abc".
    """
    folder = Path(os.path.realpath(folder))
    path = Path(os.path.realpath(path))
    return folder in path.parents


def _split_name(name):
    """Split a file name into (stem, extension) for numbering.

    Treats ".tar.gz" and friends as one extension, so the number goes before
    it: "backup.tar.gz" becomes "backup (1).tar.gz".
    """
    path = Path(name)
    stem, suffix = path.stem, path.suffix
    if len(stem) > 4 and stem.lower().endswith(".tar"):
        stem, suffix = stem[:-4], ".tar" + suffix
    return stem, suffix


def numbered_names(name):
    """Yield "name", then "name (1)", "name (2)", ... keeping the extension."""
    yield name
    stem, suffix = _split_name(name)
    number = 1
    while True:
        yield "%s (%d)%s" % (stem, number, suffix)
        number += 1


def unique_name(name, taken):
    """Return `name`, or the first numbered variant not in `taken`.

    `taken` is a set of lowercase-folded names. Names are compared ignoring
    case, because Windows and macOS treat "A.pdf" and "a.pdf" as the same file.
    """
    for candidate in numbered_names(name):
        if candidate.casefold() not in taken:
            return candidate


def inspect_category_folder(folder, category):
    """Check a category folder. Returns (problem, taken_names).

    `problem` is None if the folder is fine to move files into (it either
    doesn't exist yet, or is a normal folder inside the chosen folder).
    Otherwise it is a reason string and files for this category are skipped.
    `taken_names` are the lowercase-folded names already in the folder.
    """
    destination = Path(folder) / category
    if not os.path.lexists(destination):
        return None, set()  # will be created when applying

    # An existing "Images" that is a link, a plain file, or a folder that
    # secretly points outside the chosen folder must never be used.
    if (os.path.islink(destination) or not destination.is_dir()
            or not is_inside(folder, destination)):
        return "'%s' is not a normal folder inside the chosen folder" % category, set()
    try:
        names = os.listdir(destination)
    except OSError:
        return "the '%s' folder could not be read" % category, set()
    return None, {name.casefold() for name in names}


def plan_moves(folder, now=None):
    """Build the full plan for `folder`. Reads only; changes nothing."""
    folder = Path(folder)
    scan = scan_folder(folder, now=now)

    moves = []
    skipped = list(scan.skipped)
    inspected = {}  # category -> (problem, taken names), looked at once each

    for source in scan.candidates:
        category = category_for(source.name)
        if category not in inspected:
            inspected[category] = inspect_category_folder(folder, category)
        problem, taken = inspected[category]
        if problem:
            skipped.append((source, problem))
            continue

        name = unique_name(source.name, taken)
        taken.add(name.casefold())  # so later files in this run can't pick it too
        destination = folder / category / name
        moves.append(Move(source, destination, category, name != source.name))

    skipped.sort(key=lambda item: item[0].name)
    return Plan(moves, skipped)


def _count(number, noun):
    return "%d %s%s" % (number, noun, "" if number == 1 else "s")


def format_preview(plan, folder):
    """Return the preview as plain text, ready to print."""
    folder = Path(folder)
    lines = []

    if plan.moves:
        lines.append("Would move %s:" % _count(len(plan.moves), "file"))
        for move in plan.moves:
            target = move.destination.relative_to(folder)
            note = "  (renamed: that name was already taken)" if move.renamed else ""
            lines.append("  %s -> %s%s" % (move.source.name, target, note))
    else:
        lines.append("Nothing to move.")

    if plan.skipped:
        lines.append("")
        lines.append("Would skip %s:" % _count(len(plan.skipped), "item"))
        for path, reason in plan.skipped:
            lines.append("  %s  (%s)" % (path.name, reason))

    lines.append("")
    lines.append("This was a preview. Nothing was changed.")
    return "\n".join(lines)
