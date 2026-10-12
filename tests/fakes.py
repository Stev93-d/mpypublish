"""
Fake MicroPython, for testing on a PC the code that runs on the board.

Importing this file puts a fake `machine` in sys.modules and loads the
mpypublish package of the board as `device` (with `updater` and `httpd`): on
the PC the name mpypublish is the tool's. DeviceTest gives every test a
clock that only moves when the test says so, and clean fake hardware.
"""

import contextlib
import importlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

DEVICE_SOURCE = Path(__file__).resolve().parent.parent / "src" / "mpypublish" / "device"


class FakeTime:
    """The ticks of MicroPython: milliseconds that pass only with advance_ms()."""

    def __init__(self):
        self.ms = 5000

    def advance_ms(self, ms):
        self.ms += ms

    def ticks_ms(self):
        return self.ms

    @staticmethod
    def ticks_diff(a, b):
        return a - b


class FakeWDT:
    instances = []
    # Ports whose watchdog cannot wait a minute refuse the timeout
    refuse = False

    def __init__(self, id=0, timeout=5000):
        if FakeWDT.refuse:
            raise ValueError("timeout too long")
        self.timeout = timeout
        self.feeds = 0
        FakeWDT.instances.append(self)

    def feed(self):
        self.feeds += 1


def _reset():
    machine.reset_calls += 1


machine = types.ModuleType("machine")
machine.WDT = FakeWDT
machine.reset = _reset
machine.reset_calls = 0
sys.modules["machine"] = machine


class FakeSocket:
    """
    A socket that does not touch the network of the PC.

    Listening: accept() hands out the clients the test put in pending.
    As a client: incoming are the packets arriving (b"" = the client
    closed), sent what the board sent; send_limit limits the bytes every
    send() takes (0 = buffer full).
    """

    instances = []

    def __init__(self, *args):
        self.bound = None
        self.closed = False
        self.pending = []
        self.incoming = []
        self.sent = b""
        self.send_limit = None
        FakeSocket.instances.append(self)

    def setsockopt(self, *args):
        pass

    def bind(self, address):
        self.bound = address

    def listen(self, backlog):
        pass

    def setblocking(self, flag):
        pass

    def accept(self):
        if not self.pending:
            raise OSError(11)
        return self.pending.pop(0), ("192.168.1.20", 50000)

    def recv(self, size):
        if not self.incoming:
            raise OSError(11)
        data = self.incoming.pop(0)
        if len(data) > size:
            self.incoming.insert(0, data[size:])
            data = data[:size]
        return data

    def send(self, data):
        if self.send_limit == 0:
            raise OSError(11)
        count = len(data) if self.send_limit is None else min(len(data), self.send_limit)
        self.sent += bytes(data[:count])
        return count

    def close(self):
        self.closed = True


class FakePoll:
    """select.poll() for the FakeSockets: ready is the listening socket that has clients waiting."""

    def __init__(self):
        self.sockets = []

    def register(self, sock, eventmask=1):
        self.sockets.append(sock)

    def ipoll(self, timeout=-1, flags=0):
        return iter([(sock, 1) for sock in self.sockets if sock.pending])


fake_socket = types.ModuleType("socket")
fake_socket.socket = FakeSocket
fake_socket.AF_INET, fake_socket.SOCK_STREAM = 2, 1
fake_socket.SOL_SOCKET, fake_socket.SO_REUSEADDR = 1, 4

fake_select = types.ModuleType("select")
fake_select.poll = FakePoll
fake_select.POLLIN = 1


def _load_device():
    name = "mpypublish_device"
    folder = DEVICE_SOURCE / "mpypublish"
    spec = importlib.util.spec_from_file_location(
        name, folder / "__init__.py", submodule_search_locations=[str(folder)])
    package = importlib.util.module_from_spec(spec)
    sys.modules[name] = package
    spec.loader.exec_module(package)
    return package


device = _load_device()
updater = importlib.import_module("mpypublish_device.updater")
httpd = importlib.import_module("mpypublish_device.httpd")
httpd.socket = fake_socket
httpd.select = fake_select

clock = FakeTime()


class HttpReply:
    """An HTTP answer read from a FakeSocket: status, headers, body and, if it is, JSON."""

    def __init__(self, raw):
        self.raw = raw
        head, _, body = raw.partition(b"\r\n\r\n")
        lines = head.decode().split("\r\n")
        self.status = int(lines[0].split(" ")[1]) if raw else None
        self.headers = {}
        for line in lines[1:]:
            name, _, value = line.partition(":")
            self.headers[name.strip().lower()] = value.strip()
        self.body = body
        self.json = json.loads(body.decode()) if body else None


def http_exchange(server, raw=None, packets=None, max_polls=5000):
    """
    Connects a client to the server of the board and runs poll() until the
    server closes. The fake client stays in reply.client.
    """
    client = FakeSocket()
    client.incoming = list(packets) if packets is not None else [raw]
    server._sock.pending.append(client)

    polls = 0
    while not client.closed and polls < max_polls:
        clock.advance_ms(2)
        server.poll()
        polls += 1

    reply = HttpReply(client.sent)
    reply.client = client
    reply.polls = polls
    return reply


def http_raw(method, path, body=None, content_type="application/json"):
    payload = b"" if body is None else body if isinstance(body, bytes) else json.dumps(body).encode()
    head = "%s %s HTTP/1.1\r\nHost: board\r\nContent-Type: %s\r\nContent-Length: %d\r\n\r\n" % (
        method, path, content_type, len(payload))
    return head.encode() + payload


def http_request(server, method, path, body=None):
    """Sends a request to the server of the board, the body as JSON, and returns the answer."""
    return http_exchange(server, http_raw(method, path, body))


def http_upload(server, path, data):
    """A POST with a binary body, like the pieces of the files of an update."""
    return http_exchange(server, http_raw("POST", path, bytes(data), "application/octet-stream"))


class DeviceTest(unittest.TestCase):
    """Fake clock and hardware cleaned up, and what the code prints kept in self.stdout."""

    def setUp(self):
        global clock
        self.stdout = io.StringIO()
        redirect = contextlib.redirect_stdout(self.stdout)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

        FakeWDT.instances.clear()
        FakeWDT.refuse = False
        FakeSocket.instances.clear()
        machine.reset_calls = 0

        clock = self.clock = FakeTime()
        updater.time = clock
        self.forget()

    def forget(self):
        """What the package of the board remembers from one call to the next: as after a restart."""
        device._trial = None
        device._version = None

    def make_dir(self):
        """A temporary folder that stands for the filesystem of the board."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return os.path.realpath(tmp.name)

    def use_board(self):
        """An empty board: its root is the folder returned."""
        folder = self.make_dir()
        self.addCleanup(setattr, device, "ROOT", device.ROOT)
        device.ROOT = folder + "/"
        self.addCleanup(sys.path.__setitem__, slice(None), list(sys.path))
        return folder

    def run_from(self, slot):
        """As if boot.py had started this copy; None = app not started by boot.py."""
        sys.path[0] = "/" + slot if slot else "/lib"
        self.forget()
