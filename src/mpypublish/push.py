"""
Update of the app over the network, from the PC.

Sends a package to the updater of the board, waits for the restart and
checks that the new version started and confirmed that it works. If it did
not, the board goes back by itself to the version of before.

To try the failures, on a board on the bench:
    --sabotage crash      the package stops with an error at start
    --sabotage hang       the package hangs at start
    --sabotage corrupt    a file arrives different from what the manifest says
    --stop-after N        the sending stops after N files, like a PC switched off halfway
In the first two cases the board has to go back by itself to the version of before.
"""

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from . import Error
from .build import PACKAGE_MANIFEST

CHUNK = 8192

SABOTAGE_MAIN = {
    "crash": b"raise RuntimeError('sabotage')\n",
    "hang": b"while True:\n    pass\n",
}


# ============================================================================
# Package
# ============================================================================

def read_package(path: Path) -> tuple:
    """
    Reads the .zip of a package.

    Returns:
        (text of the manifest, files as {path: content})

    Raises:
        Error: it is not a package, or a file is not the one the manifest says
    """
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            if PACKAGE_MANIFEST not in names:
                raise Error(f"{path} is not a package: no {PACKAGE_MANIFEST} in it")
            text = archive.read(PACKAGE_MANIFEST).decode("utf-8")
            listed = json.loads(text).get("files", {})
            files = {name: archive.read(name) for name in listed if name in names}
    except (OSError, zipfile.BadZipFile, ValueError) as e:
        raise Error(f"cannot read {path}: {e}")

    for name, entry in listed.items():
        if name not in files:
            raise Error(f"{name} is missing from the package")
        if hashlib.sha256(files[name]).hexdigest() != entry["sha256"]:
            raise Error(f"{name} is not the one the manifest says")
    return text, files


def sabotage(text: str, files: dict, how: str) -> tuple:
    """
    Breaks a package, for the tests: returns (text of the manifest, files).

    crash and hang change main.py and update the manifest: the board
    accepts the package, which then does not start. corrupt changes one byte
    without touching the manifest: the board has to notice at the check.
    """
    files = dict(files)
    if how == "corrupt":
        name = sorted(name for name in files if name.endswith((".mpy", ".py")))[-1]
        content = bytearray(files[name])
        content[-1] ^= 0xFF
        files[name] = bytes(content)
        return text, files

    manifest = json.loads(text)
    files["main.py"] = SABOTAGE_MAIN[how]
    manifest["files"]["main.py"] = {
        "sha256": hashlib.sha256(files["main.py"]).hexdigest(),
        "size": len(files["main.py"]),
    }
    manifest["version"] = f"{manifest['version']}-{how}"
    return json.dumps(manifest, indent=1), files


def sign(pin: str, nonce: str, text: str) -> str:
    """The signature the board expects: HMAC-SHA256 of its random number and the manifest"""
    return hmac.new(pin.encode(), (nonce + "\n" + text).encode(), hashlib.sha256).hexdigest()


# ============================================================================
# Requests
# ============================================================================

