"""
The board, seen from the PC: mpremote commands, serial ports, and the ways to
stop the app, keep the board alive during an upload and get it back when it
stops answering.
"""

import json
import subprocess
import time

from . import Error

# The two copies of the app on the board, and the files boot.py chooses with
SLOTS = ("a", "b")
BOOT_FILE = "boot.py"
STATE_FILE = "ota.json"

# While this file is there boot.py starts nothing: the board stays at the REPL
HOLD_FILE = "hold"

# Name under which the main.py of a board that still has its code in the
# root is set aside during an upload
MAIN_PARKED = "main.py.off"

# PIN for the updates over the network; without it they are off
PIN_FILE = "ota.pin"

# What mip installs: shared by the two copies, and never touched
LIB_FOLDER = "lib"

# A reconnection of mpremote restarts the interpreter, and boot.py enters the
# folder of the active copy: commands on the board always start from the root
AT_ROOT = "import os\nos.chdir('/')\n"

# With the app stopped the board has no watchdog, and during an upload it can
# hang: USB port there but silent, until someone pulls the cable (seen on an
# ESP32-S3 with MicroPython 1.27). So every command of the upload arms a
# watchdog, or feeds it: a board that hangs restarts by itself within this
# time, and the upload goes on from where it was. Where the watchdog cannot
# wait this long (RP2 stops at 8 seconds) the upload goes without.
DEPLOY_WDT_MS = 60000
ARM_WATCHDOG = AT_ROOT + (
    "import machine\n"
    "try:\n"
    f"    _wdt = machine.WDT(timeout={DEPLOY_WDT_MS})\n"
    "    _wdt.feed()\n"
    "except Exception:\n"
    "    _wdt = None\n"
)
FEED_WATCHDOG = "_wdt and _wdt.feed()"


def run_mpremote(target: str, *args, timeout: float = 120) -> subprocess.CompletedProcess:
    """Runs an mpremote command. A board that does not answer within timeout counts as a failed command."""
    cmd = ["mpremote", "connect", target] + list(args)
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 1, "", f"no answer in {timeout:.0f} seconds")
    except FileNotFoundError:
        raise Error("mpremote not found: pip install mpremote")


def last_line(result: subprocess.CompletedProcess) -> str:
    lines = (result.stderr or result.stdout).strip().splitlines()
    return lines[-1] if lines else "no details"


def mpremote_step(target: str, description: str, *args, attempts: int = 3) -> bool:
    """Runs an mpremote command, trying again if it fails. False, with the reason printed, if it never works."""
    result = None
    for _ in range(attempts):
        result = run_mpremote(target, *args)
        if result.returncode == 0:
            return True
        time.sleep(1)

    print(f"{description}: command failed ({last_line(result)})")
    return False


# ============================================================================
# Serial port
# ============================================================================

# USB vendors of the boards MicroPython runs on and of the serial adapters
# they carry. Anything else on a serial port is not looked at.
BOARD_VENDORS = {
    0x303A: "Espressif",
    0x10C4: "CP210x",
    0x1A86: "CH34x",
    0x0403: "FTDI",
    0x2E8A: "Raspberry Pi",
    0xF055: "MicroPython",
}


def board_ports() -> list:
    """Serial ports that look like a board, as (port, description)."""
    from serial.tools import list_ports
    # Just after it appears Windows lists a port without a name for a moment: it is not there yet
    return [(p.device, p.description) for p in list_ports.comports()
            if p.device and p.vid in BOARD_VENDORS]


def pick_port(requested, candidates: list) -> str:
    """
    The port to work on: the one asked for, otherwise the only board connected.

    Raises:
        Error: no board, or more than one, and no port given
    """
    if requested:
        return requested
    if len(candidates) == 1:
        return candidates[0][0]
    if not candidates:
        raise Error("no board connected: check the USB cable, or give the port with -p")
    found = ", ".join(f"{port} ({description})" for port, description in candidates)
    raise Error(f"more than one board connected, give the port with -p: {found}")


