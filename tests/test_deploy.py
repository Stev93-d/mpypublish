"""The decisions of a deploy over USB that need no board connected."""

import contextlib
import io
import unittest
from pathlib import Path
from unittest import mock

import support
from mpypublish import Error, board, config, deploy

KEPT = config.Config(root=Path("."), name="demo", keep=["settings.json", "data/"]).kept


def entry(content):
    """Entry of a list of files: it is enough that different contents have different hashes."""
    return {"sha256": "hash of " + content, "size": len(content)}


class SyncTest(unittest.TestCase):
    """What is uploaded, what is removed and what is left alone."""

    # The package in slot /a, plus the two files boot.py chooses it with
    WANTED = {
        "a/main.py": entry("new main"),
        "a/app.mpy": entry("app"),
        "a/drivers/sensor.mpy": entry("sensor"),
        "a/pages/index.html": entry("page"),
        "a/version.json": entry("new version"),
        "a/manifest.json": entry("new manifest"),
        "boot.py": entry("selector"),
        "ota.json": entry("active: a"),
    }

    # A board deployed by hand, stopped for the upload: its code is in the root
    BOARD = {
        "hold": entry(""),
        "main.py.off": entry("old main"),
        "app.py": entry("app"),
        "drivers/sensor.py": entry("sensor"),
        "drivers/old_display.py": entry("display"),
        "pages/index.html": entry("old page"),
        "version.json": entry("written by hand"),
        "settings.json": entry("settings"),
        "data/log.csv": entry("history"),
        "lib/umqtt/simple.mpy": entry("installed with mip"),
        "ota.pin": entry("pin"),
        "boot.py": entry("the one MicroPython creates"),
    }

    DATA = ("settings.json", "data/log.csv", "lib/umqtt/simple.mpy", "ota.pin")

    def plan(self, board_files, slot="a"):
        return deploy.plan_sync(self.WANTED, board_files, slot, KEPT)

    def test_first_deploy_removes_the_code_in_the_root(self):
        plan = self.plan(self.BOARD)
        self.assertEqual(plan["upload"], sorted(self.WANTED))        # boot.py too: MicroPython's does not choose
        self.assertEqual(plan["delete"], [
            "app.py",
            "drivers/old_display.py",
            "drivers/sensor.py",
            "pages/index.html",
            "version.json",
        ])
        self.assertEqual(plan["unknown"], [])

    def test_data_libraries_and_pin_are_never_touched(self):
        plan = self.plan(self.BOARD)
        for path in self.DATA + ("main.py.off", "hold"):
            self.assertNotIn(path, plan["delete"] + plan["unknown"] + plan["upload"])

    def test_what_the_user_is_asked_about(self):
        """The parked main.py is the app the board was running: it goes with the rest."""
        self.assertEqual(deploy.code_in_the_root(self.plan(self.BOARD), self.BOARD), [
            "main.py", "app.py", "drivers/old_display.py", "drivers/sensor.py", "pages/index.html", "version.json",
        ])
        with_slots = dict(self.WANTED, **{"a/old.mpy": entry("old"), "ota.json.tmp": entry("half written")})
        self.assertEqual(deploy.code_in_the_root(self.plan(with_slots), with_slots), [])

    def test_up_to_date_board_needs_nothing(self):
        files = dict(self.WANTED, **{"settings.json": entry("settings")})
        self.assertEqual(self.plan(files), {"upload": [], "delete": [], "unknown": []})

    def test_only_changed_files_are_uploaded(self):
        files = dict(self.WANTED, **{"a/app.mpy": entry("app of before")})
        del files["a/drivers/sensor.mpy"]
        self.assertEqual(self.plan(files)["upload"], ["a/app.mpy", "a/drivers/sensor.mpy"])

    def test_the_other_copy_is_the_version_of_before_and_stays(self):
        files = dict(self.WANTED, **{
            "b/main.py": entry("main of before"),
            "b/app.mpy": entry("app of before"),
            "b/drivers/gone.mpy": entry("removed since"),
        })
        self.assertEqual(self.plan(files), {"upload": [], "delete": [], "unknown": []})

    def test_the_copy_being_loaded_keeps_only_the_package(self):
        """An x.py left behind would hide x.mpy: MicroPython looks for the source first."""
        files = dict(self.WANTED, **{
            "a/app.py": entry("app"),
            "a/pages/old.html": entry("old page"),
            "a/notes.txt": entry("notes"),
        })
        self.assertEqual(self.plan(files)["delete"], ["a/app.py", "a/notes.txt", "a/pages/old.html"])

    def test_unknown_files_are_reported_and_left(self):
        files = dict(self.WANTED, **{
            "notes.txt": entry("notes"),
            "measures/october.csv": entry("measures"),
            "settings.json.tmp": entry("interrupted save"),
        })
        plan = self.plan(files)
        self.assertEqual(plan["delete"], [])
        self.assertEqual(plan["unknown"], ["measures/october.csv", "notes.txt", "settings.json.tmp"])

    def test_interrupted_state_file_is_removed(self):
        files = dict(self.WANTED, **{"ota.json.tmp": entry("half written")})
        self.assertEqual(self.plan(files)["delete"], ["ota.json.tmp"])

    def test_room_needed(self):
        on_board = {"a/app.mpy": {"sha256": "old", "size": 900}}
        wanted = {"a/app.mpy": {"sha256": "new", "size": 1000}, "a/main.py": {"sha256": "x", "size": 300}}
        self.assertEqual(deploy.room_needed(wanted, on_board, ["a/app.mpy", "a/main.py"]), 400)


