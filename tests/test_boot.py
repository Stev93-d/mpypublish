"""
device/boot.py, which chooses the copy of the app to start.

A temporary folder stands for the root of the board. boot.py does nothing
when it is imported: it only acts as a program, on MicroPython.
"""

import importlib.util
import json
import os
import sys
import unittest

import fakes
import support

_spec = importlib.util.spec_from_file_location(
    "boot_selector", support.ROOT / "src" / "mpypublish" / "device" / "boot.py")
boot = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(boot)


class BootTest(fakes.DeviceTest):
    def setUp(self):
        super().setUp()
        self.folder = self.make_dir()
        self.root = self.folder + os.sep
        previous_dir, previous_path = os.getcwd(), list(sys.path)
        self.addCleanup(os.chdir, previous_dir)
        self.addCleanup(sys.path.__setitem__, slice(None), previous_path)

    def copy(self, slot, complete=True, version=None):
        """A copy of the app in its folder; not complete = without main.py."""
        os.makedirs(os.path.join(self.folder, slot, "lib"))
        if complete:
            with open(os.path.join(self.folder, slot, "main.py"), "w") as f:
                f.write("")
        if version:
            with open(os.path.join(self.folder, slot, "version.json"), "w") as f:
                json.dump(version, f)

    def write_state(self, data):
        with open(os.path.join(self.folder, "ota.json"), "w") as f:
            f.write(data if isinstance(data, str) else json.dumps(data))

    def state(self):
        with open(os.path.join(self.folder, "ota.json")) as f:
            return json.load(f)

    # --- which copy ---

    def test_active_copy_is_started(self):
        self.copy("a")
        self.copy("b")
        self.write_state({"active": "b"})
        self.assertEqual(boot.choose(self.root), ("b", False))
        self.assertEqual(self.state(), {"active": "b"})

    def test_without_a_state_file_the_first_copy_is_started(self):
        self.copy("a")
        self.assertEqual(boot.choose(self.root), ("a", False))

    def test_broken_state_file_still_starts_a_copy(self):
        self.copy("b")
        for content in ("{broken", "[]", "", '{"active": "c"}'):
            self.write_state(content)
            self.assertEqual(boot.choose(self.root), ("b", False), content)

    def test_active_copy_without_main_falls_back_to_the_other(self):
        self.copy("a", complete=False)
        self.copy("b")
        self.write_state({"active": "a"})
        self.assertEqual(boot.choose(self.root), ("b", False))

    def test_no_copy_at_all(self):
        """New board, or one that still has its code in the root: what is there runs."""
        self.assertEqual(boot.choose(self.root), (None, False))
        self.assertIsNone(boot.boot(self.root))
        self.assertEqual(os.listdir(self.folder), [])

    # --- copy on trial ---

    def test_trial_copy_is_started_once(self):
        self.copy("a")
        self.copy("b")
        self.write_state({"active": "a", "trial": "b", "boots": 0, "version": "2.2.0"})

        self.assertEqual(boot.choose(self.root), ("b", True))
        self.assertEqual(self.state(), {"active": "a", "trial": "b", "boots": 1, "version": "2.2.0"})

    def test_trial_that_was_not_confirmed_goes_back(self):
        self.copy("a")
        self.copy("b")
        self.write_state({"active": "a", "trial": "b", "boots": 1, "version": "2.2.0"})

        self.assertEqual(boot.choose(self.root), ("a", False))
        self.assertEqual(self.state(), {"active": "a", "last": {"result": "rolled_back", "version": "2.2.0"}})
        # At the following restarts the one of before stays
        self.assertEqual(boot.choose(self.root), ("a", False))

    def test_trial_copy_that_cannot_start_goes_back_at_once(self):
        self.copy("a")
        self.copy("b", complete=False)
        self.write_state({"active": "a", "trial": "b", "boots": 0, "version": "2.2.0"})
        self.assertEqual(boot.choose(self.root), ("a", False))
        self.assertEqual(self.state()["last"]["result"], "rolled_back")

    def test_trial_is_not_started_if_the_attempt_cannot_be_recorded(self):
        """Without the mark of the trial boot a broken copy would start again at every restart."""
        self.copy("a")
        self.copy("b")
        self.write_state({"active": "a", "trial": "b", "boots": 0})
        original = boot._write
        boot._write = lambda path, data: False
        self.addCleanup(setattr, boot, "_write", original)
        self.assertEqual(boot.choose(self.root), ("a", False))

    def test_confirmed_copy_is_the_active_one(self):
        self.copy("a")
        self.copy("b")
        self.write_state({"active": "b", "last": {"result": "ok", "version": "2.2.0"}})
        self.assertEqual(boot.choose(self.root), ("b", False))

    # --- start ---

    def test_boot_enters_the_copy_and_puts_it_first_in_the_path(self):
        self.copy("a")
        self.assertEqual(boot.boot(self.root), "a")
        self.assertEqual(os.path.realpath(os.getcwd()), os.path.join(self.folder, "a"))
        self.assertEqual(sys.path[:2], [self.root + "a", self.root + "a/lib"])
        self.assertEqual(fakes.FakeWDT.instances, [])               # the watchdog is the app's business

    def test_boot_says_what_it_starts(self):
        """It is the line `mpypublish deploy` reads to know that the new version is the one running."""
        self.copy("a", version={"version": "2.1.0", "git": "abc1234"})
        self.copy("b", version={"version": "2.2.0", "git": None})
        self.write_state({"active": "a", "trial": "b", "boots": 0})

        boot.boot(self.root)
        self.assertEqual(self.stdout.getvalue(), "mpypublish: slot b on trial, version 2.2.0 (None)\n")

        self.stdout.truncate(0)
        self.stdout.seek(0)
        boot.boot(self.root)
        self.assertEqual(self.stdout.getvalue(), "mpypublish: slot a, version 2.1.0 (abc1234)\n")

    def test_trial_boot_arms_a_watchdog(self):
        """An error before main.py arms its own would leave the board at the REPL, with no way back."""
        self.copy("a")
        self.copy("b")
        self.write_state({"active": "a", "trial": "b", "boots": 0})
        self.assertEqual(boot.boot(self.root), "b")
        self.assertEqual([wdt.timeout for wdt in fakes.FakeWDT.instances], [60000])

    def test_trial_boot_where_the_watchdog_cannot_wait_a_minute(self):
        fakes.FakeWDT.refuse = True
        self.copy("a")
        self.copy("b")
        self.write_state({"active": "a", "trial": "b", "boots": 0})
        self.assertEqual(boot.boot(self.root), "b")

    def test_hold_file_starts_nothing(self):
        """During an upload over USB the board has to stay at the REPL, without a watchdog."""
        self.copy("a")
        self.copy("b")
        self.write_state({"active": "a", "trial": "b", "boots": 0})
        with open(os.path.join(self.folder, "hold"), "w"):
            pass
        before = os.getcwd()

        self.assertIsNone(boot.boot(self.root))
        self.assertEqual(os.getcwd(), before)
        self.assertEqual(self.state()["boots"], 0)                  # the trial was not used up
        self.assertEqual(fakes.FakeWDT.instances, [])


if __name__ == "__main__":
    unittest.main()
