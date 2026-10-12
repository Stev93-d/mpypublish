"""
The update from the network (device/mpypublish/updater.py), tried through
the small HTTP server, the way `mpypublish push` uses it.

A temporary folder stands for the root of the board: copy "a" is running,
and the package arrives in "b".
"""

import hashlib
import hmac
import json
import os
import unittest

import fakes
from fakes import device, httpd, updater
from test_boot import boot

PIN = "4321"

# A small package, with a file in more than one piece and bytes that are not text
FILES = {
    "main.py": b"print('new version')\n",
    "app.mpy": b"M\x06" + bytes(range(256)) * 80,                   # 20 KB: three pieces
    "lib/sensor.mpy": b"M\x06\xff\xfe\x00sensor",
    "mpypublish/updater.mpy": b"M\x06updater",
    "version.json": b'{"name": "demo", "version": "2.2.0"}',
}


def manifest(files=FILES, **changes):
    """The manifest of a package, as `mpypublish build` writes it, as text."""
    data = {
        "name": "demo",
        "version": "2.2.0",
        "git": "abc1234",
        "built": "2026-10-10T15:00:00Z",
        "micropython": "1.27.0",
        "mpy": True,
        "files": {
            path: {"sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}
            for path, content in files.items()
        },
    }
    data.update(changes)
    return json.dumps(data)


class UpdateTest(fakes.DeviceTest):
    def setUp(self):
        super().setUp()
        self.folder = self.use_board()

        # Copy a is running; in b there still is the version of before
        self.write("a/main.py", b"print('running version')\n")
        self.write("a/version.json", b'{"name": "demo", "version": "2.1.0", "git": "0ld1234"}')
        self.write("b/main.py", b"print('version of before')\n")
        self.write("b/lib/old.py", b"leftover")
        device.write_state({"active": "a"})
        self.run_from("a")

        self.resets = 0
        self.service = self.make_service()
        self.server = httpd.Server(self.service)

    def make_service(self, pin=None):
        return updater.Updater(pin=pin, micropython="1.27.0", reset=self.reset)

    def use_pin(self, pin=PIN):
        self.service = self.make_service(pin)
        self.server = httpd.Server(self.service)

    def reset(self):
        self.resets += 1

    def path(self, name):
        return os.path.join(self.folder, name)

    def write(self, name, content):
        os.makedirs(os.path.dirname(self.path(name)), exist_ok=True)
        with open(self.path(name), "wb") as f:
            f.write(content)

    def read(self, name):
        with open(self.path(name), "rb") as f:
            return f.read()

    def tree(self, slot):
        found = []
        for base, _dirs, names in os.walk(self.path(slot)):
            found += [os.path.relpath(os.path.join(base, name), self.folder).replace(os.sep, "/") for name in names]
        return sorted(found)

    # --- as `mpypublish push` does ---

    def status(self):
        return fakes.http_request(self.server, "GET", "/ota").json

    def begin(self, text=None, pin=None):
        text = manifest() if text is None else text
        body = {"manifest": text}
        if pin is not None:
            nonce = self.status()["nonce"]
            body["auth"] = hmac.new(pin.encode(), (nonce + "\n" + text).encode(), hashlib.sha256).hexdigest()
        return fakes.http_request(self.server, "POST", "/ota/begin", body)

    def poll(self, ms=200):
        # Long waits in bigger steps: they are about deadlines, not about the work
        step = 2 if ms <= 2000 else 50
        for _ in range(ms // step):
            self.clock.advance_ms(step)
            self.server.poll()

    def send(self, index, offset, data):
        return fakes.http_upload(self.server, "/ota/file?index=%d&offset=%d" % (index, offset), data)

    def upload(self, files=FILES, skip=()):
        for index, path in enumerate(sorted(files)):
            if path in skip:
                continue
            content = files[path]
            for offset in range(0, max(len(content), 1), 8192):
                reply = self.send(index, offset, content[offset:offset + 8192])
                self.assertEqual(reply.status, 200, reply.json)

    def commit(self):
        return fakes.http_request(self.server, "POST", "/ota/commit")

    def started(self, text=None):
        """Update open and copy emptied: ready to receive."""
        self.assertEqual(self.begin(text).status, 200)
        self.poll()
        self.assertEqual(self.service.state, "receiving")

    # --- the good path ---

    def test_whole_update(self):
        text = manifest()
        reply = self.begin(text)
        self.assertEqual(reply.status, 200)
        self.assertEqual(reply.json["state"], "preparing")
        self.assertTrue(self.service.busy)

        self.poll()
        status = self.status()
        self.assertEqual(status["state"], "receiving")
        self.assertEqual(status["progress"], {
            "files": 0, "total_files": 5, "bytes": 0,
            "total_bytes": sum(len(content) for content in FILES.values()),
        })
        self.assertEqual(self.tree("b"), [])                         # the version of before is gone

        self.upload()
        self.assertEqual(self.status()["progress"]["files"], 5)

        self.assertEqual(self.commit().json["state"], "verifying")
        self.assertEqual(self.resets, 0)
        self.poll(100)
        self.assertEqual(self.status()["state"], "restarting")
        self.assertEqual(self.resets, 0)                             # first the answer gets to whoever updates
        self.poll(2000)
        self.assertEqual(self.resets, 1)

        # On the board: the whole package in copy b, and the manifest as it was sent
        self.assertEqual(self.tree("b"), sorted("b/" + path for path in list(FILES) + ["manifest.json"]))
        for path, content in FILES.items():
            self.assertEqual(self.read("b/" + path), content, path)
        self.assertEqual(self.read("b/manifest.json").decode(), text)

        # The copy that is running was not touched, and at the restart the new one starts, on trial
        self.assertEqual(self.read("a/main.py"), b"print('running version')\n")
        self.assertEqual(device.read_state(), {"active": "a", "trial": "b", "boots": 0, "version": "2.2.0"})
        self.assertEqual(boot.choose(self.folder + os.sep), ("b", True))

    def test_status_when_idle(self):
        device.write_state({"active": "a", "last": {"result": "rolled_back", "version": "2.1.5"}})
        status = self.status()
        self.assertEqual(status["state"], "idle")
        self.assertTrue(status["enabled"])
        self.assertFalse(self.service.busy)
        self.assertEqual((status["slot"], status["trial"], status["error"]), ("a", False, None))
        self.assertEqual(status["last"], {"result": "rolled_back", "version": "2.1.5"})
        # What `push` compares with the package it sent
        self.assertEqual(status["running"], {"name": "demo", "version": "2.1.0", "git": "0ld1234", "built": None})
        self.assertEqual(status["micropython"], "1.27.0")

    def test_binary_content_arrives_as_it_is(self):
        self.started()
        content = FILES["lib/sensor.mpy"]
        index = sorted(FILES).index("lib/sensor.mpy")
        self.assertEqual(self.send(index, 0, content).json, {"received": len(content), "files": 1, "total_files": 5})
        self.assertEqual(self.read("b/lib/sensor.mpy"), content)

    def test_app_with_its_own_web_server_calls_handle(self):
        self.assertIsNone(self.service.handle("GET", "/api/status"))
        self.assertIsNone(self.service.handle("GET", "/otaku"))
        status, reply = self.service.handle("GET", "/ota")
        self.assertEqual((status, reply["state"]), (200, "idle"))
        status, reply = self.service.handle("POST", "/ota/begin", {}, json.dumps({"manifest": manifest()}).encode())
        self.assertEqual((status, reply["state"]), (200, "preparing"))
        self.assertEqual(self.service.handle("DELETE", "/ota/begin")[0], 404)
        self.assertEqual(self.service.handle("POST", "/ota/nothing")[0], 404)

    def test_another_prefix(self):
        service = updater.Updater(prefix="/api/update", micropython="1.27.0", reset=self.reset)
        self.assertEqual(service.handle("GET", "/api/update")[0], 200)
        self.assertIsNone(service.handle("GET", "/ota"))

    # --- who can update ---

    def test_without_a_pin_the_manifest_is_enough(self):
        status = self.status()
        self.assertFalse(status["pin"])
        self.assertNotIn("nonce", status)
        self.assertEqual(self.begin().status, 200)

    def test_with_a_pin_the_manifest_has_to_be_signed(self):
        self.use_pin()
        status = self.status()
        self.assertTrue(status["pin"])
        self.assertRegex(status["nonce"], r"^[0-9a-f]{16}$")

        reply = self.begin()                                         # no signature at all
        self.assertEqual(reply.status, 403)
        self.assertEqual(self.service.state, "idle")
        self.assertEqual(self.tree("b"), ["b/lib/old.py", "b/main.py"])

    def test_right_pin(self):
        self.use_pin()
        self.assertEqual(self.begin(pin=PIN).status, 200)
        self.assertEqual(self.service.state, "preparing")

    def test_wrong_pin_makes_the_next_attempt_wait(self):
        self.use_pin()
        self.assertEqual(self.begin(pin="0000").status, 403)
        reply = self.begin(pin=PIN)                                  # right, but too soon
        self.assertEqual(reply.status, 403)
        self.assertIn("few seconds", reply.json["error"])

        self.poll(10000)
        self.assertEqual(self.begin(pin=PIN).status, 200)

    def test_signed_request_cannot_be_sent_again(self):
        """Whoever listened to an update on the network cannot repeat it later."""
        self.use_pin()
        text = manifest()
        nonce = self.status()["nonce"]
        body = {"manifest": text,
                "auth": hmac.new(PIN.encode(), (nonce + "\n" + text).encode(), hashlib.sha256).hexdigest()}
        self.assertEqual(fakes.http_request(self.server, "POST", "/ota/begin", body).status, 200)
        fakes.http_request(self.server, "POST", "/ota/abort")

        self.assertNotEqual(self.status()["nonce"], nonce)
        self.assertEqual(fakes.http_request(self.server, "POST", "/ota/begin", body).status, 403)

    def test_signature_is_the_one_of_the_standard_library(self):
        for key, message in ((b"4321", b"nonce\nmanifest"), (b"k" * 100, b""), (b"", b"x" * 300)):
            self.assertEqual(updater.hmac_sha256(key, message), hmac.new(key, message, hashlib.sha256).hexdigest())

    def test_pin_written_by_the_tool_is_found(self):
        self.write("ota.pin", b"a1b2c3\n")
        service = updater.Updater(micropython="1.27.0", reset=self.reset)
        self.assertTrue(service.status()["pin"])
        self.assertEqual(service._pin, "a1b2c3")

        self.write("ota.pin", b"")
        self.assertFalse(updater.Updater(micropython="1.27.0", reset=self.reset).status()["pin"])

    def test_malformed_begin(self):
        for body in ({}, {"auth": "x"}, {"manifest": {"name": "demo"}}, [1]):
            reply = fakes.http_request(self.server, "POST", "/ota/begin", body)
            self.assertEqual(reply.status, 400, body)
        reply = fakes.http_upload(self.server, "/ota/begin", b"\xff\xfe not text")
        self.assertEqual(reply.status, 400)

    # --- which package ---

    def test_package_for_something_else_is_refused(self):
        for changes, word in (
            ({"name": "relay"}, "relay"),
            ({"micropython": "1.24.1"}, "1.24.1"),
            ({"micropython": None}, "None"),
        ):
            reply = self.begin(manifest(**changes))
            self.assertEqual(reply.status, 400, changes)
            self.assertIn(word, reply.json["error"])
            self.assertEqual(self.service.state, "idle")
        self.assertEqual(self.tree("b"), ["b/lib/old.py", "b/main.py"])    # nothing was touched

    def test_which_micropython_matters_only_for_compiled_packages(self):
        self.assertEqual(self.begin(manifest(micropython="1.27.3")).status, 200)   # a patch release reads the same .mpy
        fakes.http_request(self.server, "POST", "/ota/abort")
        self.assertEqual(self.begin(manifest(micropython=None, mpy=False)).status, 200)

    def test_paths_outside_the_copy_are_refused(self):
        for path in ("../boot.py", "/boot.py", "lib//sensor.mpy", "lib/../../settings.json", ".hidden",
                     "lib/", "manifest.json", "name with spaces.py", "C:\\boot.py"):
            reply = self.begin(manifest(dict(FILES, **{path: b"x"})))
            self.assertEqual(reply.status, 400, path)
            self.assertIn("path not allowed", reply.json["error"])

    def test_package_must_be_startable_and_well_formed(self):
        without_main = {path: content for path, content in FILES.items() if path != "main.py"}
        self.assertIn("main.py", self.begin(manifest(without_main)).json["error"])

        for files in ({}, "list", {"main.py": {"size": -1, "sha256": "0" * 64}},
                      {"main.py": {"size": 10, "sha256": "short"}}, {"main.py": {"size": 10 ** 7, "sha256": "0" * 64}}):
            text = json.dumps(dict(json.loads(manifest()), files=files))
            self.assertEqual(self.begin(text).status, 400, files)
        self.assertEqual(self.begin("{not json").status, 400)

    def test_not_enough_room(self):
        original = updater._free_bytes
        updater._free_bytes = lambda: 50 * 1024
        self.addCleanup(setattr, updater, "_free_bytes", original)

        self.assertEqual(self.begin().status, 200)
        self.poll()
        status = self.status()
        self.assertEqual(status["state"], "failed")
        self.assertIn("not enough room", status["error"])
        self.assertFalse(self.service.busy)

    # --- when ---

    def test_app_in_the_root_has_to_be_deployed_over_usb_first(self):
        self.run_from(None)
        self.assertFalse(self.status()["enabled"])
        reply = self.begin()
        self.assertEqual(reply.status, 409)
        self.assertIn("USB", reply.json["error"])

    def test_copy_still_on_trial_cannot_be_replaced(self):
        """It would be written over the version to go back to."""
        device.write_state({"active": "b", "trial": "a", "boots": 1, "version": "2.2.0"})
        self.assertTrue(self.status()["trial"])
        self.assertEqual(self.begin().status, 409)
        self.assertEqual(self.read("b/main.py"), b"print('version of before')\n")

    def test_new_begin_restarts_an_interrupted_update(self):
        self.started()
        self.upload(skip=("version.json",))
        self.started()                                               # whoever was sending started over
        self.assertEqual(self.status()["progress"]["bytes"], 0)
        self.assertEqual(self.tree("b"), [])

    def test_nothing_else_while_verifying(self):
        self.started()
        self.upload()
        self.commit()
        self.assertEqual(self.begin().status, 409)
        self.assertEqual(self.send(0, 0, b"x").status, 409)
        self.assertEqual(fakes.http_request(self.server, "POST", "/ota/abort").status, 409)

    # --- the pieces ---

    def test_file_requests_need_an_open_update(self):
        self.assertEqual(self.send(0, 0, b"x").status, 409)
        self.assertEqual(self.commit().status, 409)

        # Straight after begin the copy is still being emptied. Through the
        # server it would be ready by the next request: its poll() moves the updater too
        self.service.handle("POST", "/ota/begin", {}, json.dumps({"manifest": manifest()}).encode())
        status, reply = self.service.handle("POST", "/ota/file", {"index": "0", "offset": "0"}, b"x")
        self.assertEqual(status, 409)
        self.assertIn("try again", reply["error"])

    def test_pieces_of_a_file_arrive_in_order(self):
        self.started()
        index = sorted(FILES).index("app.mpy")
        content = FILES["app.mpy"]

        self.assertEqual(self.send(index, 0, content[:8192]).json["received"], 8192)
        reply = self.send(index, 4096, content[4096:8192])           # a piece already there, or skipped
        self.assertEqual(reply.status, 409)
        self.assertIn("offset 8192 expected", reply.json["error"])
        self.assertEqual(self.send(index, 8192, content[8192:16384]).json["received"], 16384)

        # From the top: offset 0 starts the file again
        self.assertEqual(self.send(index, 0, content[:100]).json["received"], 100)
        self.assertEqual(self.read("b/app.mpy"), content[:100])

    def test_pieces_that_do_not_fit_are_refused(self):
        self.started()
        index = sorted(FILES).index("main.py")
        self.assertEqual(self.send(index, 0, FILES["main.py"] + b"too much").status, 400)
        self.assertEqual(self.send(len(FILES), 0, b"x").status, 400)
        self.assertEqual(self.send(-1, 0, b"x").status, 400)
        self.assertEqual(fakes.http_upload(self.server, "/ota/file?index=one", b"x").status, 400)
        self.assertEqual(self.tree("b"), [])

    def test_commit_needs_every_file(self):
        self.started()
        self.upload(skip=("lib/sensor.mpy",))
        reply = self.commit()
        self.assertEqual(reply.status, 409)
        self.assertIn("lib/sensor.mpy", reply.json["error"])
        self.assertEqual(self.service.state, "receiving")            # it can still be completed
        self.upload()
        self.assertEqual(self.commit().status, 200)

    # --- what goes wrong ---

    def test_corrupted_file_is_caught_before_the_restart(self):
        self.started()
        broken = dict(FILES, **{"lib/sensor.mpy": b"M\x06\xff\xfe\x00sensoR"})    # same length, one byte off
        self.upload(broken)
        self.commit()
        self.poll()

        status = self.status()
        self.assertEqual(status["state"], "failed")
        self.assertIn("lib/sensor.mpy", status["error"])
        self.assertFalse(self.service.busy)
        self.poll(5000)
        self.assertEqual(self.resets, 0)
        self.assertEqual(device.read_state(), {"active": "a"})      # the running version stays
        self.assertFalse(os.path.exists(self.path("b/manifest.json")))

    def test_update_left_halfway_is_dropped(self):
        self.started()
        self.upload(skip=("main.py", "version.json"))
        self.poll(59000)
        self.assertTrue(self.service.busy)
        self.poll(2000)

        status = self.status()
        self.assertEqual(status["state"], "failed")
        self.assertIn("interrupted", status["error"])
        self.assertFalse(self.service.busy)
        self.assertEqual(self.send(0, 0, b"x").status, 409)
        self.assertEqual(boot.choose(self.folder + os.sep), ("a", False))

    def test_each_piece_keeps_the_update_alive(self):
        self.started()
        index = sorted(FILES).index("main.py")
        for _ in range(4):
            self.poll(40000)
            self.assertEqual(self.send(index, 0, FILES["main.py"]).status, 200)
        self.assertEqual(self.service.state, "receiving")

    def test_abort(self):
        self.started()
        self.upload(skip=("version.json",))
        reply = fakes.http_request(self.server, "POST", "/ota/abort")
        self.assertEqual(reply.json["state"], "idle")
        self.assertFalse(self.service.busy)
        self.assertEqual(self.commit().status, 409)

    def test_failure_is_cleared_by_the_next_update(self):
        self.started()
        self.poll(61000)
        self.assertEqual(self.service.state, "failed")
        self.started()
        self.assertIsNone(self.status()["error"])

    def test_error_while_working_does_not_stop_the_loop(self):
        self.started()
        self.upload()
        os.remove(self.path("b/main.py"))                            # gone between the arrival and the check
        self.commit()
        self.poll()
        self.assertEqual(self.service.state, "failed")
        self.assertEqual(self.resets, 0)


class ServerTest(fakes.DeviceTest):
    """The small HTTP server: clients that are slow, silent, or ask for something else."""

    def setUp(self):
        super().setUp()
        self.use_board()
        self.run_from(None)
        self.server = httpd.Server(updater.Updater(micropython="1.27.0", reset=lambda: None), port=8080)

    def test_listens_on_the_port_asked_for(self):
        self.assertEqual(fakes.FakeSocket.instances[0].bound, ("", 8080))

    def test_other_paths_are_not_found(self):
        reply = fakes.http_request(self.server, "GET", "/")
        self.assertEqual((reply.status, reply.json), (404, {"error": "not found"}))
        self.assertEqual(reply.headers["content-length"], str(len(reply.body)))
        self.assertTrue(reply.client.closed)

    def test_request_that_arrives_in_pieces(self):
        raw = fakes.http_raw("POST", "/ota/begin", {"manifest": "{}"})
        reply = fakes.http_exchange(self.server, packets=[raw[:10], raw[10:45], raw[45:]])
        # Read whole, and refused for what it asks: here no copy is running
        self.assertEqual(reply.status, 409)
        self.assertIn("USB", reply.json["error"])

    def test_long_answer_goes_out_in_blocks(self):
        client = fakes.FakeSocket()
        client.incoming = [fakes.http_raw("GET", "/ota")]
        client.send_limit = 100
        self.server._sock.pending.append(client)
        for _ in range(200):
            self.server.poll()
        reply = fakes.HttpReply(client.sent)
        self.assertEqual(reply.status, 200)
        self.assertEqual(reply.json["state"], "idle")
        self.assertTrue(client.closed)

    def test_silent_client_gives_up_its_place(self):
        silent = fakes.FakeSocket()
        self.server._sock.pending.append(silent)
        for _ in range(100):
            self.clock.advance_ms(40)
            self.server.poll()
        self.assertTrue(silent.closed)
        self.assertEqual(fakes.http_request(self.server, "GET", "/ota").status, 200)

    def test_client_that_does_not_read_is_dropped(self):
        client = fakes.FakeSocket()
        client.incoming = [fakes.http_raw("GET", "/ota")]
        client.send_limit = 0
        self.server._sock.pending.append(client)
        for _ in range(300):
            self.clock.advance_ms(40)
            self.server.poll()
        self.assertTrue(client.closed)
        self.assertEqual(client.sent, b"")

    def test_client_that_closes_halfway(self):
        raw = fakes.http_raw("POST", "/ota/begin", {"manifest": "{}"})
        reply = fakes.http_exchange(self.server, packets=[raw[:30], b""])
        self.assertIsNone(reply.status)
        self.assertEqual(fakes.http_request(self.server, "GET", "/ota").status, 200)

    def test_request_too_large(self):
        raw = fakes.http_raw("POST", "/ota/file?index=0&offset=0", b"x" * 20000, "application/octet-stream")
        reply = fakes.http_exchange(self.server, raw)
        self.assertIsNone(reply.status)
        self.assertTrue(reply.client.closed)
        self.assertIn("request too large", self.stdout.getvalue())

    def test_server_also_moves_the_updater(self):
        polled = []
        self.server.updater.poll = lambda: polled.append(1)
        self.server.poll()
        self.assertEqual(polled, [1])


if __name__ == "__main__":
    unittest.main()