class SlotTest(unittest.TestCase):
    """Which copy gets the upload, and in what order the files arrive."""

    RUNNING_A = {"a/main.py": entry("main"), "a/app.mpy": entry("app")}

    def test_new_board_starts_from_the_first_copy(self):
        self.assertEqual(deploy.target_slot(None, {}), "a")
        self.assertEqual(deploy.target_slot(None, {"main.py.off": entry("main"), "app.py": entry("x")}), "a")

    def test_uploads_into_the_copy_that_is_not_running(self):
        self.assertEqual(deploy.target_slot({"active": "a"}, self.RUNNING_A), "b")
        both = dict(self.RUNNING_A, **{"b/main.py": entry("new main")})
        self.assertEqual(deploy.target_slot({"active": "b"}, both), "a")

    def test_copy_on_trial_is_the_one_replaced(self):
        both = dict(self.RUNNING_A, **{"b/main.py": entry("main on trial")})
        self.assertEqual(deploy.target_slot({"active": "a", "trial": "b", "boots": 1}, both), "b")

    def test_state_pointing_to_a_missing_copy_does_not_count(self):
        self.assertEqual(deploy.target_slot({"active": "b"}, self.RUNNING_A), "a")
        self.assertEqual(deploy.target_slot({"active": "c"}, self.RUNNING_A), "a")
        self.assertEqual(deploy.target_slot({"active": "a"}, {"a/app.mpy": entry("without main")}), "a")

    def test_image_is_the_package_in_its_copy_plus_selector_and_state(self):
        build_dir, package = support.build_package(self)
        image = deploy.device_image(build_dir, "b")
        self.assertEqual(set(image),
                         {"b/" + path for path in package["files"]} | {"b/manifest.json", "boot.py", "ota.json"})
        self.assertEqual(image["b/lib/sensor.py"], build_dir / "lib/sensor.py")
        self.assertEqual(image["boot.py"], deploy.BOOT_SOURCE)
        self.assertTrue(deploy.BOOT_SOURCE.is_file())
        self.assertEqual(image["ota.json"].read_text(), '{"active": "b"}')
        # Written next to the build, not inside: it is not part of the package
        self.assertNotIn(build_dir, image["ota.json"].parents)

    def test_what_makes_the_copy_active_goes_last(self):
        paths = ["ota.json", "boot.py", "a/main.py", "a/lib/sensor.mpy", "a/app.mpy"]
        self.assertEqual(sorted(paths, key=deploy.upload_order), [
            "a/app.mpy", "a/lib/sensor.mpy", "a/main.py", "boot.py", "ota.json",
        ])


