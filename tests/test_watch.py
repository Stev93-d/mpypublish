"""The start of the app that was just uploaded, and the comparison with the package."""

import unittest
from unittest import mock

import support
from mpypublish import watch
from test_board import FakeClock

BOOT_LOG = """MPY: soft reboot
mpypublish: slot b, version 2.2.0 (ef22149)
Starting...
Connecting to: Home-WiFi
HTTP server started on port 80

Main loop started
"""

EXPECTED = {"slot": "b", "version": "2.2.0", "git": "ef22149"}

CRASH = (
    "MPY: soft reboot\r\nmpypublish: slot b, version 2.2.0 (ef22149)\r\n"
    "Traceback (most recent call last):\r\n"
    "  File \"main.py\", line 26, in <module>\r\n"
    "  File \"hardware.py\", line 55, in load_board\r\n"
    "BoardError: board.json missing\r\n"
    "MicroPython v1.27.0 on 2025-12-09; Generic ESP32S3 module with ESP32S3\r\n"
    "Type \"help()\" for more information.\r\n>>> "
)


class BootLogTest(unittest.TestCase):
    def test_normal_boot(self):
        self.assertEqual(watch.parse_boot_log(BOOT_LOG, "Main loop started"), {
            "slot": "b", "trial": False, "version": "2.2.0", "git": "ef22149", "ready": True, "error": None,
        })

    def test_boot_in_progress(self):
        log = watch.parse_boot_log(BOOT_LOG.split("HTTP server")[0], "Main loop started")
        self.assertEqual((log["slot"], log["version"], log["ready"]), ("b", "2.2.0", False))
        self.assertIsNone(watch.parse_boot_log("MPY: soft reboot\r\n")["slot"])

    def test_copy_on_trial(self):
        log = watch.parse_boot_log("mpypublish: slot a on trial, version 2.3.0 (abc1234-dirty)\n")
        self.assertEqual((log["slot"], log["trial"], log["git"]), ("a", True, "abc1234-dirty"))

    def test_app_built_outside_git_has_no_commit(self):
        log = watch.parse_boot_log("mpypublish: slot a, version dev (None)\n")
        self.assertEqual((log["version"], log["git"]), ("dev", None))

    def test_app_that_ends_at_the_repl_with_a_traceback(self):
        log = watch.parse_boot_log(CRASH, "Main loop started")
        self.assertEqual(log["error"], "BoardError: board.json missing")
        self.assertFalse(log["ready"])

    def test_exception_the_app_caught_and_printed_is_not_a_stop(self):
        """Many apps print the traceback of a failed connection and go on."""
        log = watch.parse_boot_log(
            "mpypublish: slot a, version 1.0.0 (None)\n"
            "Traceback (most recent call last):\n  File \"net.py\", line 12, in connect\n"
            "OSError: [Errno 113] EHOSTUNREACH\nretrying in 5 s\nMain loop started\n", "Main loop started")
        self.assertIsNone(log["error"])
        self.assertTrue(log["ready"])

    def test_traceback_that_cannot_be_read(self):
        log = watch.parse_boot_log("Traceback (most recent call last):\r\n  File \"main.py\", line 3\r\n>>> ")
        self.assertEqual(log["error"], "error at start")

    def test_differences_with_the_package(self):
        self.assertEqual(watch.check_running(EXPECTED, dict(EXPECTED, trial=False)), [])
        problems = watch.check_running(EXPECTED, dict(EXPECTED, git="8a9441d", slot="a"))
        self.assertEqual(len(problems), 2)
        self.assertIn("uploaded b, running a", problems[0])
        self.assertIn("8a9441d", problems[1])


class FakeConsole:
    """Fake serial port: every reading takes half a second and brings the next line of the log."""

    def __init__(self, clock, log, opens=True):
        self.clock = clock
        self.lines = log.splitlines()
        self.opens = opens
        self.text = ""
        self.sent = []
        self.closed = False

    def send(self, data):
        self.sent.append(data)
        return self.opens

    def poll(self):
        self.clock.now += 0.5
        if self.lines:
            self.text += self.lines.pop(0) + "\r\n"

    def close(self):
        self.closed = True


class StartTest(unittest.TestCase):
    def start(self, log, ready="Main loop started", opens=True):
        clock = FakeClock()
        self.console = FakeConsole(clock, log, opens)
        with mock.patch.object(watch, "time", clock):
            started = clock.now
            problems, self.log = watch.start_and_watch("COM8", EXPECTED, ready, console=self.console)
            self.elapsed = clock.now - started
        return problems

    def test_app_starts_from_the_repl_with_the_port_open(self):
        self.assertEqual(self.start(BOOT_LOG), [])
        self.assertEqual(self.console.sent, [watch.SOFT_REBOOT])
        self.assertTrue(self.console.closed)
        self.assertIn("Main loop started", self.log)

    def test_another_version_fails_at_once(self):
        problems = self.start(BOOT_LOG.replace("ef22149", "8a9441d"))
        self.assertEqual(len(problems), 1)
        self.assertIn("git", problems[0])
        self.assertLess(self.elapsed, 5)

    def test_copy_of_before_started_instead_of_the_new_one(self):
        problems = self.start(BOOT_LOG.replace("slot b, version 2.2.0 (ef22149)", "slot a, version 2.1.0 (abc1234)"))
        self.assertEqual(len(problems), 3)

    def test_app_that_stops_at_start(self):
        problems = self.start(CRASH)
        self.assertEqual(len(problems), 1)
        self.assertIn("BoardError", problems[0])
        self.assertIn("Traceback", self.log)                         # the caller shows it

    def test_app_that_never_says_it_is_ready(self):
        problems = self.start(BOOT_LOG.split("Main loop started")[0])
        self.assertEqual(len(problems), 1)
        self.assertIn("'Main loop started' within 60 seconds", problems[0])
        self.assertGreaterEqual(self.elapsed, 60)

    def test_without_a_ready_line_a_few_quiet_seconds_are_enough(self):
        self.assertEqual(self.start(BOOT_LOG, ready=None), [])
        self.assertGreaterEqual(self.elapsed, watch.SETTLE_S)
        self.assertLess(self.elapsed, 10)

    def test_without_a_ready_line_a_crash_is_still_seen(self):
        problems = self.start(CRASH, ready=None)
        self.assertIn("BoardError", problems[0])

    def test_board_that_starts_nothing(self):
        problems = self.start("MPY: soft reboot\nMicroPython v1.27.0\n>>> ")
        self.assertEqual(len(problems), 1)
        self.assertIn("boot.py did not say", problems[0])

    def test_port_that_does_not_open(self):
        problems = self.start(BOOT_LOG, opens=False)
        self.assertEqual(len(problems), 1)
        self.assertIn("COM8", problems[0])
        self.assertTrue(self.console.closed)


if __name__ == "__main__":
    unittest.main()
