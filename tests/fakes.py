"""
Fake MicroPython modules, for testing on a PC the code that runs on the board.

Importing this file puts them in sys.modules. DeviceTest cleans them up
before every test.
"""

import contextlib
import io
import os
import sys
import tempfile
import types
import unittest


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


class DeviceTest(unittest.TestCase):
    """Fake hardware cleaned up, and what the code prints kept in self.stdout."""

    def setUp(self):
        self.stdout = io.StringIO()
        redirect = contextlib.redirect_stdout(self.stdout)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

        FakeWDT.instances.clear()
        FakeWDT.refuse = False
        machine.reset_calls = 0

    def make_dir(self):
        """A temporary folder that stands for the filesystem of the board."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return os.path.realpath(tmp.name)