class FileListTest(unittest.TestCase):
    def test_reads_the_listing_of_the_board(self):
        output = (
            "F 15287 0a1b2c main.py\r\n"
            "F 571 ffee00 lib/sensor.mpy\r\n"
            "F 12 abc123 test notes.txt\r\n"
            "END\r\n"
        )
        self.assertEqual(deploy.parse_file_list(output), {
            "main.py": {"sha256": "0a1b2c", "size": 15287},
            "lib/sensor.mpy": {"sha256": "ffee00", "size": 571},
            "test notes.txt": {"sha256": "abc123", "size": 12},
        })

    def test_empty_board(self):
        self.assertEqual(deploy.parse_file_list("END\n"), {})

    def test_truncated_listing_is_refused(self):
        """With half a list the files not seen would look missing, or would not be removed."""
        with self.assertRaises(ValueError):
            deploy.parse_file_list("F 15287 0a1b2c main.py\n")

    def test_hashes_match_those_of_the_build(self):
        _, package = support.build_package(self)
        listing = "".join(
            f"F {item['size']} {item['sha256']} {path}\n" for path, item in package["files"].items()
        ) + "END\n"
        self.assertEqual(deploy.plan_sync(package["files"], deploy.parse_file_list(listing))["upload"], [])

    def test_data_folders_and_lib_are_not_even_read(self):
        """A year of measures would take minutes to hash, for files a deploy never touches."""
        settings = config.Config(root=Path("."), name="demo", keep=["settings.json", "data/", "logs/2026/"])
        self.assertEqual(deploy.skipped_folders(settings), ["lib", "data", "logs/2026"])

        answer = mock.Mock(returncode=0, stdout="END\n")
        with mock.patch.object(board, "run_mpremote", return_value=answer) as run:
            deploy.read_device_files("COM8", ["lib", "data"])
        script = run.call_args[0][2]
        self.assertIn("SKIP = ('lib', 'data')\n", script)
        compile(script, "listing", "exec")


class CopyTest(unittest.TestCase):
    """The copy of the files, with the watchdog that restarts a board that hangs."""

    WANTED = {
        "a/app.mpy": entry("app"),
        "a/main.py": entry("main"),
        "a/lib/sensor.mpy": entry("sensor"),
        "boot.py": entry("selector"),
        "ota.json": entry("active: a"),
    }
    IMAGE = {path: Path("build") / path for path in WANTED}

    def test_commands_create_folders_and_feed_the_watchdog(self):
        image = {
            "b/lib/fonts/small.mpy": Path("build") / "lib/fonts/small.mpy",
            "boot.py": Path("device") / "boot.py",
        }
        commands = deploy.copy_commands(image, ["b/lib/fonts/small.mpy", "boot.py"])
        self.assertEqual(len(commands), 1)
        args = commands[0]

        self.assertEqual(args[0], "exec")
        compile(args[1], "copy", "exec")
        self.assertIn("machine.WDT(timeout=60000)", args[1])
        self.assertIn("['b', 'b/lib', 'b/lib/fonts']", args[1])
        self.assertEqual(args[2:], [
            "+", "cp", str(Path("build") / "lib/fonts/small.mpy"), ":/b/lib/fonts/small.mpy",
            "+", "exec", board.FEED_WATCHDOG,
            "+", "cp", str(Path("device") / "boot.py"), ":/boot.py", "+", "exec", board.FEED_WATCHDOG,
        ])

    def test_many_files_are_split_in_groups(self):
        paths = ["a/lib/m%02d.mpy" % n for n in range(40)]
        commands = deploy.copy_commands({path: Path("build") / path for path in paths}, paths, group=15)
        self.assertEqual([args.count("cp") for args in commands], [15, 15, 10])
        for args in commands:
            self.assertIn("machine.WDT", args[1])                    # every invocation arms the watchdog again

    def test_removal_goes_in_groups_and_takes_the_empty_folders(self):
        scripts = []
        paths = ["a/lib/m%02d.mpy" % n for n in range(45)]
        with mock.patch.object(board, "mpremote_step",
                               side_effect=lambda target, what, *args: scripts.append(args[1]) or True):
            self.assertTrue(deploy.remove_device_files("COM8", paths))
        self.assertEqual(len(scripts), 2)
        for script in scripts:
            compile(script, "removal", "exec")
            self.assertIn("os.rmdir(p)", script)
        self.assertIn("'a/lib/m39.mpy']", scripts[0])
        self.assertIn("['a/lib/m40.mpy'", scripts[1])

    def sync(self, listings, copies, board_returns=True):
        """
        sync_files with a fake board: listings are the lists it gives at
        every reading, copies the outcome of every copy. Returns what was copied.
        """
        copied = []
        listings = list(listings)
        copies = list(copies)

        def copy(target, image, paths):
            copied.append(list(paths))
            return copies.pop(0)

        self.output = io.StringIO()
        with mock.patch.object(deploy, "read_device_files", side_effect=lambda target, skip=(): listings.pop(0)), \
                mock.patch.object(deploy, "copy_files", side_effect=copy), \
                mock.patch.object(board, "blind_reset"), \
                mock.patch.object(board, "wait_for_board", return_value=board_returns) as self.wait, \
                contextlib.redirect_stdout(self.output):
            self.present = deploy.sync_files("COM8", self.IMAGE, self.WANTED)
        return copied

    def test_board_already_up_to_date(self):
        self.assertEqual(self.sync([dict(self.WANTED)], []), [])
        self.assertEqual(self.present, self.WANTED)

    def test_main_then_selector_then_state_are_copied_last(self):
        """A board that restarts halfway through the copy must not find a copy that looks whole."""
        copied = self.sync([{}, dict(self.WANTED)], [True])
        self.assertEqual(copied, [["a/app.mpy", "a/lib/sensor.mpy", "a/main.py", "boot.py", "ota.json"]])
        self.wait.assert_not_called()

    def test_copy_resumes_after_the_board_hangs(self):
        half = {"a/app.mpy": entry("app")}
        copied = self.sync([{}, half, dict(self.WANTED)], [False, True])
        self.assertEqual(copied[1], ["a/lib/sensor.mpy", "a/main.py", "boot.py", "ota.json"])   # only what was missing
        self.wait.assert_called_once()
        self.assertIn("watchdog", self.output.getvalue())

    def test_file_that_arrived_broken_is_copied_again(self):
        broken = dict(self.WANTED, **{"a/main.py": entry("half a main")})
        copied = self.sync([{}, broken, dict(self.WANTED)], [True, True])
        self.assertEqual(copied[1], ["a/main.py"])

    def test_board_that_never_comes_back(self):
        with self.assertRaisesRegex(Error, "did not come back"):
            self.sync([{}], [False], board_returns=False)
        self.assertIn("Unplug the USB cable", self.output.getvalue())

    def test_gives_up_when_files_never_arrive_whole(self):
        broken = dict(self.WANTED, **{"a/main.py": entry("half a main")})
        with self.assertRaisesRegex(Error, "main.py"):
            self.sync([broken] * 4, [True] * 4)


