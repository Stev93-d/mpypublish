"""Ports, and boards that disappear for a few seconds or stop answering. No board is needed."""

import contextlib
import io
import subprocess
import unittest
from unittest import mock

import support
from mpypublish import Error, board


class FakeClock:
    """In place of the time module: time passes only when the code sleeps."""

    def __init__(self):
        self.now = 1000.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakePort:
    def __init__(self, device, vid, description="USB Serial Device"):
        self.device, self.vid, self.description = device, vid, description


class PortTest(unittest.TestCase):
    def test_requested_port_wins(self):
        self.assertEqual(board.pick_port("COM3", [("COM8", "board")]), "COM3")
        self.assertEqual(board.pick_port("COM3", []), "COM3")

    def test_single_board_is_used(self):
        self.assertEqual(board.pick_port(None, [("COM8", "USB Serial Device")]), "COM8")

    def test_no_board(self):
        with self.assertRaisesRegex(Error, "no board"):
            board.pick_port(None, [])

    def test_two_boards_need_a_choice(self):
        """No guessing about which board gets the upload."""
        with self.assertRaisesRegex(Error, "COM5.*COM8"):
            board.pick_port(None, [("COM5", "thermostat"), ("COM8", "test board")])

    def test_only_ports_that_look_like_a_board(self):
        ports = [
            FakePort("COM8", 0x303A),               # ESP32-S3, native USB
            FakePort("COM4", 0x1A86),               # CH340 on a dev board
            FakePort("COM1", None, "Communications Port"),
            FakePort("COM7", 0x2C99, "3D printer"),
        ]
        with mock.patch("serial.tools.list_ports.comports", return_value=ports):
            self.assertEqual(board.board_ports(), [("COM8", "USB Serial Device"), ("COM4", "USB Serial Device")])

    def test_port_without_a_name_is_not_there_yet(self):
        with mock.patch("serial.tools.list_ports.comports", return_value=[FakePort("", 0x303A)]):
            self.assertEqual(board.board_ports(), [])

    def test_port_that_is_coming_back_is_awaited(self):
        """After a reset the USB port is missing for a few seconds: that is not 'no board'."""
        answers = [[], [], [("COM8", "USB Serial Device")], [("COM8", "USB Serial Device")]]
        with mock.patch.object(board, "time", FakeClock()), \
                mock.patch.object(board, "board_ports", side_effect=lambda: answers.pop(0)):
            self.assertEqual(board.find_port(None), "COM8")

    def test_no_board_at_all(self):
        with mock.patch.object(board, "time", FakeClock()), \
                mock.patch.object(board, "board_ports", return_value=[]):
            with self.assertRaises(Error):
                board.find_port(None)

    def test_requested_port_is_not_awaited(self):
        clock = FakeClock()
        with mock.patch.object(board, "time", clock), mock.patch.object(board, "board_ports", return_value=[]):
            self.assertEqual(board.find_port("COM3"), "COM3")
        self.assertEqual(clock.now, 1000.0)


