"""The commands, with the board replaced by mocks."""

import contextlib
import io
import os
import unittest
from unittest import mock

import support
from mpypublish import board, cli, deploy, watch


class CliTest(unittest.TestCase):
    def run_cli(self, *argv, cwd=None):
        """Runs a command: (exit code, what it printed)."""
        output = io.StringIO()
        previous = os.getcwd()
        if cwd:
            os.chdir(cwd)
        try:
            with contextlib.redirect_stdout(output), \
                    mock.patch.object(board, "find_port", side_effect=lambda requested: requested or "COM8"):
                code = cli.main(list(argv))
        finally:
            os.chdir(previous)
        return code, output.getvalue()


class BuildCommandTest(CliTest):
    def test_build_from_a_folder_of_the_project(self):
        project = support.make_project(self).parent
        code, output = self.run_cli("build", cwd=project / "src")
        self.assertEqual(code, 0)
        self.assertIn("demo: ", output)
        self.assertTrue((project / "build" / "demo-1.2.0.zip").is_file())

    def test_errors_are_one_line_and_an_exit_code(self):
        project = support.make_project(self).parent
        (project / "src" / "main.py").unlink()
        code, output = self.run_cli("build", cwd=project)
        self.assertEqual(code, 1)
        self.assertRegex(output, r"^error: no main.py")

    def test_config_given_by_hand(self):
        path = support.make_project(self)
        code, _ = self.run_cli("-c", str(path), "build")
        self.assertEqual(code, 0)


class DeployCommandTest(CliTest):
    PACKAGE = {"version": "1.2.0", "git": None}

    def deploy(self, problems, *argv):
        project = support.make_project(self).parent
        with mock.patch.object(deploy, "deploy", return_value=(self.PACKAGE, "b")) as self.deployed, \
                mock.patch.object(watch, "start_and_watch", return_value=(problems, "boot log\nlast line\n")) as self.watched:
            return self.run_cli("deploy", *argv, cwd=project)

    def test_deploy_then_start_and_check(self):
        code, output = self.deploy([])
        self.assertEqual(code, 0)
        self.assertIn("Running: version 1.2.0 (not in git) in /b", output)
        self.assertNotIn("boot log", output)

        target, expected = self.watched.call_args[0]
        self.assertEqual((target, expected), ("COM8", {"slot": "b", "version": "1.2.0", "git": None}))
        self.assertEqual(self.watched.call_args[1], {"ready": "Main loop started", "timeout": 60})

    def test_options_reach_the_deploy(self):
        self.deploy([], "-p", "COM3", "--no-mpy", "--yes")
        _config, target, use_mpy, yes, _verbose = self.deployed.call_args[0]
        self.assertEqual((target, use_mpy, yes), ("COM3", False, True))

    def test_app_that_does_not_start_shows_its_log(self):
        code, output = self.deploy(["the app stopped at start: ImportError: no module named 'x'"])
        self.assertEqual(code, 1)
        self.assertIn("  | boot log\n  | last line\n", output)
        self.assertIn("error: the app stopped at start", output)
        self.assertNotIn("Running", output)


class OtherCommandsTest(CliTest):
    def test_rollback_starts_the_copy_of_before(self):
        version = {"version": "2.1.0", "git": "abc1234"}
        with mock.patch.object(deploy, "rollback", return_value=(version, "a")), \
                mock.patch.object(watch, "start_and_watch", return_value=([], "")) as watched:
            # Outside a project: there is no `ready` line to wait for
            code, output = self.run_cli("rollback", cwd=support.make_project(self).parent.parent)
        self.assertEqual(code, 0)
        self.assertIn("Running: version 2.1.0 (abc1234) in /a", output)
        self.assertEqual(watched.call_args[0][1], {"slot": "a", "version": "2.1.0", "git": "abc1234"})
        self.assertIsNone(watched.call_args[1]["ready"])

    def test_pin_is_written_and_the_app_restarted(self):
        with mock.patch.object(board, "write_root_file", return_value=True) as write, \
                mock.patch.object(board, "reset", return_value=True) as reset:
            code, output = self.run_cli("pin", "a1b2c3")
        self.assertEqual(code, 0)
        write.assert_called_once_with("COM8", "ota.pin", "a1b2c3")
        reset.assert_called_once()
        self.assertNotIn("a1b2c3", output)

    def test_pin_removed(self):
        with mock.patch.object(board, "write_root_file", return_value=True) as write, \
                mock.patch.object(board, "reset", return_value=True):
            code, output = self.run_cli("pin", "--remove")
        write.assert_called_once_with("COM8", "ota.pin", None)
        self.assertIn("off", output)

    def test_pin_has_to_be_typeable(self):
        with mock.patch.object(board, "write_root_file", side_effect=AssertionError("must not get to the board")):
            for argv in (["123"], ["12 34"], ["with-dash"], ["x" * 33], [], ["1234", "--remove"]):
                code, output = self.run_cli("pin", *argv)
                self.assertEqual(code, 1, argv)
                self.assertIn("error: ", output)

    STATUS = {
        "micropython": "1.27.0",
        "state": {"active": "b", "last": {"result": "ok", "version": "2.2.0"}},
        "slots": {"a": {"version": "2.1.0", "git": "abc1234", "built": "2026-10-10T15:00:00Z"},
                  "b": {"version": "2.2.0", "git": None, "built": "2026-10-12T09:30:00Z"}},
        "pin": True, "hold": False, "free": 1310720,
    }

    def test_what_ls_says_about_a_board(self):
        self.assertEqual(cli.describe(self.STATUS, "COM8"), [
            "Board on COM8: MicroPython 1.27.0, 1280 KB free",
            "  /a  2.1.0 (abc1234), built 2026-10-10T15:00:00Z",
            "  /b  2.2.0 (not in git), built 2026-10-12T09:30:00Z  <- active",
            "  last update over the network: ok (2.2.0)",
            "  updates over the network: on, a PIN is set",
        ])

    def test_copy_on_trial_and_interrupted_upload(self):
        status = dict(self.STATUS, state={"active": "a", "trial": "b", "boots": 0}, hold=True, pin=False)
        lines = cli.describe(status, "COM8")
        self.assertIn("<- active", lines[1])
        self.assertIn("<- on trial", lines[2])
        self.assertIn("off, no PIN", lines[3])
        self.assertIn("interrupted", lines[4])

    def test_board_never_deployed(self):
        lines = cli.describe({"micropython": "1.27.0", "state": None, "slots": {}, "pin": False, "free": None}, "COM8")
        self.assertEqual(lines[0], "Board on COM8: MicroPython 1.27.0")
        self.assertIn("not deployed with mpypublish", lines[1])

    def test_ls_restarts_the_app_it_interrupted(self):
        status = dict(self.STATUS, tree=["a/", "  main.py 812", "boot.py 3120"])
        with mock.patch.object(board, "read_status", return_value=status), \
                mock.patch.object(board, "reset", return_value=True) as reset:
            code, output = self.run_cli("ls", "-p", "COM3")
        self.assertEqual(code, 0)
        self.assertIn("Board on COM3", output)
        self.assertIn("    main.py 812\n", output)
        reset.assert_called_once_with("COM3")

        # With an upload interrupted nothing must start: the copy may be half written
        with mock.patch.object(board, "read_status", return_value=dict(status, hold=True)), \
                mock.patch.object(board, "reset", side_effect=AssertionError("must stay stopped")):
            self.run_cli("ls")


if __name__ == "__main__":
    unittest.main()
