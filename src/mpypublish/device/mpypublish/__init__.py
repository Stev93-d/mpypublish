"""
mpypublish on the board: which copy of the app is running, and the
confirmation of a copy on trial.

The app is in /a or in /b; boot.py, in the root, chooses which one to start
by reading ota.json:

    {"active": "a"}
    {"active": "a", "trial": "b", "boots": 0, "version": "2.2.0"}
    {"active": "b", "last": {"result": "ok", "version": "2.2.0"}}

A copy that arrived from the network starts on trial, once. The app says it
works with confirm(); if it does not, at the next restart boot.py goes back
to the copy of before and leaves "rolled_back" in "last".

    import mpypublish

    if wifi.isconnected() and sensor_ok():
        mpypublish.confirm()          # does nothing when there is nothing to confirm
"""

import json
import os
import sys

# Where ota.json and the two copies are; the tests point it to a folder of the PC
ROOT = "/"
SLOTS = ("a", "b")
STATE_FILE = "ota.json"

_trial = None
_version = None


def current_slot():
    """The copy that is running, "a" or "b"; None if the app was not started by boot.py."""
    # boot.py puts the folder of the copy it chose first in sys.path
    first = sys.path[0] if sys.path else ""
    for slot in SLOTS:
        if first == "/" + slot:
            return slot
    return None


def app_dir():
    """
    The folder of the copy that is running, "/a" or "/b": where the files
    that came with the package are. Empty string if the app was not started
    by boot.py.
    """
    slot = current_slot()
    return ROOT + slot if slot else ""


def other_slot(slot):
    return SLOTS[1] if slot == SLOTS[0] else SLOTS[0]


def root_path(name=""):
    """A path in the root of the board, where ota.json and the two copies are"""
    return ROOT + name


def read_state():
    """The content of ota.json; an empty dictionary if it is missing or not valid."""
    return _read(ROOT + STATE_FILE)


def _read(path):
    try:
        with open(path, "r") as f:
            data = json.load(f)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def write_state(data):
    """
    Writes ota.json without ever leaving it half written: temporary file,
    read back, then rename. False if it does not work, and the file of
    before stays as it was.
    """
    path = ROOT + STATE_FILE
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f)
        if _read(tmp) != data:
            raise ValueError("the file read back is not the one written")
        try:
            os.rename(tmp, path)
        except OSError:
            # Where renaming over an existing file is not allowed
            os.remove(path)
            os.rename(tmp, path)
        return True
    except Exception as e:
        print("ota.json not written:", e)
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def version():
    """
    {"name", "version", "git", "built"} of the copy that is running, from
    the version.json `mpypublish build` puts in every package. The file is
    read once. Without it (files copied by hand) the version is "dev".
    """
    global _version
    if _version is None:
        info = {"name": None, "version": "dev", "git": None, "built": None}
        folder = app_dir()
        data = _read(folder + "/version.json" if folder else "version.json")
        for key in info:
            value = data.get(key)
            if isinstance(value, str) and value:
                info[key] = value
        _version = info
    return _version


def start_trial(slot, new_version):
    """Puts on trial the copy just written: boot.py starts it at the next restart, once."""
    return write_state({
        "active": other_slot(slot),
        "trial": slot,
        "boots": 0,
        "version": new_version,
    })


def running_trial():
    """True if the copy that is running is on trial and has not confirmed yet."""
    global _trial
    if _trial is None:
        slot = current_slot()
        _trial = slot is not None and read_state().get("trial") == slot
    return _trial


def confirm():
    """
    The copy on trial works: from here on it is the active one.

    To be called when the app has shown it can do its job, and can still be
    updated: on the network, with the updater answering. When the copy is
    not on trial it does nothing, so it can stay in the main loop.

    Returns:
        False only if the confirmation could not be written
    """
    global _trial
    if not running_trial():
        return True
    done = write_state({
        "active": current_slot(),
        "last": {"result": "ok", "version": version()["version"]},
    })
    if done:
        _trial = False
        print("mpypublish: version %s confirmed" % version()["version"])
    return done
