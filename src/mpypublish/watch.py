"""
Last step of a deploy: the app is started and its boot log is read, to check
that what runs is the version that was uploaded.
"""

import re
import time

from . import board

# At the REPL: cancels the line, leaves the raw REPL if it was left on, restarts the interpreter
SOFT_REBOOT = b"\x03\x02\x04"

# Without a `ready` line to wait for: how long the app has to run without a traceback
SETTLE_S = 5


def parse_boot_log(text: str, ready: str = None) -> dict:
    """What the boot log, read from the serial port, says about the app that started"""
    log = {"slot": None, "trial": False, "version": None, "git": None, "ready": False, "error": None}

    # The line boot.py prints before entering the copy
    match = re.search(r"^mpypublish: slot (\w+)( on trial)?, version (\S+) \((\S+)\)", text, re.M)
    if match:
        log["slot"] = match.group(1)
        log["trial"] = bool(match.group(2))
        log["version"] = None if match.group(3) == "None" else match.group(3)
        log["git"] = None if match.group(4) == "None" else match.group(4)

    log["ready"] = bool(ready) and ready in text

    # main.py back at the REPL after a traceback: the line before the prompt
    # says why. An exception the app caught and printed has no prompt after it.
    if "Traceback (most recent call last)" in text:
        after = text.split("Traceback (most recent call last)")[-1]
        if ">>>" in after:
            log["error"] = "error at start"
            for line in after.splitlines()[1:]:
                if line.startswith(("MicroPython ", ">>>")):
                    break
                if line.strip() and not line.startswith(" "):
                    log["error"] = line.strip()

    return log


def check_running(expected: dict, found: dict, keys=("slot", "version", "git")) -> list:
    """Differences between what was uploaded (expected) and what the board says it started (found)"""
    return [
        f"{key}: uploaded {expected.get(key)}, running {found.get(key)}"
        for key in keys if found.get(key) != expected.get(key)
    ]


def start_and_watch(target: str, expected: dict, ready: str = None, timeout: float = 60, console=None) -> tuple:
    """
    Starts the app that was just uploaded and checks that it is the one of the package.

    The board is at the REPL. The app starts with a restart of the
    interpreter, with the port open: a reset of the board would make the USB
    port disappear for a few seconds, and the first lines of the log, the
    one of boot.py among them, would be lost.

    boot.py has to say it started the expected copy and version. Then the
    log has to get to the `ready` line; without one, it is enough that the
    app runs a few seconds without a traceback.

    Returns:
        (list of the problems, empty if all is well; the log read)
    """
    console = console or board.BoardConsole(target)
    try:
        if not console.send(SOFT_REBOOT):
            return [f"port {target} does not open: the app was not started"], ""

        deadline = time.time() + timeout
        started = None
        while time.time() < deadline:
            console.poll()
            log = parse_boot_log(console.text, ready)
            if log["error"]:
                return [f"the app stopped at start: {log['error']}"], console.text
            if log["slot"] is None:
                continue

            problems = check_running(expected, log)
            if problems:
                return problems, console.text
            if started is None:
                started = time.time()
            if log["ready"] or (not ready and time.time() - started >= SETTLE_S):
                return [], console.text

        if started is None:
            return [f"boot.py did not say what it started within {timeout:.0f} seconds"], console.text
        return [f"the app did not get to '{ready}' within {timeout:.0f} seconds"], console.text
    finally:
        console.close()
