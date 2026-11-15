# downloads-tidier

A small command-line tool that sorts the files in a messy folder (usually
Downloads) into subfolders by file type, **safely**.

- Python 3.9+, **standard library only** (nothing to install)
- Works on Windows and macOS, Linux too
- Three commands: `preview` (the default, changes nothing), `apply`, `undo`

```
Downloads/
  report.pdf, photo.jpg, setup.exe, ...      →      Documents/  Images/  Installers/  ...
```

## Quick start

Get the code (download the ZIP or clone this repository), open a terminal in
its folder, and run:

```bash
python -m downloads_tidier            # preview your Downloads folder. Changes nothing.
python -m downloads_tidier apply      # move the files
python -m downloads_tidier undo       # put everything from the last apply back
```

On some systems the command is `python3` or `py` instead of `python`.

Optionally, install it so you can type `downloads-tidier` from anywhere
(this downloads the standard build tool, `setuptools`, once):

```bash
pip install .
downloads-tidier            # same as: python -m downloads_tidier
downloads-tidier apply
```

To tidy a different folder, add `--folder`:

```bash
python -m downloads_tidier --folder "C:\Users\you\Desktop"
python -m downloads_tidier apply --folder ~/Desktop
```

**Always run a preview first and read it.** Nothing is moved until you run `apply`.

## What it looks like

Preview (the default):

```
Previewing: C:\Users\you\Downloads

Would move 8 files:
  budget.xlsx -> Spreadsheets\budget.xlsx
  holiday.png -> Images\holiday.png
  invoice.pdf -> Documents\invoice.pdf
  notes -> Other\notes
  photo.jpg -> Images\photo (1).jpg  (renamed: that name was already taken)
  setup.exe -> Installers\setup.exe
  song.mp3 -> Audio\song.mp3
  vacation.zip -> Archives\vacation.zip

Would skip 6 items:
  .DS_Store  (system file)
  Images  (subfolder)
  Old Projects  (subfolder)
  just-downloaded.pdf  (changed in the last 10 minutes)
  movie.mp4.crdownload  (partial download)
  shortcut.lnk  (shortcut)

This was a preview. Nothing was changed.

To move these files for real, run again with "apply".
```

`apply` prints the same list as "Moved 8 files", and `undo` prints
"Put back 8 files" with each file and where it went back to.

## Where files go

| Folder | File types |
|---|---|
| Images | jpg, jpeg, png, gif, webp, heic, bmp, tiff, svg |
| Documents | pdf, doc, docx, txt, rtf, odt, md, pages, ppt, pptx, key |
| Spreadsheets | xls, xlsx, csv, ods, numbers |
| Archives | zip, rar, 7z, tar, gz, tgz, bz2, xz |
| Installers | exe, msi, dmg, pkg, appimage, deb, rpm, iso |
| Video | mp4, mov, mkv, avi, webm, m4v, wmv |
| Audio | mp3, wav, flac, m4a, aac, ogg, wma |
| Other | everything else, including files with no extension |

Matching ignores upper/lower case and uses the last extension only
(`backup.tar.gz` counts as `.gz`).

### Changing the rules

All the rules live in one small file: [`downloads_tidier/rules.py`](downloads_tidier/rules.py).
Add or remove extensions in the table at the top. The same file lists the
names, shortcut types and partial-download types that are skipped, and the
10-minute limit.

## How it stays safe

- **Nothing is ever deleted.** The one exception is internal: to claim a new
  file name, the tool first creates an empty placeholder file. If a move then
  fails, it removes that empty placeholder it made itself, and nothing else.
- **Nothing is ever overwritten.** The new name is claimed first, and the
  operating system refuses if anything is already there. If a name clashes,
  the incoming file is renamed `name (1).ext`, `name (2).ext`, and so on.
  Names are compared ignoring case, as Windows and macOS do.
- **Nothing leaves the chosen folder.** Only the files directly inside the
  folder are looked at. Category folders must be ordinary folders inside it:
  if one is a link, a plain file, or points somewhere else, files for that
  category are skipped. It also refuses to run on the top of a drive, on your
  whole home folder, or on system folders.
- **Moves are instant renames, never copies.** Files go into subfolders of the
  same folder, so they are renamed in place. The tool never uses
  copy-then-delete.
- **These are skipped, and reported with a reason:** subfolders, symbolic
  links, shortcuts (`.lnk`, `.url`, `.webloc`, `.desktop`), hidden and system
  files (names starting with a dot, the Windows hidden/system flags,
  `desktop.ini`, `Thumbs.db`, `.DS_Store`), half-downloaded files
  (`.crdownload`, `.part`, `.download`, `.tmp`), and anything modified in the
  last 10 minutes.
- **A file that can't be moved doesn't stop the run.** If a file is open in
  another program, or the system refuses, it is left where it is and listed in
  the report. Everything else carries on.
- **The folder is checked again just before each move**, in case something
  changed since the preview.

## Undo

Every `apply` is recorded in a hidden log file inside the folder you tidied
(`.downloads-tidier-log.jsonl`). The log is only ever added to, one line per
move, and contains no full paths. The tool does not move anything unless it
can write to the log first.

`undo` puts back the most recent apply. Run it again to undo the one before,
and so on.

- A restored file goes back to its original name using the same
  never-overwrite method. If that name is taken again (say you downloaded a new
  `report.pdf` since), that file is **left where it is** and listed. Sort out
  the clash yourself, then run `undo` again to retry just those files.
- A file you already deleted or moved yourself is reported as "already gone".
- Empty category folders are left in place. The tool never deletes anything,
  including folders.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | All fine |
| 1 | Finished, but some files could not be handled (or the run stopped early) |
| 2 | Could not start (for example the folder doesn't exist) |

## Known limits

- **Modified time decides "recent".** A browser that keeps a download's original
  server date can make a brand-new file look old. Partial downloads are still
  protected by their extension.
- **macOS Finder aliases aren't detected.** Python sees them as ordinary files.
  Shortcut files are skipped by extension only.
- **Default folder:** on Windows the tool follows a relocated Downloads folder
  (it reads the location from the registry). On Linux it uses `~/Downloads`
  and doesn't follow a relocated one. Use `--folder` if yours is elsewhere.
- **Undo restores whatever file is at the recorded spot.** If you replaced
  `Documents/report.pdf` with a different file since, undo moves that one back.
- **If one file can't be restored, undo stays on that run.** Older applies wait
  until it is resolved.
- **It only looks at the top level** of the chosen folder, never inside
  subfolders.

## Running the tests

```bash
python -m unittest
```

The tests build their own temporary folders and files, and never touch real
folders. Some tests are skipped where the system doesn't allow them, for
example creating real symbolic links on Windows without Developer Mode.

A GitHub Actions workflow ([`.github/workflows/tests.yml`](.github/workflows/tests.yml))
runs the tests on Windows, macOS and Linux with Python 3.9 and 3.13.

## Project layout

```
downloads_tidier/
  rules.py      the sorting rules and skip lists (the file to edit)
  scanner.py    decides which files are safe to move, and why others are skipped
  planner.py    works out the moves and clash renames; builds the preview
  mover.py      the safe move, the undo log, apply and undo
  cli.py        the preview / apply / undo commands
tests/          tests for each part
```

## License

[MIT](LICENSE)
