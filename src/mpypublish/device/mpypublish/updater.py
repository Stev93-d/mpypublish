"""
Update of the app from the network

Receives a package, the one `mpypublish build` makes, into the copy that is
not running, checks its hashes and puts it on trial. The copy that is
running is not touched: if the new one does not confirm that it works, at
the next restart boot.py goes back to this one.

The updater knows nothing about HTTP servers: every request goes through
handle(), which takes method, path, query and body and gives back a status
and a dictionary. An app with a web server calls it from there; one without
uses mpypublish.httpd.

    GET  /ota              state, and what is running
    POST /ota/begin        manifest of the package, signed if there is a PIN
    POST /ota/file         a piece of a file, binary body (?index=3&offset=8192)
    POST /ota/commit       check, trial and restart
    POST /ota/abort        gives up

Everything moves in small steps from poll(), to be called at every turn of
the main loop: emptying a folder or checking the hashes never stops it for
longer than one file.

Protection: with a PIN on the board (`mpypublish pin`) whoever updates has
to sign the manifest, together with a random number read from the board,
with HMAC-SHA256: the PIN does not travel. Without a PIN anyone who reaches
the board can update it. In both cases the content travels in the clear.
"""

import binascii
import hashlib
import json
import os
import sys
import time

from . import current_slot, other_slot, read_state, root_path, start_trial, version

IDLE = "idle"
PREPARING = "preparing"         # the copy that gets the package is being emptied
RECEIVING = "receiving"
VERIFYING = "verifying"
RESTARTING = "restarting"
FAILED = "failed"

# The manifest of the package: it is in the copy like the other files, but
# the board writes it, with the text that was signed
MANIFEST = "manifest.json"
PIN_FILE = "ota.pin"

_PATH_CHARS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-."


class UpdateError(Exception):
    """A request that cannot be done: HTTP status and reason"""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


class Countdown:
    """
    A deadline measured with ticks_ms, which does not depend on the clock
    (an NTP sync can move it). Once expired it stays expired until the next
    start(), so the wrap of the ticks does not arm it again.
    """

    def __init__(self):
        self._start = None
        self._duration = 0
        self._expired = False

    def start(self, duration_ms):
        self._start = time.ticks_ms()
        self._duration = duration_ms
        self._expired = False

    def stop(self):
        self._start = None
        self._expired = False

    @property
    def running(self):
        return self._start is not None

    def expired(self):
        """True if started and the time has passed. Never started: False."""
        if self._start is None:
            return False
        if not self._expired and time.ticks_diff(time.ticks_ms(), self._start) >= self._duration:
            self._expired = True
        return self._expired


def hmac_sha256(key, message):
    """HMAC-SHA256 in hex, like hmac.new(key, message, "sha256").hexdigest()"""
    if len(key) > 64:
        key = hashlib.sha256(key).digest()
    key = key + bytes(64 - len(key))
    inner = hashlib.sha256(bytes([b ^ 0x36 for b in key]) + message).digest()
    outer = hashlib.sha256(bytes([b ^ 0x5C for b in key]) + inner).digest()
    return binascii.hexlify(outer).decode()


def _valid_path(path):
    """A relative path, inside the copy, made of plain names only"""
    if not isinstance(path, str) or not path or len(path) > 96:
        return False
    for part in path.split("/"):
        if not part or part[0] == "." or part[-1] == ".":
            return False
        for char in part:
            if char not in _PATH_CHARS:
                return False
    return True


def _is_dir(path):
    return os.stat(path)[0] & 0x4000 != 0


def _remove_tree(path):
    """Removes a folder with what is in it: one file per step."""
    try:
        names = os.listdir(path)
    except OSError:
        return
    for name in names:
        full = path + "/" + name
        if _is_dir(full):
            for _ in _remove_tree(full):
                yield
        else:
            os.remove(full)
            yield
    os.rmdir(path)


def _make_dirs(path):
    """Makes the folders the given file needs"""
    parts = path.split("/")[:-1]
    for depth in range(1, len(parts) + 1):
        try:
            os.mkdir("/".join(parts[:depth]))
        except OSError:
            pass


def _free_bytes():
    try:
        stat = os.statvfs(root_path())
        return stat[0] * stat[3]
    except Exception:
        # Where it cannot be known (on a PC) it is no reason to refuse
        return None


