"""mpypublish.toml: what is read, and what is refused before touching a board."""

import unittest

import support
from mpypublish import Error, config


class ConfigTest(unittest.TestCase):
    def load(self, toml):
        return config.load(support.make_project(self, toml=toml))

    def test_reads_the_project(self):
        settings = self.load(support.TOML)
        self.assertEqual((settings.name, settings.version), ("demo", "1.2.0"))
        self.assertFalse(settings.mpy)
        self.assertIsNone(settings.micropython)
        self.assertEqual([source.path for source in settings.sources],
                         [settings.root / "src", settings.root / "settings.example.json"])
        self.assertEqual(settings.sources[0].exclude, ["*.md", "tests/"])
        self.assertEqual(settings.ready, "Main loop started")
        self.assertEqual((settings.ota_path, settings.ready_timeout), ("/ota", 60))

    def test_found_from_a_folder_below(self):
        path = support.make_project(self)
        self.assertEqual(config.find(path.parent / "src" / "lib"), path)

    def test_no_file_anywhere(self):
        empty = support.make_project(self).parent / "src"
        (empty.parent / config.CONFIG_FILE).unlink()
        with self.assertRaisesRegex(Error, "no mpypublish.toml"):
            config.find(empty)

    def test_kept_files_and_folders(self):
        settings = self.load(support.TOML)
        for path in ("settings.json", "data/log.csv", "data/2026/10.csv"):
            self.assertTrue(settings.kept(path), path)
        for path in ("settings.json.bak", "a/settings.json", "data", "database/x"):
            self.assertFalse(settings.kept(path), path)

    def test_a_misspelled_key_is_not_ignored(self):
        with self.assertRaisesRegex(Error, "exlude"):
            self.load('[app]\nname = "demo"\n[[source]]\nfrom = "src"\nexlude = ["*.md"]\n')
        with self.assertRaisesRegex(Error, r"\[boards\]"):
            self.load('[app]\nname = "demo"\n[[source]]\nfrom = "src"\n[boards]\nkeep = []\n')

    def test_what_is_needed(self):
        with self.assertRaisesRegex(Error, "name"):
            self.load('[[source]]\nfrom = "src"\n')
        with self.assertRaisesRegex(Error, "source"):
            self.load('[app]\nname = "demo"\n')
        with self.assertRaisesRegex(Error, "from"):
            self.load('[app]\nname = "demo"\n[[source]]\nto = "lib"\n')

    def test_names_that_would_not_make_a_file_name(self):
        for name in ("my app", "demo/1", "", "x" * 33):
            with self.assertRaises(Error, msg=name):
                self.load('[app]\nname = "%s"\n[[source]]\nfrom = "src"\n' % name)

    def test_wrong_types(self):
        with self.assertRaisesRegex(Error, "mpy"):
            self.load('[app]\nname = "demo"\nmpy = "yes"\n[[source]]\nfrom = "src"\n')
        with self.assertRaisesRegex(Error, "keep"):
            self.load('[app]\nname = "demo"\n[[source]]\nfrom = "src"\n[board]\nkeep = "settings.json"\n')
        with self.assertRaisesRegex(Error, "ready_timeout"):
            self.load('[app]\nname = "demo"\n[[source]]\nfrom = "src"\n[board]\nready_timeout = "a minute"\n')

    def test_paths_stay_on_the_board(self):
        with self.assertRaisesRegex(Error, r"\.\."):
            self.load('[app]\nname = "demo"\n[[source]]\nfrom = "src"\nto = "../lib"\n')
        settings = self.load('[app]\nname = "demo"\n[[source]]\nfrom = "src"\nto = "/lib/"\n'
                             '[board]\nkeep = ["/data/"]\n[ota]\npath = "update"\n')
        self.assertEqual(settings.sources[0].to, "lib")
        self.assertEqual(settings.keep, ["data/"])
        self.assertEqual(settings.ota_path, "/update")

    def test_broken_file(self):
        with self.assertRaisesRegex(Error, "mpypublish.toml"):
            self.load('[app\nname = "demo"\n')


if __name__ == "__main__":
    unittest.main()