def find_port(requested) -> str:
    """
    The port to work on, waiting a few seconds if it is not there yet: after
    a reset the USB port of a board disappears for 4-5 seconds.
    """
    if not requested:
        deadline = time.time() + 10
        while not board_ports() and time.time() < deadline:
            time.sleep(0.5)
    return pick_port(requested, board_ports())


# ============================================================================
# Stopping the app, and getting the board back
# ============================================================================

def wait_for_board(target: str, attempts: int = 20, arm: bool = True) -> bool:
    """
    Waits for the board to answer again after a reset (the USB port goes and comes back).

    The first command it gets arms the watchdog of the upload: from there on
    a board that hangs restarts by itself. With arm=False the board is left
    without, which is what is wanted once the upload is over.
    """
    for _ in range(attempts):
        time.sleep(1)
        if run_mpremote(target, "exec", ARM_WATCHDOG if arm else "pass", timeout=15).returncode == 0:
            return True
    return False


def blind_reset(target: str):
    """
    Restarts a board that does not answer, writing the command without waiting for a reply.

    In the "port there but silent" state the board runs nothing while the PC
    holds the port open: what was written to it runs when the port closes.
    It is how the board came back the one time this was needed, when not
    even esptool could talk to it.
    """
    import serial
    try:
        with serial.Serial(target, 115200, timeout=0.2, write_timeout=2) as port:
            port.write(b"\x03\x03\x02\r\nimport machine\r\nmachine.reset()\r\n")
            port.flush()
    except OSError:                     # serial.SerialException too
        pass


def board_is_stuck():
    print("The board does not answer: the app is NOT running.")
    print("Unplug the USB cable, plug it back in and run the command again.")


def recover_board(target: str) -> bool:
    """
    The board stopped answering in the middle of an upload: gets it going again.

    First with a reset written blind; if that is not enough the watchdog of
    the upload restarts it anyway within its time.
    """
    print(f"The board does not answer: resetting it, or waiting for the watchdog"
          f" (up to {DEPLOY_WDT_MS // 1000} seconds)...")
    blind_reset(target)
    if wait_for_board(target, attempts=40):
        return True
    board_is_stuck()
    return False


def stop_app(target: str) -> bool:
    """
    Stops the app before an upload.

    An app that arms a watchdog cannot just be interrupted from USB: the
    board would restart in the middle of the upload. So the file that tells
    boot.py to start nothing is created and the board is reset: it stays at
    the REPL, without a watchdog. A board that still has its code in the root
    has no such boot.py: there main.py is set aside.
    """
    print("Stopping the app on the board...")
    held = mpremote_step(target, "Stopping the app", "exec", (
        AT_ROOT +
        f"open('{HOLD_FILE}', 'w').close()\n"
        "try:\n"
        f"    os.rename('main.py', '{MAIN_PARKED}')\n"
        "except OSError:\n"
        "    pass\n"
    ))
    if not held or not mpremote_step(target, "Reset", "reset"):
        return False
    return wait_for_board(target)


def release(target: str, uploaded: bool) -> bool:
    """
    Lets the app start again at the next boot: removes the file that holds
    boot.py and the old main.py that was set aside, which goes back to its
    place instead if the upload failed.
    """
    if uploaded:
        action = f"os.remove('{MAIN_PARKED}')"
    else:
        action = f"os.rename('{MAIN_PARKED}', 'main.py')"

    return mpremote_step(target, "Releasing the board", "exec", (
        AT_ROOT +
        f"for step in (lambda: {action}, lambda: os.remove('{HOLD_FILE}')):\n"
        "    try:\n"
        "        step()\n"
        "    except OSError:\n"
        "        pass\n"
    ))


def reset(target: str) -> bool:
    """Resets the board: also the only way to get rid of the watchdog of the upload"""
    return mpremote_step(target, "Reset", "reset")