def request(base: str, method: str, action: str = "", body=None, timeout: float = 8) -> tuple:
    """
    One request to the updater of the board. base is its address,
    "http://192.168.1.50/ota"; action what follows, "/begin".

    body: an object to send as JSON, or bytes to send as they are.

    Returns:
        (HTTP status, JSON answer); (None, None) if the board does not answer
    """
    headers = {}
    data = None
    if isinstance(body, (bytes, bytearray)):
        data = bytes(body)
        headers["Content-Type"] = "application/octet-stream"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    elif method == "POST":
        data = b""

    call = urllib.request.Request(base + action, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(call, timeout=timeout) as response:
            status, payload = response.status, response.read()
    except urllib.error.HTTPError as e:
        status, payload = e.code, e.read()
    except (OSError, ValueError):
        return None, None

    try:
        return status, json.loads(payload.decode("utf-8"))
    except ValueError:
        return status, None


def refused(what: str, status, reply) -> Error:
    if status is None:
        return Error(f"{what}: the board does not answer")
    reason = reply.get("error") if isinstance(reply, dict) else None
    return Error(f"{what}: {reason or 'error ' + str(status)}")


def wait_for(base: str, wanted: tuple, timeout: float, what: str) -> dict:
    """Waits for the state of the update to be one of those given; 'failed' is always an error."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, reply = request(base, "GET")
        if status == 200:
            if reply["state"] == "failed":
                raise Error(f"{what}: {reply.get('error')}")
            if reply["state"] in wanted:
                return reply
        time.sleep(0.5)
    raise Error(f"{what}: time is up")


# ============================================================================
# Update
# ============================================================================

def send(base: str, text: str, files: dict, pin: str = None, stop_after: int = None) -> bool:
    """
    Sends the package and asks for the check.

    Returns:
        True if the board took everything and is about to restart;
        False if the sending was stopped halfway on purpose (stop_after)

    Raises:
        Error: the board refuses, or does not answer
    """
    status, reply = request(base, "GET")
    if status != 200:
        raise refused("state of the update", status, reply)
    if not reply.get("enabled"):
        raise Error("this board cannot be updated over the network: its app was not started from one"
                    " of the two copies (deploy it over USB once)")

    body = {"manifest": text}
    if reply.get("pin"):
        if not pin:
            raise Error("this board asks for a PIN: give it with --pin")
        body["auth"] = sign(pin, reply["nonce"], text)
    status, reply = request(base, "POST", "/begin", body)
    if status != 200:
        raise refused("package refused", status, reply)
    wait_for(base, ("receiving",), 30, "getting ready")

    names = sorted(json.loads(text)["files"])
    total = sum(len(files[name]) for name in names)
    sent = 0
    for index, name in enumerate(names):
        if stop_after is not None and index >= stop_after:
            print(f"\nStopped after {index} files of {len(names)}, like a PC switched off halfway")
            return False
        content = files[name]
        for offset in range(0, max(len(content), 1), CHUNK):
            piece = content[offset:offset + CHUNK]
            status, reply = request(base, "POST", f"/file?index={index}&offset={offset}", piece)
            if status != 200:
                raise refused(f"\n{name}", status, reply)
            sent += len(piece)
        print(f"\r  {index + 1}/{len(names)} files, {sent * 100 // max(total, 1)}%", end="", flush=True)
    print()

    status, reply = request(base, "POST", "/commit")
    if status != 200:
        raise refused("check", status, reply)
    # After the check the board restarts: from there on it may not answer any more
    deadline = time.time() + 30
    while time.time() < deadline:
        status, reply = request(base, "GET", timeout=3)
        if status == 200 and reply["state"] == "failed":
            raise Error(f"check not passed: {reply.get('error')}")
        if status is None or (status == 200 and reply["state"] in ("restarting", "idle")):
            return True
        time.sleep(0.3)
    raise Error("check: time is up")


def same_build(expected: dict, running: dict) -> bool:
    return all(running.get(key) == expected.get(key) for key in ("version", "git", "built"))


def watch(base: str, expected: dict, before: dict, timeout: float = 240) -> str:
    """
    After the restart: waits for the new version to answer and to confirm.

    Returns:
        "ok" if the new version runs and is confirmed,
        "rolled_back" if the one of before is back,
        "unconfirmed" if the new one runs but never confirmed,
        "timeout" if within the time it cannot be told
    """
    deadline = time.time() + timeout
    seen = None
    on_trial = False
    while time.time() < deadline:
        time.sleep(2)
        status, reply = request(base, "GET", timeout=3)
        if status != 200:
            continue

        running = reply.get("running") or {}
        build = (running.get("version"), running.get("git"), running.get("built"))
        if build != seen:
            seen = build
            print(f"  version {running.get('version')} ({running.get('git') or 'not in git'}) answers"
                  f"{', on trial' if reply.get('trial') else ''}")

        last = reply.get("last") or {}
        new = same_build(expected, running)
        on_trial = new and bool(reply.get("trial"))
        if new and not reply.get("trial") and last.get("result") == "ok":
            return "ok"
        if not new and last.get("result") == "rolled_back" and last.get("version") == expected.get("version"):
            if same_build(before, running):
                return "rolled_back"
    return "unconfirmed" if on_trial else "timeout"


def address(host: str, prefix: str = "/ota") -> str:
    """"http://192.168.1.50/ota" from "192.168.1.50", "192.168.1.50:8080" or a whole URL"""
    host = host.rstrip("/")
    if "://" not in host:
        host = "http://" + host
    return host + prefix


def push(host: str, package_path: Path, prefix: str = "/ota", pin: str = None, how: str = None,
         stop_after: int = None, wait: bool = True) -> int:
    """
    Updates the board at host with the package in package_path.

    Returns:
        0 updated and confirmed (or stopped on purpose), 2 the board went
        back to the version of before, 1 it cannot be told
    """
    base = address(host, prefix)
    status, reply = request(base, "GET")
    if status != 200 or not isinstance(reply, dict) or "running" not in reply:
        raise Error(f"no updater answers at {base}: is the board on the network, and does the app start one?")
    before = reply["running"]
    print(f"{before.get('name')}: version {before.get('version')} ({before.get('git') or 'not in git'})"
          f" in /{reply.get('slot')}, MicroPython {reply.get('micropython')}")

    text, files = read_package(package_path)
    if how:
        text, files = sabotage(text, files, how)
        print(f"Package broken on purpose: {how}")
    expected = json.loads(text)
    print(f"{Path(package_path).name}: version {expected['version']} ({expected.get('git') or 'not in git'}),"
          f" {len(files)} files, {sum(len(content) for content in files.values()) // 1024} KB")

    if not send(base, text, files, pin, stop_after):
        return 0
    print("Package checked: the board restarts with the new version on trial")
    if not wait:
        return 0

    result = watch(base, expected, before)
    if result == "ok":
        print(f"Updated and confirmed: version {expected['version']} ({expected.get('git') or 'not in git'})")
        return 0
    if result == "rolled_back":
        print(f"The new version did not confirm: version {before.get('version')}"
              f" ({before.get('git') or 'not in git'}) is back")
        return 2
    if result == "unconfirmed":
        print("The new version runs but has not confirmed: at the next restart the one of before comes back."
              " Does the app call mpypublish.confirm()?")
        return 1
    print("Time is up: it cannot be told which version is running")
    return 1