class FakeBoard:
    """
    A board for deploy(): a dictionary of files in place of the flash, and
    the list of what was done to it, in order.
    """

    def __init__(self, files=None, state=None, micropython="1.27.0", free=1000000):
        self.files = dict(files or {})
        self.state = state
        self.micropython = micropython
        self.free = free
        self.stops = True
        self.arrives = True             # False: the copy fails
        self.resets = True
        self.stubborn = []              # files that cannot be removed
        self.log = []

    def stop_app(self, target):
        self.log.append("stop")
        return self.stops

    def read_status(self, target, tree=False):
        slots = {slot: {"version": "0.9.0"} for slot in ("a", "b") if slot + "/main.py" in self.files}
        return {"micropython": self.micropython, "state": self.state, "slots": slots,
                "pin": False, "hold": "hold" in self.files, "free": self.free}

    def reset(self, target):
        self.log.append("reset")
        return self.resets

    def write_root_file(self, target, name, text):
        self.log.append(("write", name, text))
        return True

    def read_device_files(self, target, skip=()):
        return dict(self.files)

    def remove_device_files(self, target, paths):
        self.log.append(("remove", sorted(paths)))
        for path in paths:
            if path not in self.stubborn:
                del self.files[path]
        return True

    def sync_files(self, target, image, wanted, skip=()):
        self.log.append("copy")
        if not self.arrives:
            raise Error("the board did not come back")
        self.files.update(wanted)
        return dict(self.files)

    def release(self, target, uploaded):
        self.log.append(("release", uploaded))
        return True

    def restart_previous_app(self, target):
        self.log.append("previous app restarted")
        return True