def restart_previous_app(target: str) -> bool:
    """The upload failed: puts things back and restarts the board with the app it had"""
    if release(target, False) and reset(target):
        print("Board restarted with the app it had before")
        return True
    board_is_stuck()
    return False


# ============================================================================
# Reading and writing in the root of the board
# ============================================================================

# Everything the commands need to know about a board, in one round trip
STATUS_SCRIPT = AT_ROOT + '''
import json
def load(path):
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception:
        return None
    return data if isinstance(data, dict) else None
def exists(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False
try:
    s = os.statvfs("/")
    free = s[0] * s[3]
except Exception:
    free = None
print("STATUS", json.dumps({
    "micropython": os.uname().release,
    "state": load("ota.json"),
    "slots": {slot: load(slot + "/version.json") or {} for slot in ("a", "b") if exists(slot + "/main.py")},
    "pin": exists("ota.pin"),
    "hold": exists("hold"),
    "free": free,
}))
'''


# The files of the board with their size, indented like their folders
TREE_SCRIPT = '''
def tree(path, indent):
    for name in sorted(os.listdir(path or "/")):
        full = (path + "/" + name) if path else name
        info = os.stat(full)
        if info[0] & 0x4000:
            print("TREE", "  " * indent + name + "/")
            tree(full, indent + 1)
        else:
            print("TREE", "  " * indent + name, info[6])
tree("", 0)
'''


def parse_status(output: str):
    """The dictionary printed by STATUS_SCRIPT; None if it is not in the output"""
    for line in output.splitlines():
        if line.startswith("STATUS "):
            try:
                status = json.loads(line[len("STATUS "):])
            except ValueError:
                return None
            return status if isinstance(status, dict) else None
    return None


def read_status(target: str, tree: bool = False) -> dict:
    """
    What is on the board: MicroPython, ota.json ("state"), the version of
    the copies that can start ("slots"), whether there is a PIN, free room.
    With tree=True also the files, one per line, in "tree".

    Reading interrupts the app, if it is running.

    Raises:
        Error: the board does not say
    """
    result = run_mpremote(target, "exec", STATUS_SCRIPT + (TREE_SCRIPT if tree else ""))
    status = parse_status(result.stdout) if result.returncode == 0 else None
    if status is None:
        raise Error(f"cannot read the board on {target}: {last_line(result)}")
    if tree:
        status["tree"] = [line[len("TREE "):] for line in result.stdout.splitlines() if line.startswith("TREE ")]
    return status


def write_root_file(target: str, name: str, text) -> bool:
    """Writes a small text file in the root of the board; None as text removes it"""
    if text is None:
        action = (
            "try:\n"
            f"    os.remove({name!r})\n"
            "except OSError:\n"
            "    pass\n"
        )
    else:
        action = (
            f"with open({name!r}, 'w') as f:\n"
            f"    f.write({text!r})\n"
        )
    return mpremote_step(target, f"Writing {name}", "exec", AT_ROOT + action)


class BoardConsole:
    """
    The serial port of the board, from the PC. Reading does not interrupt the app.

    If the USB port goes and comes back (it does at every reset of the board)
    it opens again by itself.
    """

    def __init__(self, port: str):
        self.port = port
        self.text = ""
        self._serial = None

    def _open(self):
        import serial
        if self._serial is None:
            self._serial = serial.Serial(self.port, 115200, timeout=0.2, write_timeout=2)
        return self._serial

    def poll(self):
        """Adds to self.text what has arrived"""
        try:
            data = self._open().read(4096)
        except OSError:                 # serial.SerialException too
            self.close()
            time.sleep(0.2)
            return
        if data:
            self.text += data.decode("utf-8", "replace")

    def send(self, data: bytes) -> bool:
        try:
            self._open().write(data)
            return True
        except OSError:
            self.close()
            return False

    def close(self):
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:
                pass
            self._serial = None
