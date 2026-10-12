"""
`mpypublish push` against the real code of the board: the package made by
the build goes through the small HTTP server and the updater, without a
network and without a board.
"""

import contextlib
import io
import json
import os
import unittest
import zipfile
from unittest import mock

import fakes
import support
from fakes import device, httpd, updater
from mpypublish import Error, build, push
from test_boot import boot

BASE = "http://board/ota"


class PushTest(fakes.DeviceTest):
    def setUp(self):
        super().setUp()
        build_dir, self.package = support.build_package(self)
        self.zip_path = build.write_package_zip(build_dir, self.package)
        self.text, self.files = push.read_package(self.zip_path)

        # The board: copy a is running
        self.folder = self.use_board()
        os.mkdir(os.path.join(self.folder, "a"))
        with open(os.path.join(self.folder, "a", "main.py"), "w") as f:
            f.write("")
        with open(os.path.join(self.folder, "a", "version.json"), "w") as f:
            json.dump({"name": "demo", "version": "1.1.0", "git": "0ld1234"}, f)
        device.write_state({"active": "a"})
        self.run_from("a")

        self.resets = 0
        self.start_updater()

        # The requests of push get to the fake server, and its time is the one of the test
        for name, replacement in (("request", self.request), ("time", self)):
            patcher = mock.patch.object(push, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def start_updater(self, pin=None):
        self.service = updater.Updater(pin=pin, micropython="1.27.0", reset=self.reset)
        self.server = httpd.Server(self.service)

    def reset(self):
        self.resets += 1

    def request(self, base, method, action="", body=None, timeout=8):
        self.assertEqual(base, BASE)
        if isinstance(body, (bytes, bytearray)):
            reply = fakes.http_upload(self.server, "/ota" + action, body)
        else:
            reply = fakes.http_request(self.server, method, "/ota" + action, body)
        self.sleep(0.05)
        return reply.status, reply.json

    # In place of the time module of push
    def time(self):
        return self.clock.ticks_ms() / 1000

    def sleep(self, seconds):
        for _ in range(int(seconds * 100)):
            self.clock.advance_ms(10)
            self.server.poll()

    def send(self, text=None, files=None, **options):
        with contextlib.redirect_stdout(io.StringIO()) as self.output:
            return push.send(BASE, text or self.text, files or self.files, **options)

    def on_board(self, path):
        with open(os.path.join(self.folder, "b", path), "rb") as f:
            return f.read()

    # --- package ---

    def test_package_is_read_from_the_zip(self):
        self.assertEqual(json.loads(self.text), self.package)
        self.assertEqual(set(self.files), set(self.package["files"]))

    def test_tampered_zip_is_refused(self):
        tampered = os.path.join(self.make_dir(), "package.zip")
        with zipfile.ZipFile(self.zip_path) as source, zipfile.ZipFile(tampered, "w") as target:
            for name in source.namelist():
                content = source.read(name)
                target.writestr(name, content + b"#" if name == "app.py" else content)
        with self.assertRaisesRegex(Error, "app.py"):
            push.read_package(tampered)

    def test_file_that_is_not_a_package(self):
        path = os.path.join(self.make_dir(), "other.zip")
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("readme.txt", "hello")
        with self.assertRaisesRegex(Error, "manifest.json"):
            push.read_package(path)
        with self.assertRaisesRegex(Error, "cannot read"):
            push.read_package(os.path.join(self.make_dir(), "missing.zip"))

    def test_address_of_the_updater(self):
        self.assertEqual(push.address("192.168.1.50"), "http://192.168.1.50/ota")
        self.assertEqual(push.address("192.168.1.50:8080", "/api/update"), "http://192.168.1.50:8080/api/update")
        self.assertEqual(push.address("http://thermostat.local/"), "http://thermostat.local/ota")

    # --- sending ---

    def test_whole_package_reaches_the_other_copy(self):
        self.assertTrue(self.send())
        self.sleep(3)
        self.assertEqual(self.resets, 1)

        for path, content in self.files.items():
            self.assertEqual(self.on_board(path), content, path)
        self.assertEqual(self.on_board("manifest.json").decode(), self.text)
        self.assertEqual(device.read_state(), {"active": "a", "trial": "b", "boots": 0, "version": "1.2.0"})
        self.assertEqual(boot.choose(self.folder + os.sep), ("b", True))

    def test_app_not_started_from_a_copy(self):
        self.run_from(None)
        with self.assertRaisesRegex(Error, "over USB once"):
            self.send()
        self.assertFalse(os.path.exists(os.path.join(self.folder, "b")))

    def test_board_with_a_pin(self):
        self.start_updater(pin="4321")
        with self.assertRaisesRegex(Error, "--pin"):
            self.send()
        with self.assertRaisesRegex(Error, "PIN not valid"):
            self.send(pin="0000")
        self.sleep(11)                                               # the wait after a wrong PIN
        self.assertTrue(self.send(pin="4321"))

    def test_pin_given_to_a_board_that_asks_for_none(self):
        self.assertTrue(self.send(pin="4321"))

    def test_package_of_another_app(self):
        other = dict(self.package, name="relay")
        with self.assertRaisesRegex(Error, "relay"):
            self.send(text=json.dumps(other))

    def test_stopping_halfway_leaves_the_update_open(self):
        self.assertFalse(self.send(stop_after=3))
        self.assertEqual(self.service.state, "receiving")
        self.assertEqual(self.service.status()["progress"]["files"], 3)
        self.sleep(61)
        self.assertEqual(self.service.state, "failed")
        self.assertEqual(device.read_state(), {"active": "a"})

    # --- failures made on purpose ---

    def test_corrupted_file_is_refused_by_the_board(self):
        text, files = push.sabotage(self.text, self.files, "corrupt")
        self.assertEqual(text, self.text)
        with self.assertRaisesRegex(Error, "not whole"):
            self.send(text, files)
        self.sleep(3)
        self.assertEqual(self.resets, 0)
        self.assertEqual(device.read_state(), {"active": "a"})

    def test_package_that_will_not_start_is_accepted(self):
        """The board cannot know before: boot.py finds out, at the next restart."""
        for how in ("crash", "hang"):
            text, files = push.sabotage(self.text, self.files, how)
            self.assertEqual(json.loads(text)["version"], "1.2.0-" + how)
            self.assertTrue(self.send(text, files))
            self.sleep(3)
            self.assertEqual(self.on_board("main.py"), push.SABOTAGE_MAIN[how])
            self.assertEqual(device.read_state()["version"], "1.2.0-" + how)

            # There was no restart: start over for the next failure
            device.write_state({"active": "a"})
            self.service.state = "idle"

    # --- after the restart ---

    OLD = {"name": "demo", "version": "1.1.0", "git": "0ld1234", "built": None}

    def watch(self, answers):
        """watch() with the answers the board gives one after the other; None = it does not answer."""
        answers = list(answers)

        def answer(base, method, action="", body=None, timeout=8):
            reply = answers.pop(0) if len(answers) > 1 else answers[0]
            return (200, reply) if reply is not None else (None, None)

        with mock.patch.object(push, "request", side_effect=answer), \
                contextlib.redirect_stdout(io.StringIO()) as self.output:
            return push.watch(BASE, self.package, self.OLD, timeout=120)

    def running(self, what, **more):
        return dict({"running": {key: what.get(key) for key in ("name", "version", "git", "built")},
                     "trial": False, "last": None}, **more)

    def test_new_version_has_to_answer_and_confirm(self):
        self.assertEqual(self.watch([
            None,                                                    # it is restarting
            self.running(self.package, trial=True),
            self.running(self.package, last={"result": "ok", "version": "1.2.0"}),
        ]), "ok")
        self.assertIn("version 1.2.0 (not in git) answers, on trial", self.output.getvalue())

    def test_version_of_before_is_back(self):
        self.assertEqual(self.watch([
            None,
            self.running(self.OLD, last={"result": "rolled_back", "version": "1.2.0"}),
        ]), "rolled_back")

    def test_rollback_of_another_update_does_not_count(self):
        answer = self.running(self.OLD, last={"result": "rolled_back", "version": "1.0.9"})
        self.assertEqual(self.watch([answer]), "timeout")

    def test_version_that_never_confirms_is_not_a_success(self):
        self.assertEqual(self.watch([self.running(self.package, trial=True)]), "unconfirmed")
        self.assertEqual(self.watch([None]), "timeout")

    # --- the whole command ---

    def push(self, **options):
        with contextlib.redirect_stdout(io.StringIO()) as self.output:
            return push.push("board", self.zip_path, **options)

    def test_nothing_answers(self):
        with mock.patch.object(push, "request", return_value=(None, None)):
            with self.assertRaisesRegex(Error, "no updater answers at http://board/ota"):
                self.push()
        # Something else answers on that port
        with mock.patch.object(push, "request", return_value=(200, {"status": "ok"})):
            with self.assertRaisesRegex(Error, "no updater"):
                self.push()

    def test_push_without_waiting(self):
        self.assertEqual(self.push(wait=False), 0)
        printed = self.output.getvalue()
        self.assertIn("demo: version 1.1.0 (0ld1234) in /a, MicroPython 1.27.0", printed)
        self.assertIn("demo-1.2.0.zip: version 1.2.0", printed)
        self.assertEqual(device.read_state()["trial"], "b")

    def test_outcomes_and_exit_codes(self):
        for result, code, words in (
            ("ok", 0, "Updated and confirmed: version 1.2.0"),
            ("rolled_back", 2, "version 1.1.0 (0ld1234) is back"),
            ("unconfirmed", 1, "mpypublish.confirm()"),
            ("timeout", 1, "Time is up"),
        ):
            device.write_state({"active": "a"})
            self.service.state = "idle"
            with mock.patch.object(push, "watch", return_value=result):
                self.assertEqual(self.push(), code, result)
            self.assertIn(words, self.output.getvalue())

    def test_sabotage_and_stop_reach_the_board(self):
        self.assertEqual(self.push(how="crash", wait=False), 0)
        self.assertEqual(self.on_board("main.py"), push.SABOTAGE_MAIN["crash"])
        self.assertIn("broken on purpose: crash", self.output.getvalue())

        device.write_state({"active": "a"})
        self.service.state = "idle"
        self.assertEqual(self.push(stop_after=2), 0)
        self.assertEqual(self.service.state, "receiving")


if __name__ == "__main__":
    unittest.main()
