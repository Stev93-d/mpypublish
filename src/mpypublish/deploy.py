"""
Deploy over USB.

On the board the package lives in one of two folders, /a and /b. It is
uploaded into the one that is not running, and only at the end ota.json makes
it the active one, so the version of before stays whole. Which one to start
is decided by boot.py, in the root.

Steps: build the package, stop the app, read what is on the board, copy what
is missing or different, read the hashes back, remove the code that is no
longer part of the app, let the board start again.
"""

import json
from pathlib import Path

from . import Error, board
from . import build as builder
from .board import BOOT_FILE, HOLD_FILE, LIB_FOLDER, MAIN_PARKED, PIN_FILE, SLOTS, STATE_FILE
from .config import Config

BOOT_SOURCE = Path(__file__).resolve().parent / "device" / "boot.py"


# ============================================================================
# What is on the board and what has to change
# ============================================================================

# Lists the files of the board with size and SHA-256. END says the list is whole.
LIST_SCRIPT = '''
import hashlib, binascii
def walk(path):
    for name in os.listdir(path or "/"):
        full = (path + "/" + name) if path else name
        if full in SKIP:
            continue
        info = os.stat(full)
        if info[0] & 0x4000:
            walk(full)
            continue
        h = hashlib.sha256()
        with open(full, "rb") as f:
            while True:
                chunk = f.read(1024)
                if not chunk:
                    break
                h.update(chunk)
        print("F", info[6], binascii.hexlify(h.digest()).decode(), full)
        _wdt and _wdt.feed()
walk("")
print("END")
'''


def skipped_folders(config: Config) -> list:
    """Folders of the board that are not even read: the data of the app can be large, and is not ours"""
    return [LIB_FOLDER] + [entry.rstrip("/") for entry in config.keep if entry.endswith("/")]


def parse_file_list(output: str) -> dict:
    """
    Reads the output of LIST_SCRIPT: {path: {"sha256", "size"}}.

    Raises:
        ValueError: the list is not whole, or cannot be understood
    """
    files = {}
    complete = False
    for line in output.splitlines():
        line = line.strip()
        if line == "END":
            complete = True
        elif line.startswith("F "):
            # The path is the last field because it can have spaces in it
            _, size, sha256, path = line.split(" ", 3)
            files[path] = {"sha256": sha256, "size": int(size)}
    if not complete:
        raise ValueError("list of the files on the board not whole")
    return files


def read_device_files(target: str, skip=()) -> dict:
    """The files on the board with their hashes. To be called with the app stopped."""
    script = board.ARM_WATCHDOG + f"SKIP = {tuple(skip)!r}" + LIST_SCRIPT
    result = board.run_mpremote(target, "exec", script)
    if result.returncode != 0:
        raise Error(f"cannot read the files on the board: {board.last_line(result)}")
    try:
        return parse_file_list(result.stdout)
    except ValueError as e:
        raise Error(str(e))


def other_slot(slot: str) -> str:
    return SLOTS[1] if slot == SLOTS[0] else SLOTS[0]


def target_slot(state, device_files: dict) -> str:
    """
    The slot to upload into: the one that is not running.

    state is the ota.json of the board. Without an active slot (new board, or
    one that still has its code in the root) the first one is used.
    """
    active = state.get("active") if isinstance(state, dict) else None
    if active in SLOTS and f"{active}/main.py" in device_files:
        return other_slot(active)
    return SLOTS[0]


def device_image(build_dir: Path, slot: str) -> dict:
    """
    What the upload brings to the board, as {path on the board: file on the
    PC}: the package in its slot, then boot.py and ota.json in the root.
    """
    image = {f"{slot}/{path}": build_dir / path for path in builder.local_files(build_dir)}
    image[BOOT_FILE] = BOOT_SOURCE

    state = build_dir.parent / f"{build_dir.name}.root" / STATE_FILE
    state.parent.mkdir(exist_ok=True)
    state.write_text(json.dumps({"active": slot}))
    image[STATE_FILE] = state
    return image


def image_entries(image: dict) -> dict:
    """Size and SHA-256 of the files of device_image, to compare them with the board"""
    return {path: builder.file_entry(source) for path, source in image.items()}


def upload_order(path: str) -> tuple:
    """
    The order of the copy: a board that restarts halfway must not find a
    slot that looks whole. The main.py of the slot goes after its modules;
    boot.py and ota.json, which make it the active one, go last.
    """
    if path == STATE_FILE:
        rank = 3
    elif path == BOOT_FILE:
        rank = 2
    elif path.split("/")[-1] in builder.NOT_COMPILED:
        rank = 1
    else:
        rank = 0
    return rank, path