class CommandTest(unittest.TestCase):
    def test_mpremote_that_never_answers_counts_as_failed(self):
        with mock.patch.object(board.subprocess, "run", side_effect=subprocess.TimeoutExpired("mpremote", 120)):
            result = board.run_mpremote("COM8", "exec", "pass")
        self.assertEqual(result.returncode, 1)
        self.assertIn("no answer", result.stderr)

    def test_mpremote_not_installed(self):
        with mock.patch.object(board.subprocess, "run", side_effect=FileNotFoundError("mpremote")):
            with self.assertRaisesRegex(Error, "pip install mpremote"):
                board.run_mpremote("COM8", "exec", "pass")

    def test_failed_step_says_why(self):
        failed = subprocess.CompletedProcess([], 1, "", "Traceback\nOSError: [Errno 28] ENOSPC\n")
        output = io.StringIO()
        with mock.patch.object(board, "run_mpremote", return_value=failed) as run, \
                mock.patch.object(board, "time", FakeClock()), contextlib.redirect_stdout(output):
            self.assertFalse(board.mpremote_step("COM8", "Copy", "cp", "x", ":x"))
        self.assertEqual(run.call_count, 3)
        self.assertIn("ENOSPC", output.getvalue())

    def test_watchdog_of_the_upload(self):
        self.assertIn("os.chdir('/')", board.ARM_WATCHDOG)           # boot.py may have left the board in /a
        self.assertIn("machine.WDT(timeout=60000)", board.ARM_WATCHDOG)
        # Where a minute is too long for the watchdog the upload must go on without
        compile(board.ARM_WATCHDOG + board.FEED_WATCHDOG, "board", "exec")
        self.assertIn("except Exception", board.ARM_WATCHDOG)

    def test_stopping_the_app_holds_the_selector(self):
        """Without /hold boot.py would start the app again, with its watchdog, halfway through the upload."""
        commands = []

        def step(target, description, *args, attempts=3):
            commands.append(args)
            return True

        with mock.patch.object(board, "mpremote_step", side_effect=step), \
                mock.patch.object(board, "wait_for_board", return_value=True), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(board.stop_app("COM8"))
            board.release("COM8", True)
            board.release("COM8", False)

        hold, reset, release, give_back = commands
        self.assertIn("os.chdir('/')", hold[1])
        self.assertIn("open('hold', 'w')", hold[1])
        self.assertIn("os.rename('main.py', 'main.py.off')", hold[1])     # board with its code still in the root
        self.assertEqual(reset, ("reset",))
        self.assertIn("os.remove('hold')", release[1])
        self.assertIn("os.remove('main.py.off')", release[1])
        self.assertIn("os.rename('main.py.off', 'main.py')", give_back[1])

    def test_board_that_does_not_stop(self):
        with mock.patch.object(board, "mpremote_step", return_value=False), \
                mock.patch.object(board, "wait_for_board", side_effect=AssertionError("no need to wait")), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(board.stop_app("COM8"))


class RecoveryTest(unittest.TestCase):
    def test_blind_reset_writes_and_closes(self):
        port = mock.MagicMock()
        with mock.patch("serial.Serial", return_value=port) as opened:
            board.blind_reset("COM8")
        self.assertEqual(opened.call_args[0][0], "COM8")
        written = port.__enter__.return_value.write.call_args[0][0]
        self.assertIn(b"machine.reset()", written)
        port.__exit__.assert_called_once()                           # it is at the close that the board runs it

    def test_blind_reset_on_a_missing_port_is_harmless(self):
        import serial
        with mock.patch("serial.Serial", side_effect=serial.SerialException("port missing")):
            board.blind_reset("COM8")

    def test_stuck_board_is_reset_then_awaited(self):
        output = io.StringIO()
        with mock.patch.object(board, "blind_reset") as reset, \
                mock.patch.object(board, "wait_for_board", return_value=True), \
                contextlib.redirect_stdout(output):
            self.assertTrue(board.recover_board("COM8"))
        reset.assert_called_once_with("COM8")
        self.assertNotIn("Unplug", output.getvalue())

    def test_board_that_cannot_be_recovered_needs_the_cable(self):
        output = io.StringIO()
        with mock.patch.object(board, "blind_reset"), \
                mock.patch.object(board, "wait_for_board", return_value=False), \
                contextlib.redirect_stdout(output):
            self.assertFalse(board.recover_board("COM8"))
        self.assertIn("Unplug the USB cable", output.getvalue())

    def test_failed_upload_gives_the_app_back(self):
        with mock.patch.object(board, "release", return_value=True) as release, \
                mock.patch.object(board, "reset", return_value=True), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(board.restart_previous_app("COM8"))
        release.assert_called_once_with("COM8", False)


class ReadTest(unittest.TestCase):
    def answer(self, stdout, returncode=0):
        return mock.patch.object(board, "run_mpremote",
                                 return_value=subprocess.CompletedProcess([], returncode, stdout, ""))

    def test_state_file(self):
        with self.answer('{"active": "b"}\r\n'):
            self.assertEqual(board.read_json_file("COM8", "ota.json"), {"active": "b"})
        for stdout, code in (("", 1), ("{broken", 0), ("[1, 2]", 0)):
            with self.answer(stdout, code):
                self.assertIsNone(board.read_json_file("COM8", "ota.json"), stdout)

    def test_micropython_version_and_free_room(self):
        with self.answer("1.27.0\r\n"):
            self.assertEqual(board.read_micropython_version("COM8"), "1.27.0")
        with self.answer("", 1):
            self.assertIsNone(board.read_micropython_version("COM8"))
            self.assertIsNone(board.read_free_bytes("COM8"))
        with self.answer("1310720\r\n"):
            self.assertEqual(board.read_free_bytes("COM8"), 1310720)


if __name__ == "__main__":
    unittest.main()