class DeployTest(unittest.TestCase):
    """The whole deploy, in the order things happen, on a board that is a dictionary."""

    HAND_DEPLOYED = {
        "hold": entry(""),
        "main.py.off": entry("old main"),
        "app.py": entry("old app"),
        "settings.json": entry("settings"),
        "boot.py": entry("the one MicroPython creates"),
    }

    def deploy(self, fake, answer="n", toml=support.TOML, files=support.FILES, **options):
        settings = config.load(support.make_project(self, toml=toml, files=files))
        self.output = io.StringIO()
        with contextlib.ExitStack() as stack:
            for module, name, replacement in (
                (board, "stop_app", fake.stop_app),
                (board, "read_status", fake.read_status),
                (board, "reset", fake.reset),
                (board, "wait_for_board", lambda target, attempts=20, arm=True: True),
                (board, "release", fake.release),
                (board, "restart_previous_app", fake.restart_previous_app),
                (deploy, "read_device_files", fake.read_device_files),
                (deploy, "remove_device_files", fake.remove_device_files),
                (deploy, "sync_files", fake.sync_files),
            ):
                stack.enter_context(mock.patch.object(module, name, replacement))
            self.asked = stack.enter_context(mock.patch("builtins.input", return_value=answer))
            stack.enter_context(contextlib.redirect_stdout(self.output))
            return deploy.deploy(settings, "COM8", False, **options)

    def test_new_board(self):
        fake = FakeBoard({"boot.py": entry("the one MicroPython creates")})
        package, slot = self.deploy(fake)
        self.assertEqual(slot, "a")
        self.assertEqual(package["version"], "1.2.0")
        self.assertEqual(fake.log, ["stop", "copy", "reset", ("release", True)])
        self.assertIn("a/main.py", fake.files)
        self.asked.assert_not_called()

    def test_second_deploy_goes_into_the_other_copy(self):
        fake = FakeBoard()
        self.deploy(fake)
        first = dict(fake.files)
        fake.state, fake.log = {"active": "a"}, []

        _, slot = self.deploy(fake)
        self.assertEqual(slot, "b")
        self.assertEqual(fake.log, ["stop", "copy", "reset", ("release", True)])
        for path in first:
            if path.startswith("a/"):
                self.assertEqual(fake.files[path], first[path], path)

    def test_copy_being_replaced_loses_its_main_first(self):
        """With modules of two versions in it, the copy must not look like one that can start."""
        fake = FakeBoard({
            "a/main.py": entry("running"),
            "b/main.py": entry("main of two versions ago"),
            "b/gone.mpy": entry("removed since"),
        }, state={"active": "a"})
        self.deploy(fake)
        self.assertEqual(fake.log, ["stop", ("remove", ["b/gone.mpy", "b/main.py"]), "copy", "reset", ("release", True)])

    def test_code_in_the_root_is_asked_about_and_a_no_uploads_nothing(self):
        fake = FakeBoard(self.HAND_DEPLOYED)
        with self.assertRaisesRegex(Error, "nothing uploaded"):
            self.deploy(fake, answer="n")
        self.assertEqual(fake.log, ["stop", "previous app restarted"])
        self.assertEqual(fake.files, self.HAND_DEPLOYED)
        listed = self.output.getvalue()
        self.assertIn("  main.py\n", listed)
        self.assertIn("  app.py\n", listed)
        self.assertNotIn("settings.json", listed)

    def test_code_in_the_root_goes_only_after_the_new_copy_is_whole(self):
        fake = FakeBoard(self.HAND_DEPLOYED)
        self.deploy(fake, answer="y")
        self.assertEqual(fake.log, ["stop", "copy", ("remove", ["app.py"]), "reset", ("release", True)])
        self.assertIn("settings.json", fake.files)

        again = FakeBoard(self.HAND_DEPLOYED)
        self.deploy(again, yes=True)
        self.asked.assert_not_called()
        self.assertEqual(again.log, fake.log)

    def test_package_that_does_not_build_leaves_the_board_running(self):
        files = {name: text for name, text in support.FILES.items() if name != "src/main.py"}
        fake = FakeBoard()
        with self.assertRaisesRegex(Error, "main.py"):
            self.deploy(fake, files=files)
        self.assertEqual(fake.log, [])

    def test_board_that_does_not_stop(self):
        fake = FakeBoard()
        fake.stops = False
        with self.assertRaisesRegex(Error, "nothing uploaded"):
            self.deploy(fake)
        self.assertEqual(fake.log, ["stop"])

    def test_board_with_another_micropython(self):
        toml = support.TOML.replace('version = "1.2.0"', 'version = "1.2.0"\nmicropython = "1.27.0"')
        fake = FakeBoard(micropython="1.24.1")
        with self.assertRaisesRegex(Error, "1.24.1.*1.27.0"):
            self.deploy(fake, toml=toml)
        self.assertEqual(fake.log, ["stop", "previous app restarted"])

        # Sources run on any MicroPython: without a version asked for, none is checked
        self.deploy(FakeBoard(micropython="1.24.1"))

    def test_not_enough_room(self):
        fake = FakeBoard(free=100)
        with self.assertRaisesRegex(Error, "not enough room"):
            self.deploy(fake)
        self.assertEqual(fake.log, ["stop", "previous app restarted"])

    def test_board_where_the_free_room_cannot_be_read(self):
        self.deploy(FakeBoard(free=None))

    def test_copy_that_fails_brings_back_the_app_of_before(self):
        fake = FakeBoard({"a/main.py": entry("running")}, state={"active": "a"})
        fake.arrives = False
        with self.assertRaises(Error):
            self.deploy(fake)
        self.assertEqual(fake.log, ["stop", "copy", "previous app restarted"])

    def test_old_code_that_cannot_be_removed_keeps_the_board_stopped(self):
        """The new copy is the active one by now: starting the old app is no longer an option."""
        fake = FakeBoard(self.HAND_DEPLOYED)
        fake.stubborn = ["app.py"]
        with self.assertRaisesRegex(Error, "app.py.*stays stopped"):
            self.deploy(fake, yes=True)
        self.assertEqual(fake.log, ["stop", "copy", ("remove", ["app.py"])])

    def test_files_the_tool_does_not_know_are_reported(self):
        fake = FakeBoard({"notes.txt": entry("notes")})
        self.deploy(fake)
        self.assertIn("left where it is: notes.txt", self.output.getvalue())
        self.assertIn("notes.txt", fake.files)

    def test_room_left_by_the_copy_being_replaced_counts(self):
        old = {"sha256": "old", "size": 500000}
        fake = FakeBoard({"a/main.py": entry("running"), "b/main.py": entry("old"), "b/big.mpy": old},
                         state={"active": "a"}, free=100)
        self.deploy(fake)
        self.assertNotIn("b/big.mpy", fake.files)

    def test_upload_ends_with_a_reset_before_the_board_is_released(self):
        """
        The watchdog of the upload only goes away with a reset. With /hold
        still there nothing starts, and the app is then started without it.
        """
        fake = FakeBoard()
        self.deploy(fake)
        self.assertEqual(fake.log[-2:], ["reset", ("release", True)])

        fake = FakeBoard()
        fake.resets = False
        with self.assertRaisesRegex(Error, "stays stopped"):
            self.deploy(fake)
        self.assertEqual(fake.log, ["stop", "copy", "reset"])
        self.assertIn("Unplug the USB cable", self.output.getvalue())