def plan_sync(wanted: dict, device_files: dict, slot: str = None, kept=None) -> dict:
    """
    Compares what has to be on the board (wanted) with what is there.

    slot is where the package goes: only the package stays there. The other
    slot is the version of before and is not touched. kept says which files
    are data of the app.

    Returns:
        {"upload": new or changed files,
         "delete": code that is no longer needed,
         "unknown": files the tool does not know, left where they are}
    """
    upload = sorted(
        path for path, entry in wanted.items()
        if device_files.get(path, {}).get("sha256") != entry["sha256"]
    )

    # A board deployed by hand has its code in the root: what is left of it goes
    prefix = f"{slot}/" if slot else ""
    package = {path[len(prefix):] for path in wanted if path.startswith(prefix)} if slot else set(wanted)
    code_dirs = {path.split("/")[0] for path in package if "/" in path}

    delete, unknown = [], []
    for path in sorted(device_files):
        top = path.split("/")[0]
        if path in wanted or path in (MAIN_PARKED, HOLD_FILE, PIN_FILE) or (kept and kept(path)):
            continue
        if top == LIB_FOLDER and "/" in path:
            continue
        if slot and top in SLOTS and "/" in path:
            if top == slot:
                delete.append(path)
        elif path == STATE_FILE + ".tmp":
            delete.append(path)
        elif path.endswith((".py", ".mpy")) or top in code_dirs or path in package:
            delete.append(path)
        else:
            unknown.append(path)

    return {"upload": upload, "delete": delete, "unknown": unknown}


def code_in_the_root(plan: dict, device_files: dict) -> list:
    """What a board that was deployed by hand loses: its code moves into the slots"""
    gone = [path for path in plan["delete"]
            if path.split("/")[0] not in SLOTS and path != STATE_FILE + ".tmp"]
    if MAIN_PARKED in device_files:
        gone.insert(0, "main.py")
    return gone


def room_needed(wanted: dict, device_files: dict, upload: list) -> int:
    """Bytes the upload adds to the flash: new files whole, changed ones by the difference"""
    return sum(wanted[path]["size"] - device_files.get(path, {}).get("size", 0) for path in upload)


# ============================================================================
# Copying and removing
# ============================================================================

def remove_device_files(target: str, paths: list) -> bool:
    """Removes files from the board, and the folders left empty"""
    for start in range(0, len(paths), 40):
        script = (
            board.ARM_WATCHDOG +
            f"for p in {paths[start:start + 40]!r}:\n"
            "    try:\n"
            "        os.remove(p)\n"
            "    except OSError:\n"
            "        continue\n"
            "    while '/' in p:\n"
            "        p = p.rsplit('/', 1)[0]\n"
            "        try:\n"
            "            os.rmdir(p)\n"
            "        except OSError:\n"
            "            break\n"
        )
        if not board.mpremote_step(target, "Removing files", "exec", script):
            return False
    return True


def copy_commands(image: dict, paths: list, group: int = 15) -> list:
    """
    mpremote arguments that copy to the board the given files of image
    ({path on the board: file on the PC}).

    One invocation every `group` files: it creates the folders, arms the
    watchdog of the upload and feeds it after every file.
    """
    commands = []
    for start in range(0, len(paths), group):
        chunk = paths[start:start + group]
        folders = sorted({
            "/".join(parts[:depth])
            for parts in (path.split("/") for path in chunk)
            for depth in range(1, len(parts))
        })
        args = ["exec", (
            board.ARM_WATCHDOG +
            f"for d in {folders!r}:\n"
            "    try:\n"
            "        os.mkdir(d)\n"
            "    except OSError:\n"
            "        pass\n"
        )]
        for path in chunk:
            args += ["+", "cp", str(image[path]), ":/" + path, "+", "exec", board.FEED_WATCHDOG]
        commands.append(args)
    return commands


def copy_files(target: str, image: dict, paths: list) -> bool:
    """Copies to the board the given files of image. False if the copy stops halfway."""
    copied = 0
    for args in copy_commands(image, paths):
        result = board.run_mpremote(target, *args, timeout=300)
        if result.returncode != 0:
            print(f"Copy stopped after {copied} files ({board.last_line(result)})")
            return False
        copied += args.count("cp")
        print(f"  {copied}/{len(paths)} files copied")
    return True


