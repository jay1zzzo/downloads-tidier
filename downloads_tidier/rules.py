"""The sorting rules: which file extensions go into which category folder.

This is the one place to edit if you want to change how files are sorted.
Add or remove extensions below (lowercase, without the leading dot). Anything
not listed here goes into the "Other" folder.
"""

from pathlib import Path

# Folder name -> the file extensions that belong in it.
# Each extension should appear in only one category (a test checks this).
CATEGORIES = {
    "Images": (
        "jpg", "jpeg", "png", "gif", "webp", "heic", "bmp", "tiff", "svg",
    ),
    "Documents": (
        "pdf", "doc", "docx", "txt", "rtf", "odt", "md", "pages",
        "ppt", "pptx", "key",
    ),
    "Spreadsheets": (
        "xls", "xlsx", "csv", "ods", "numbers",
    ),
    "Archives": (
        "zip", "rar", "7z", "tar", "gz", "tgz", "bz2", "xz",
    ),
    "Installers": (
        "exe", "msi", "dmg", "pkg", "appimage", "deb", "rpm", "iso",
    ),
    "Video": (
        "mp4", "mov", "mkv", "avi", "webm", "m4v", "wmv",
    ),
    "Audio": (
        "mp3", "wav", "flac", "m4a", "aac", "ogg", "wma",
    ),
}

# Where files go when their extension isn't listed above (or they have none).
OTHER = "Other"

# --- Files the tool must leave alone -------------------------------------

# Exact file names to skip (written in lowercase; matching ignores case).
SKIP_FILE_NAMES = (
    ".ds_store", "desktop.ini", "thumbs.db", "ehthumbs.db",
)

# Shortcuts and launchers (extensions, lowercase, no dot).
SHORTCUT_EXTENSIONS = (
    "lnk", "url", "webloc", "desktop",
)

# Half-downloaded files (extensions, lowercase, no dot).
PARTIAL_DOWNLOAD_EXTENSIONS = (
    "crdownload", "part", "download", "tmp",
)

# Anything modified more recently than this many seconds ago is skipped,
# in case it is still being written or is in use.
RECENT_SECONDS = 10 * 60

# The undo log the tool keeps inside the chosen folder. Never moved.
LOG_FILENAME = ".downloads-tidier-log.json"

# Every folder name the tool may create, in a fixed order.
ALL_CATEGORIES = tuple(CATEGORIES) + (OTHER,)

# Quick lookup built once from the table above: extension -> category.
_EXTENSION_TO_CATEGORY = {
    ext: category
    for category, extensions in CATEGORIES.items()
    for ext in extensions
}


def category_for(filename):
    """Return the category folder name for a file name (a str or Path).

    Only the last extension counts, so "backup.tar.gz" is judged by ".gz".
    Upper/lower case doesn't matter. No extension means "Other".
    """
    extension = Path(filename).suffix.lstrip(".").lower()
    return _EXTENSION_TO_CATEGORY.get(extension, OTHER)