class RollbackTest(unittest.TestCase):
    BOTH = {"a": {"version": "2.1.0", "git": "abc1234"}, "b": {"version": "2.2.0", "git": "def5678"}}

    def status(self, state, slots=BOTH, **more):
        return dict({"state": state, "slots": slots, "hold": False}, **more)

    def test_goes_to_the_other_copy(self):
        self.assertEqual(deploy.rollback_slot(self.status({"active": "b"})), "a")
        self.assertEqual(deploy.rollback_slot(self.status({"active": "a"})), "b")

    def test_copy_on_trial_is_given_up_for_the_confirmed_one(self):
        state = {"active": "a", "trial": "b", "boots": 1, "version": "2.2.0"}
        self.assertEqual(deploy.rollback_slot(self.status(state)), "a")

    def test_nothing_to_go_back_to(self):
        """After the first deploy the other copy is empty."""
        with self.assertRaisesRegex(Error, "/b has no app"):
            deploy.rollback_slot(self.status({"active": "a"}, slots={"a": {}}))
        with self.assertRaisesRegex(Error, "no copy"):
            deploy.rollback_slot(self.status(None, slots={}))

    def test_interrupted_upload_is_not_rolled_back(self):
        with self.assertRaisesRegex(Error, "deploy"):
            deploy.rollback_slot(self.status({"active": "a"}, hold=True))

    def test_writes_the_state_and_says_what_will_start(self):
        fake = FakeBoard({"a/main.py": entry("old"), "b/main.py": entry("new")}, state={"active": "b"})
        with mock.patch.object(board, "read_status", fake.read_status), \
                mock.patch.object(board, "write_root_file", fake.write_root_file):
            version, slot = deploy.rollback("COM8")
        self.assertEqual((version, slot), ({"version": "0.9.0"}, "a"))
        self.assertEqual(fake.log, [("write", "ota.json", '{"active": "a"}')])


if __name__ == "__main__":
    unittest.main()
