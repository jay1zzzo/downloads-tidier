import unittest
from pathlib import Path

from downloads_tidier import rules
from downloads_tidier.rules import category_for


class CategoryForTests(unittest.TestCase):
    def test_known_extensions_land_in_the_right_category(self):
        examples = {
            "photo.jpg": "Images",
            "logo.png": "Images",
            "report.pdf": "Documents",
            "notes.txt": "Documents",
            "budget.xlsx": "Spreadsheets",
            "data.csv": "Spreadsheets",
            "bundle.zip": "Archives",
            "setup.exe": "Installers",
            "app.dmg": "Installers",
            "clip.mp4": "Video",
            "song.mp3": "Audio",
        }
        for name, expected in examples.items():
            with self.subTest(name=name):
                self.assertEqual(category_for(name), expected)

    def test_every_listed_extension_maps_to_its_own_category(self):
        for category, extensions in rules.CATEGORIES.items():
            for ext in extensions:
                with self.subTest(ext=ext):
                    self.assertEqual(category_for("file." + ext), category)

    def test_upper_and_mixed_case_extensions_match(self):
        self.assertEqual(category_for("PHOTO.JPG"), "Images")
        self.assertEqual(category_for("Report.PdF"), "Documents")
        self.assertEqual(category_for("SETUP.EXE"), "Installers")

    def test_unknown_extension_goes_to_other(self):
        self.assertEqual(category_for("mystery.xyz123"), "Other")

    def test_no_extension_goes_to_other(self):
        self.assertEqual(category_for("README"), "Other")
        self.assertEqual(category_for("trailing-dot."), "Other")

    def test_only_the_last_extension_counts(self):
        self.assertEqual(category_for("backup.tar.gz"), "Archives")
        self.assertEqual(category_for("photo.jpg.exe"), "Installers")
        self.assertEqual(category_for("report.pdf.xyz123"), "Other")

    def test_dots_elsewhere_in_the_name_are_fine(self):
        self.assertEqual(category_for("v1.2.3 notes.pdf"), "Documents")
        self.assertEqual(category_for("my.holiday.photo.png"), "Images")

    def test_accepts_path_objects_and_ignores_folders_in_the_path(self):
        self.assertEqual(category_for(Path("some.folder") / "song.mp3"), "Audio")
        self.assertEqual(category_for(Path("report.pdf")), "Documents")


class RuleTableTests(unittest.TestCase):
    def test_all_the_required_categories_exist(self):
        self.assertEqual(
            set(rules.ALL_CATEGORIES),
            {"Images", "Documents", "Spreadsheets", "Archives",
             "Installers", "Video", "Audio", "Other"},
        )

    def test_other_is_last_and_not_in_the_extension_table(self):
        self.assertEqual(rules.ALL_CATEGORIES[-1], "Other")
        self.assertNotIn("Other", rules.CATEGORIES)

    def test_no_extension_is_listed_in_two_categories(self):
        seen = {}
        for category, extensions in rules.CATEGORIES.items():
            for ext in extensions:
                self.assertNotIn(
                    ext, seen,
                    "%r is in both %s and %s" % (ext, seen.get(ext), category),
                )
                seen[ext] = category

    def test_extensions_are_written_lowercase_without_a_dot(self):
        for extensions in rules.CATEGORIES.values():
            for ext in extensions:
                with self.subTest(ext=ext):
                    self.assertEqual(ext, ext.lower())
                    self.assertFalse(ext.startswith("."))
                    self.assertTrue(ext)

    def test_category_names_are_safe_folder_names(self):
        # They become real folder names, so no path separators allowed.
        for name in rules.ALL_CATEGORIES:
            with self.subTest(name=name):
                self.assertEqual(Path(name).name, name)
                self.assertNotIn("/", name)
                self.assertNotIn("\\", name)


if __name__ == "__main__":
    unittest.main()
