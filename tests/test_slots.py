"""
The state of the two copies as the app sees it (device/mpypublish), together
with device/boot.py which reads it at start: the two have to agree on the
same file.
"""

import json
import os
import unittest

import fakes
from fakes import device
from test_boot import boot


class SlotStateTest(fakes.DeviceTest):
    def setUp(self):
        super().setUp()
        self.folder = self.use_board()
        self.run_from(None)

    def copy(self, slot, version=None):
        os.makedirs(os.path.join(self.folder, slot))
        with open(os.path.join(self.folder, slot, "main.py"), "w") as f:
            f.write("")
        if version:
            with open(os.path.join(self.folder, slot, "version.json"), "w") as f:
                json.dump(version, f)

    def choose(self):
        """What boot.py would do at the next restart."""
        return boot.choose(self.folder + os.sep)

    def saved(self):
        with open(os.path.join(self.folder, "ota.json")) as f:
            return json.load(f)

    # --- state file ---

    def test_missing_or_broken_state_is_empty(self):
        self.assertEqual(device.read_state(), {})
        for content in ("{broken", "[1, 2]", ""):
            with open(os.path.join(self.folder, "ota.json"), "w") as f:
                f.write(content)
            self.assertEqual(device.read_state(), {}, content)

    def test_state_is_written_through_a_temporary_file(self):
        self.assertTrue(device.write_state({"active": "a"}))
        self.assertTrue(device.write_state({"active": "b"}))
        self.assertEqual(self.saved(), {"active": "b"})
        self.assertEqual(os.listdir(self.folder), ["ota.json"])

    def test_failed_write_keeps_the_state_of_before(self):
        device.write_state({"active": "a"})
        self.assertFalse(device.write_state({"active": {1, 2}}))     # cannot be written as JSON
        self.assertEqual(self.saved(), {"active": "a"})
        self.assertEqual(os.listdir(self.folder), ["ota.json"])

    def test_copy_that_is_running(self):
        self.assertIsNone(device.current_slot())                     # on a PC, or with the code in the root
        self.assertEqual(device.app_dir(), "")
        self.run_from("b")
        self.assertEqual((device.app_dir(), device.current_slot()), (self.folder + "/b", "b"))
        self.assertEqual((device.other_slot("a"), device.other_slot("b")), ("b", "a"))

    def test_version_of_the_copy_that_is_running(self):
        self.copy("a", {"name": "demo", "version": "2.1.0", "git": "abc1234", "built": "2026-10-10T15:00:00Z"})
        self.copy("b", {"name": "demo", "version": "2.2.0", "git": None})
        self.run_from("b")
        self.assertEqual(device.version(), {"name": "demo", "version": "2.2.0", "git": None, "built": None})

    def test_files_copied_by_hand_have_no_version(self):
        self.copy("a")
        self.run_from("a")
        self.assertEqual(device.version(), {"name": None, "version": "dev", "git": None, "built": None})

    # --- trial, confirmation, way back ---

    def test_new_copy_goes_on_trial_and_is_confirmed(self):
        self.copy("a")
        self.copy("b", {"name": "demo", "version": "2.2.0"})
        device.write_state({"active": "a"})
        self.run_from("a")

        # The update is over: copy b is written and checked
        self.assertTrue(device.start_trial("b", "2.2.0"))
        self.assertFalse(device.running_trial())                     # it is still a that runs
        self.assertEqual(self.choose(), ("b", True))

        # Restart: b runs, on trial, until it confirms
        self.run_from("b")
        self.assertTrue(device.running_trial())
        self.assertTrue(device.confirm())
        self.assertFalse(device.running_trial())
        self.assertEqual(self.saved(), {"active": "b", "last": {"result": "ok", "version": "2.2.0"}})
        self.assertEqual(self.choose(), ("b", False))

    def test_copy_that_does_not_confirm_is_left_at_the_next_boot(self):
        self.copy("a")
        self.copy("b")
        device.write_state({"active": "a"})
        self.run_from("a")
        device.start_trial("b", "2.2.0")

        self.assertEqual(self.choose(), ("b", True))
        self.assertEqual(self.choose(), ("a", False))                # restarted without a confirmation
        self.run_from("a")
        self.assertFalse(device.running_trial())
        self.assertEqual(device.read_state()["last"], {"result": "rolled_back", "version": "2.2.0"})

    def test_confirm_can_stay_in_the_main_loop(self):
        """Nothing to confirm: no write to the flash, and the state is read once."""
        self.copy("a")
        device.write_state({"active": "a", "last": {"result": "ok", "version": "2.1.0"}})
        self.run_from("a")
        before = os.stat(os.path.join(self.folder, "ota.json")).st_mtime_ns

        for _ in range(3):
            self.assertTrue(device.confirm())
        os.remove(os.path.join(self.folder, "ota.json"))
        self.assertTrue(device.confirm())                            # not read again either
        self.assertEqual(os.listdir(self.folder), ["a"])
        self.assertGreater(before, 0)

    def test_confirmation_that_cannot_be_written_is_tried_again(self):
        self.copy("a")
        self.copy("b")
        device.write_state({"active": "a", "trial": "b", "boots": 1})
        self.run_from("b")

        original = device.write_state
        device.write_state = lambda data: False
        self.addCleanup(setattr, device, "write_state", original)
        self.assertFalse(device.confirm())
        self.assertTrue(device.running_trial())

        device.write_state = original
        self.assertTrue(device.confirm())
        self.assertEqual(self.saved()["active"], "b")

    def test_app_not_started_by_boot_is_never_on_trial(self):
        device.write_state({"active": "a", "trial": "b", "boots": 1})
        self.assertFalse(device.running_trial())
        self.assertTrue(device.confirm())
        self.assertEqual(self.saved(), {"active": "a", "trial": "b", "boots": 1})


if __name__ == "__main__":
    unittest.main()
