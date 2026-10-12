"""What ends up in a package, and what stops a build."""

import contextlib
import hashlib
import io
import json
import shutil
import subprocess
import unittest
import zipfile
from unittest import mock

import support
from mpypublish import Error, build, config

APP = {"main.py", "app.py", "lib/sensor.py", "pages/index.html", "settings.example.json"}


def app_files(package):
    """The files of the package that come from the project, without what the build adds."""
    return {path for path in package["files"]
            if path != "version.json" and not path.startswith(build.RUNTIME_FOLDER + "/")}


class BuildTest(unittest.TestCase):
    def test_package_has_the_sources_and_the_version(self):
        build_dir, package = support.build_package(self)
        self.assertEqual(app_files(package), APP)
        self.assertIn("version.json", package["files"])
        self.assertTrue((build_dir / "manifest.json").is_file())
        self.assertEqual((build_dir / "lib/sensor.py").read_text(), support.FILES["src/lib/sensor.py"])

    def test_package_brings_the_device_side_of_mpypublish(self):
        """What the app imports to confirm a trial and to be updated from the network."""
        build_dir, package = support.build_package(self)
        for name in ("__init__.py", "updater.py", "httpd.py"):
            self.assertIn("mpypublish/" + name, package["files"])
        self.assertIn("def confirm", (build_dir / "mpypublish/__init__.py").read_text())
        # boot.py is not in the package: it goes in the root, and only over USB
        self.assertEqual([path for path in package["files"] if path.endswith("boot.py")], [])

    def test_folder_of_the_device_side_is_taken(self):
        files = dict(support.FILES, **{"src/mpypublish/extra.py": "X = 1\n"})
        with self.assertRaisesRegex(Error, "mpypublish/"):
            support.build_package(self, files=files)

    def test_caches_hidden_files_and_exclusions_stay_on_the_pc(self):
        _, package = support.build_package(self)
        for path in package["files"]:
            self.assertNotIn("__pycache__", path)
            self.assertFalse(path.endswith((".md", ".env")), path)
            self.assertFalse(path.startswith("tests/"), path)

    def test_exclusion_patterns(self):
        self.assertTrue(build.excluded("notes.md", ["*.md"]))
        self.assertTrue(build.excluded("docs/notes.md", ["*.md"]))
        self.assertTrue(build.excluded("lib/tests/test_x.py", ["tests/"]))
        self.assertTrue(build.excluded("lib/old/x.py", ["lib/old/*"]))
        self.assertFalse(build.excluded("mytests/x.py", ["tests/"]))
        self.assertFalse(build.excluded("lib/x.py", ["x"]))

    def test_destination_folder(self):
        toml = support.TOML.replace('from = "src"', 'from = "src"\nto = "app"') + '\n[[source]]\nfrom = "main.py"\n'
        files = dict(support.FILES, **{"main.py": "import app.app\n"})
        _, package = support.build_package(self, toml=toml, files=files)
        self.assertIn("app/lib/sensor.py", package["files"])
        self.assertNotIn("lib/sensor.py", package["files"])
        self.assertIn("settings.example.json", package["files"])      # a single file lands at the top

    def test_project_folder_as_the_source(self):
        """The flat layout, main.py next to mpypublish.toml: build/ and the file itself are not the app."""
        toml = '[app]\nname = "flat"\n[[source]]\nfrom = "."\n'
        files = {"main.py": "print('hi')\n", "lib/x.py": "X = 1\n", "build/flat/main.py": "old build\n"}
        _, package = support.build_package(self, toml=toml, files=files)
        self.assertEqual(app_files(package), {"main.py", "lib/x.py"})

    def test_two_files_with_the_same_destination(self):
        toml = support.TOML + '\n[[source]]\nfrom = "extra"\n'
        files = dict(support.FILES, **{"extra/app.py": "other\n"})
        with self.assertRaisesRegex(Error, "both end up in :app.py"):
            support.build_package(self, toml=toml, files=files)

    def test_package_without_main_is_refused(self):
        """boot.py on the board starts the main.py of the slot: without one nothing would run."""
        files = {name: text for name, text in support.FILES.items() if name != "src/main.py"}
        with self.assertRaisesRegex(Error, "main.py"):
            support.build_package(self, files=files)

    def test_boot_in_the_package_is_refused(self):
        files = dict(support.FILES, **{"src/boot.py": "import network\n"})
        with self.assertRaisesRegex(Error, "boot.py"):
            support.build_package(self, files=files)

    def test_missing_source(self):
        toml = support.TOML.replace('from = "src"', 'from = "source"')
        with self.assertRaisesRegex(Error, "source not found"):
            support.build_package(self, toml=toml)

    def test_manifest_describes_the_package(self):
        build_dir, package = support.build_package(self)
        self.assertEqual(json.loads((build_dir / "manifest.json").read_text()), package)

        self.assertEqual((package["name"], package["version"]), ("demo", "1.2.0"))
        self.assertIsNone(package["micropython"])
        self.assertFalse(package["mpy"])
        self.assertRegex(package["built"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

        # Every file of the build but the manifest itself, with its hash
        listed = build.local_files(build_dir)
        del listed["manifest.json"]
        self.assertEqual(package["files"], listed)

        version = json.loads((build_dir / "version.json").read_text())
        self.assertEqual(version, {key: package[key] for key in ("name", "version", "git", "built")})

    def test_rebuilding_starts_from_an_empty_folder(self):
        path = support.make_project(self)
        settings = config.load(path)
        with contextlib.redirect_stdout(io.StringIO()):
            build_dir, _ = build.build(settings, False)
            (path.parent / "src/app.py").unlink()
            _, package = build.build(settings, False)
        self.assertNotIn("app.py", package["files"])
        self.assertFalse((build_dir / "app.py").exists())

    def test_package_in_one_file(self):
        build_dir, package = support.build_package(self)
        path = build.write_package_zip(build_dir, package)
        self.assertEqual(path, build_dir.parent / "demo-1.2.0.zip")
        with zipfile.ZipFile(path) as archive:
            self.assertEqual(set(archive.namelist()), set(package["files"]) | {"manifest.json"})
            self.assertEqual(json.loads(archive.read("manifest.json")), package)
            for name, item in package["files"].items():
                self.assertEqual(hashlib.sha256(archive.read(name)).hexdigest(), item["sha256"], name)


@unittest.skipUnless(shutil.which("git"), "git not installed")
class RevisionTest(unittest.TestCase):
    def git(self, folder, *args):
        subprocess.run(["git", "-c", "user.name=test", "-c", "user.email=test@example.com",
                        "-c", "commit.gpgsign=false", *args], cwd=folder, capture_output=True, check=True)

    def test_commit_then_dirty(self):
        folder = support.make_project(self).parent
        self.assertIsNone(build.git_revision(folder))

        self.git(folder, "init")
        self.git(folder, "add", ".")
        self.git(folder, "commit", "-m", "first")
        self.assertRegex(build.git_revision(folder), r"^[0-9a-f]{7,}$")

        (folder / "src/app.py").write_text("changed\n")
        self.assertRegex(build.git_revision(folder), r"^[0-9a-f]{7,}-dirty$")


@unittest.skipUnless(build.mpy_cross_path(), "mpy-cross not installed")
class CompiledBuildTest(unittest.TestCase):
    def test_everything_is_compiled_but_main(self):
        build_dir, package = support.build_package(self, use_mpy=True)
        self.assertTrue(package["mpy"])
        self.assertEqual([path for path in package["files"] if path.endswith(".py")], ["main.py"])
        self.assertIn("lib/sensor.mpy", package["files"])
        self.assertIn("pages/index.html", package["files"])
        for path in package["files"]:
            if path.endswith(".mpy"):
                self.assertEqual((build_dir / path).read_bytes()[:1], b"M", path)

    def test_package_says_which_micropython_it_is_for(self):
        _, package = support.build_package(self, use_mpy=True)
        self.assertEqual(package["micropython"], build.mpy_cross_version(build.mpy_cross_path()))

    def test_tracebacks_show_the_path_on_the_board(self):
        build_dir, _ = support.build_package(self, use_mpy=True)
        compiled = (build_dir / "lib/sensor.mpy").read_bytes()
        self.assertIn(b"lib/sensor.py", compiled)
        self.assertNotIn(b"src", compiled)

    def test_same_sources_give_the_same_package(self):
        """Hashes must not depend on where the build is: they compare board and package."""
        first = support.build_package(self, use_mpy=True)[1]["files"]
        second = support.build_package(self, use_mpy=True)[1]["files"]
        del first["version.json"], second["version.json"]             # carries the date
        self.assertEqual(first, second)

    def test_mpy_cross_of_another_series_stops_the_build(self):
        toml = support.TOML.replace('version = "1.2.0"', 'version = "1.2.0"\nmicropython = "1.27.0"')
        with mock.patch.object(build, "mpy_cross_version", return_value="1.24.1"):
            with self.assertRaisesRegex(Error, "1.24.1"):
                support.build_package(self, use_mpy=True, toml=toml)
        # A patch release reads the same .mpy
        with mock.patch.object(build, "mpy_cross_version", return_value="1.27.1"):
            support.build_package(self, use_mpy=True, toml=toml)

    def test_missing_mpy_cross_stops_the_build(self):
        """No silent fallback to the sources: the board would get a package other than the expected one."""
        with mock.patch.object(build, "mpy_cross_path", return_value=None):
            with self.assertRaisesRegex(Error, "--no-mpy"):
                support.build_package(self, use_mpy=True)
            support.build_package(self, use_mpy=False)

    def test_source_that_does_not_compile(self):
        files = dict(support.FILES, **{"src/app.py": "def run(:\n"})
        with self.assertRaisesRegex(Error, "app.py does not compile"):
            support.build_package(self, use_mpy=True, files=files)


if __name__ == "__main__":
    unittest.main()
