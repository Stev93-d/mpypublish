"""
boot.py - chooses which of the two copies of the app to start

It lives in the root of the board and only `mpypublish deploy` writes it,
over USB: an update that arrives from the network never touches it. That is
why it does as little as possible and imports nothing of the app.

The two copies are in /a and /b. ota.json says which one is active and
whether the other is on trial:

    {"active": "a"}
    {"active": "a", "trial": "b", "boots": 0, "version": "2.2.0"}

A copy on trial is started once, with a watchdog already armed. If it does
not confirm that it works (the app does, with mpypublish.confirm()), at the
next restart the one of before comes back.

Once a copy is chosen it goes into sys.path and becomes the current folder:
MicroPython then starts the main.py it finds there. With the file /hold
nothing is started and the board stays at the REPL, without a watchdog:
`mpypublish deploy` uses it to upload.
"""

import os
import sys

SLOTS = ("a", "b")
STATE = "ota.json"
HOLD = "hold"

# Time given to the copy on trial to get as far as arming its own watchdog
TRIAL_WDT_MS = 60000


def _read(path):
    try:
        import json
        with open(path) as f:
            data = json.load(f)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _write(path, data):
    """Writes to a temporary file and then renames it. False if it does not work."""
    try:
        import json
        with open(path + ".tmp", "w") as f:
            json.dump(data, f)
        try:
            os.rename(path + ".tmp", path)
        except OSError:
            # Where renaming over an existing file is not allowed
            os.remove(path)
            os.rename(path + ".tmp", path)
        return True
    except Exception:
        return False


def _usable(root, slot):
    """A copy can be started if it has its main.py: the upload writes it last"""
    try:
        os.stat(root + slot + "/main.py")
        return True
    except OSError:
        return False


def choose(root="/"):
    """
    The copy to start, and whether it is on trial.

    Returns:
        (copy, on_trial); (None, False) if no copy can be started
    """
    state = _read(root + STATE)
    active = state.get("active")
    if active not in SLOTS:
        active = SLOTS[0]
    other = SLOTS[1] if active == SLOTS[0] else SLOTS[0]

    if state.get("trial") == other:
        if not state.get("boots") and _usable(root, other):
            state["boots"] = 1
            if _write(root + STATE, state):
                return other, True
        # Already started once without a confirmation, or cannot start: the one of before stays
        _write(root + STATE, {
            "active": active,
            "last": {"result": "rolled_back", "version": state.get("version")},
        })

    for slot in (active, other):
        if _usable(root, slot):
            return slot, False
    return None, False


def boot(root="/"):
    """Enters the chosen copy. Returns the copy, or None if nothing is started."""
    try:
        os.stat(root + HOLD)
        return None
    except OSError:
        pass

    slot, trial = choose(root)
    if slot is None:
        # No copy: new board, or one that still has its code in the root
        return None

    if trial:
        # Also covers an error that stops main.py before it arms its own.
        # Where the watchdog cannot wait this long a hang is not covered.
        try:
            import machine
            machine.WDT(timeout=TRIAL_WDT_MS)
        except Exception:
            pass

    folder = root + slot
    version = _read(folder + "/version.json")
    print("mpypublish: slot %s%s, version %s (%s)" % (
        slot, " on trial" if trial else "", version.get("version"), version.get("git")))

    # lib/ too: a package can bring its libraries the way a board has them in /lib
    sys.path.insert(0, folder)
    sys.path.insert(1, folder + "/lib")
    os.chdir(folder)
    return slot


if __name__ == "__main__":
    boot()