def sync_files(target: str, image: dict, wanted: dict, skip=(), attempts: int = 4) -> dict:
    """
    Brings to the board the files of image that are missing or different,
    until the hashes read back from the board all match those of wanted.

    If the board hangs halfway, the watchdog of the upload restarts it and
    the copy goes on from the files still missing.

    Returns:
        the files on the board at the end, as read_device_files gives them

    Raises:
        Error: the board does not come back, or the files do not arrive whole
    """
    missing = sorted(wanted)
    for _ in range(attempts):
        try:
            present = read_device_files(target, skip)
            missing = sorted(plan_sync(wanted, present)["upload"], key=upload_order)
            if not missing:
                return present
            print(f"  to copy: {len(missing)} files of {len(wanted)}")
            if copy_files(target, image, missing):
                continue
        except Error as e:
            print(e)

        if not board.recover_board(target):
            raise Error("the board did not come back")

    raise Error(f"files did not arrive whole after {attempts} attempts ({', '.join(missing)})")


# ============================================================================
# Deploy
# ============================================================================

def confirm(question: str) -> bool:
    try:
        return input(question + " y/N: ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def check_micropython(config: Config, package: dict, found):
    """The .mpy files are for one MicroPython: on a board with another one they would not load"""
    expected = package["micropython"]
    if expected and (package["mpy"] or config.micropython) and builder.series(found) != builder.series(expected):
        raise Error(f"nothing uploaded: the board runs MicroPython {found or '(unknown)'},"
                    f" the package is for {expected}")


def deploy(config: Config, target: str, use_mpy: bool = None, yes: bool = False, verbose: bool = False) -> tuple:
    """
    Uploads the app into the slot that is not running and makes it the active one.

    The board is left stopped at the REPL, ready to be restarted.

    Returns:
        (manifest of the package, slot it went into)

    Raises:
        Error: when the upload stops before the end
    """
    # First, away from the board: a package that does not build must not stop the app
    build_dir, package = builder.build(config, use_mpy, verbose=verbose)
    print(f"  for the update over the network: {builder.write_package_zip(build_dir, package)}")

    print(f"Board on {target}")
    if not board.stop_app(target):
        raise Error(f"cannot stop the app on {target}: nothing uploaded")

    # Until the new copy is all on the board, an error brings back the app of before
    restart_previous = True
    try:
        check_micropython(config, package, board.read_micropython_version(target))

        skip = skipped_folders(config)
        device_files = read_device_files(target, skip)
        slot = target_slot(board.read_json_file(target, STATE_FILE), device_files)
        print(f"Slot: /{slot}")

        image = device_image(build_dir, slot)
        wanted = image_entries(image)
        plan = plan_sync(wanted, device_files, slot, config.kept)

        gone = code_in_the_root(plan, device_files)
        if gone and not yes:
            print("This board has code in its root: from now on the app lives in /a and /b.")
            print("These files will be removed, and boot.py replaced by the one that chooses the slot:")
            for path in gone:
                print(f"  {path}")
            if not confirm("Go on?"):
                raise Error("nothing uploaded (--yes skips the question)")

        # While it is being written the slot must not look startable: it has
        # the modules of two versions. Its main.py goes first and comes back last.
        stale = [path for path in plan["delete"] if path.startswith(slot + "/")]
        old_main = f"{slot}/main.py"
        if (plan["upload"] or stale) and old_main in device_files and old_main not in stale:
            stale.append(old_main)
        if stale:
            if not remove_device_files(target, stale):
                raise Error("cannot make room in the slot: nothing uploaded")
            for path in stale:
                del device_files[path]

        upload = plan_sync(wanted, device_files)["upload"]
        needed, free = room_needed(wanted, device_files, upload), board.read_free_bytes(target)
        if free is not None and needed > free:
            raise Error(f"not enough room on the board: about {needed // 1024} KB to add, {free // 1024} KB free"
                        f" (two copies of the app take twice the room)")

        present = sync_files(target, image, wanted, skip)
        print(f"{len(wanted)} files on the board, hashes verified")
        plan = plan_sync(wanted, present, slot, config.kept)
        for path in plan["unknown"]:
            print(f"  not mine, left where it is: {path}")

        # From here the active copy is the new one. If something goes wrong
        # the board stays stopped at the REPL
        restart_previous = False
        leftovers = plan["delete"]
        if leftovers:
            print(f"Removing {len(leftovers)} files that are no longer part of the app")
            if verbose:
                for path in leftovers:
                    print(f"  {path}")
            remove_device_files(target, leftovers)
            remaining = plan_sync(wanted, read_device_files(target, skip), slot, config.kept)["delete"]
            if remaining:
                raise Error(f"files not removed ({', '.join(remaining)}): the board stays stopped")

        if not board.release(target, True):
            board.board_is_stuck()
            raise Error("the board stays stopped")
    except Error:
        if restart_previous:
            board.restart_previous_app(target)
        raise

    return package, slot