def _nonce():
    return binascii.hexlify(os.urandom(8)).decode()


def _series(micropython):
    """"1.27" for "1.27.0": .mpy files are good for the whole series"""
    return ".".join(str(micropython).split(".")[:2])


def _read_pin():
    try:
        with open(root_path(PIN_FILE)) as f:
            return f.read().strip()
    except OSError:
        return None


def _machine_reset():
    import machine
    machine.reset()


class Updater:
    """
    Usage:
        updater = Updater()

        # In the web server of the app, for the paths under /ota:
        status, reply = updater.handle(method, path, query, body)

        # In the main loop:
        updater.poll()
        if updater.busy:
            ...  # an update is arriving: a good moment to do less
    """

    SESSION_TIMEOUT_MS = 60000      # Without requests for this long the update is dropped
    AUTH_LOCK_MS = 10000            # Wait after a wrong PIN
    RESTART_DELAY_MS = 1500         # Time to deliver the answer before restarting
    MAX_CHUNK = 8192
    MAX_FILES = 500
    MAX_FILE_BYTES = 1024 * 1024
    SPACE_MARGIN = 64 * 1024        # Room that has to stay free once the package is written

    def __init__(self, prefix="/ota", pin=None, name=None, micropython=None, reset=None):
        """
        Args:
            prefix: where the requests of the updater are
            pin: PIN for the updates; None = the one written by `mpypublish pin`, if any
            name: name of the app, which has to be the one of the package;
                None = the one of the copy that is running
            micropython: MicroPython version that is running; None = asked to the interpreter
            reset: function that restarts the board; None = machine.reset
        """
        self.prefix = prefix
        self.name = name or version()["name"]
        self.micropython = micropython or ".".join(str(part) for part in sys.implementation.version[:3])
        pin = pin if pin is not None else _read_pin()
        self._pin = pin if isinstance(pin, str) and pin else None
        self._reset = reset or _machine_reset

        self.state = IDLE
        self.error = None
        self._nonce = _nonce()
        self._folder = None         # the copy that gets the package, as a folder
        self._slot = None
        self._version = None
        self._manifest_text = None
        self._files = []            # (path, size, sha256), sorted by path
        self._received = []         # bytes arrived of every file
        self._job = None            # generator of the work in progress (emptying, checking)
        self._session = Countdown()
        self._auth_lock = Countdown()
        self._restart = Countdown()

    @property
    def enabled(self):
        """Only an app started from one of the two copies can be updated from the network."""
        return current_slot() is not None

    @property
    def busy(self):
        """True from the start of an update to the restart."""
        return self.state in (PREPARING, RECEIVING, VERIFYING, RESTARTING)

    # =========================================================================
    # State
    # =========================================================================

    def status(self):
        saved = read_state()
        slot = current_slot()
        status = {
            "enabled": self.enabled,
            "state": self.state,
            "error": self.error,
            "slot": slot,
            "trial": slot is not None and saved.get("trial") == slot,
            "running": version(),
            "micropython": self.micropython,
            "pin": self._pin is not None,
            "progress": {
                "files": sum(1 for index in range(len(self._files)) if self._complete(index)),
                "total_files": len(self._files),
                "bytes": sum(self._received),
                "total_bytes": sum(size for _path, size, _sha in self._files),
            },
            "last": saved.get("last"),
        }
        if self._pin is not None:
            status["nonce"] = self._nonce
        return status

    def _complete(self, index):
        return self._received[index] == self._files[index][1]

    def _fail(self, message):
        print("mpypublish: update failed:", message)
        self.state = FAILED
        self.error = message
        self._job = None
        self._session.stop()

    # =========================================================================
    # Requests
    # =========================================================================

    def handle(self, method, path, query=None, body=b""):
        """
        One request, from whatever server the app has.

        Args:
            path: path of the request, without the query
            query: the query as a dictionary of strings
            body: body of the request, as bytes

        Returns:
            (HTTP status, dictionary to send back as JSON); None if the
            path is not one of the updater
        """
        if path != self.prefix and not path.startswith(self.prefix + "/"):
            return None
        action = path[len(self.prefix):]
        try:
            if action == "" and method == "GET":
                return 200, self.status()
            if method != "POST":
                raise UpdateError(404, "not found")
            if action == "/begin":
                return 200, self._begin_request(body)
            if action == "/file":
                return 200, self._file_request(query or {}, body)
            if action == "/commit":
                self.commit()
                return 200, self.status()
            if action == "/abort":
                self.abort()
                return 200, self.status()
            raise UpdateError(404, "not found")
        except UpdateError as e:
            return e.status, {"error": e.message}

    def _begin_request(self, body):
        try:
            data = json.loads(body.decode() if isinstance(body, (bytes, bytearray)) else body)
        except ValueError:
            data = None
        if not isinstance(data, dict) or not isinstance(data.get("manifest"), str):
            raise UpdateError(400, "manifest (as text) needed")
        self.begin(data["manifest"], data.get("auth"))
        return self.status()

    def _file_request(self, query, body):
        try:
            index = int(query.get("index"))
            offset = int(query.get("offset", 0))
        except (TypeError, ValueError):
            raise UpdateError(400, "index and offset have to be whole numbers")
        return self.write(index, offset, body or b"")

    # =========================================================================
    # Update
    # =========================================================================

    def begin(self, manifest_text, auth=None):
        """
        Opens an update with the manifest of the package.

        Raises:
            UpdateError: 403 wrong signature, 409 not now, 400 package not for this board
        """
        if self.state in (VERIFYING, RESTARTING):
            raise UpdateError(409, "update already in progress")
        self._check_auth(manifest_text, auth)

        slot = current_slot()
        if slot is None:
            raise UpdateError(409, "app not started from one of the two copies: deploy it over USB first")
        if read_state().get("trial"):
            raise UpdateError(409, "the version just loaded has not confirmed that it works yet")

        new_version, files = self._check_manifest(manifest_text)

        self._slot = other_slot(slot)
        self._folder = root_path(self._slot)
        self._version = new_version
        self._manifest_text = manifest_text
        self._files = files
        self._received = [0] * len(files)
        self.error = None
        self.state = PREPARING
        self._job = self._prepare()
        self._session.start(self.SESSION_TIMEOUT_MS)
        print("mpypublish: version %s arriving in /%s" % (new_version, self._slot))

    def _check_auth(self, manifest_text, auth):
        if self._pin is None:
            return
        if self._auth_lock.running and not self._auth_lock.expired():
            raise UpdateError(403, "wrong PIN a moment ago: try again in a few seconds")

        # The random number is good for one attempt: a recorded request cannot be sent again
        expected = hmac_sha256(self._pin.encode(), (self._nonce + "\n" + manifest_text).encode())
        self._nonce = _nonce()
        if auth != expected:
            self._auth_lock.start(self.AUTH_LOCK_MS)
            raise UpdateError(403, "PIN not valid")

    def _check_manifest(self, text):
        """Version and files of the package, if it is for this board: (version, [(path, size, sha256)])"""
        try:
            manifest = json.loads(text)
        except ValueError:
            manifest = None
        if not isinstance(manifest, dict):
            raise UpdateError(400, "manifest not valid")

        if manifest.get("name") != self.name:
            raise UpdateError(400, "package of another app (%s), this is %s" % (manifest.get("name"), self.name))
        # Sources run on any MicroPython, .mpy files only on the one they were compiled for
        if manifest.get("mpy") and _series(manifest.get("micropython")) != _series(self.micropython):
            raise UpdateError(400, "package for MicroPython %s, here it is %s" % (
                manifest.get("micropython"), self.micropython))

        listed = manifest.get("files")
        if not isinstance(listed, dict) or not 0 < len(listed) <= self.MAX_FILES:
            raise UpdateError(400, "list of the files missing or too long")

        files = []
        for path in sorted(listed):
            entry = listed[path]
            size = entry.get("size") if isinstance(entry, dict) else None
            sha256 = entry.get("sha256") if isinstance(entry, dict) else None
            if not _valid_path(path) or path == MANIFEST:
                raise UpdateError(400, "path not allowed: %s" % path)
            if not isinstance(size, int) or isinstance(size, bool) or not 0 <= size <= self.MAX_FILE_BYTES:
                raise UpdateError(400, "size not valid: %s" % path)
            if not isinstance(sha256, str) or len(sha256) != 64:
                raise UpdateError(400, "hash not valid: %s" % path)
            files.append((path, size, sha256))

        # Without main.py boot.py would not start it
        if "main.py" not in listed:
            raise UpdateError(400, "no main.py in the package")

        return str(manifest.get("version")), files

    def _prepare(self):
        """Empties the copy that gets the package and checks there is room"""
        for _ in _remove_tree(self._folder):
            yield
        os.mkdir(self._folder)

        free = _free_bytes()
        needed = sum(size for _path, size, _sha in self._files) + len(self._manifest_text)
        if free is not None and free < needed + self.SPACE_MARGIN:
            self._fail("not enough room: %d KB needed, %d free" % (needed // 1024, free // 1024))
            return
        self.state = RECEIVING

    def write(self, index, offset, data):
        """
        Writes a piece of a file of the package. The pieces of a file arrive
        in order; offset 0 starts it again.

        Returns:
            {"received": bytes arrived of that file, "files": whole files, "total_files": ...}
        """
        if self.state == PREPARING:
            raise UpdateError(409, "getting ready: try again in a moment")
        if self.state != RECEIVING:
            raise UpdateError(409, "no update in progress")
        if not 0 <= index < len(self._files):
            raise UpdateError(400, "no file %d" % index)

        path, size, _sha256 = self._files[index]
        if len(data) > self.MAX_CHUNK or offset + len(data) > size:
            raise UpdateError(400, "piece too large: %s" % path)
        if offset != 0 and offset != self._received[index]:
            raise UpdateError(409, "%s: offset %d expected" % (path, self._received[index]))

        target = self._folder + "/" + path
        if offset == 0:
            _make_dirs(target)
        with open(target, "wb" if offset == 0 else "ab") as f:
            f.write(data)
        self._received[index] = offset + len(data)
        self._session.start(self.SESSION_TIMEOUT_MS)

        return {
            "received": self._received[index],
            "files": sum(1 for other in range(len(self._files)) if self._complete(other)),
            "total_files": len(self._files),
        }

    def commit(self):
        """All the files are there: the check starts, and if it passes the copy goes on trial and the board restarts."""
        if self.state != RECEIVING:
            raise UpdateError(409, "no update in progress")
        missing = [self._files[index][0] for index in range(len(self._files)) if not self._complete(index)]
        if missing:
            raise UpdateError(409, "%d files missing (%s)" % (len(missing), missing[0]))

        self.state = VERIFYING
        self._job = self._verify()
        self._session.stop()

    def _verify(self):
        """Reads every file back from the flash and compares its hash: one file per step"""
        for path, size, sha256 in self._files:
            digest = hashlib.sha256()
            read = 0
            with open(self._folder + "/" + path, "rb") as f:
                while True:
                    block = f.read(1024)
                    if not block:
                        break
                    digest.update(block)
                    read += len(block)
            if read != size or binascii.hexlify(digest.digest()).decode() != sha256:
                self._fail("file not whole: %s" % path)
                return
            yield

        with open(self._folder + "/" + MANIFEST, "wb") as f:
            f.write(self._manifest_text.encode())

        if not start_trial(self._slot, self._version):
            self._fail("cannot put the new version on trial")
            return

        print("mpypublish: update checked, restarting with version %s on trial" % self._version)
        self.state = RESTARTING
        self._restart.start(self.RESTART_DELAY_MS)

    def abort(self):
        """Gives up an update not yet checked; the copy that is running was never touched."""
        if self.state in (VERIFYING, RESTARTING):
            raise UpdateError(409, "update already being checked")
        self.state = IDLE
        self.error = None
        self._job = None
        self._session.stop()

    def poll(self):
        """One step of the work in progress. To be called at every turn of the main loop."""
        if self._job is not None:
            try:
                next(self._job)
            except StopIteration:
                self._job = None
            except Exception as e:
                self._fail("error during the update: %s" % e)

        if self.state in (PREPARING, RECEIVING) and self._session.expired():
            self._fail("update interrupted: no data for %d seconds" % (self.SESSION_TIMEOUT_MS // 1000))

        if self.state == RESTARTING and self._restart.expired():
            self._restart.stop()
            self._reset()
