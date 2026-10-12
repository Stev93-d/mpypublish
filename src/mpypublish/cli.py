"""
Command line of mpypublish.

    mpypublish build        # only the package, the board is not touched
    mpypublish deploy       # build and upload to the board connected over USB
    mpypublish rollback     # back to the copy of before
    mpypublish ls           # what is on the board
    mpypublish push 192.168.1.50      # build and send over the network
    mpypublish pin 4321     # PIN the updates over the network are signed with
"""

import argparse
import re
import sys
from pathlib import Path

from . import Error, __version__, board, watch
from . import build as builder
from . import config as configuration
from . import deploy as deployer
from . import push as pusher


def load_config(args, needed=True):
    """The mpypublish.toml of the project; None, when it is not needed, if there is none"""
    if args.config:
        return configuration.load(Path(args.config))
    try:
        return configuration.load(configuration.find())
    except Error:
        if needed:
            raise
        return None


def start_app(target: str, expected: dict, config, verbose: bool = False) -> int:
    """Starts the app and says whether what runs is what was expected"""
    print("Starting the app...")
    problems, log = watch.start_and_watch(
        target, expected,
        ready=config.ready if config else None,
        timeout=config.ready_timeout if config else 60,
    )
    if problems or verbose:
        for line in log.splitlines():
            print(f"  | {line}")
    for problem in problems:
        print(f"error: {problem}")
    if problems:
        return 1

    print(f"Running: version {expected['version']} ({expected['git'] or 'not in git'}) in /{expected['slot']}")
    return 0


def describe(status: dict, target: str) -> list:
    """The lines `ls` prints about a board, before its files"""
    free = status.get("free")
    lines = [f"Board on {target}: MicroPython {status.get('micropython')}"
             + (f", {free // 1024} KB free" if free is not None else "")]

    state = status.get("state") or {}
    slots = status.get("slots") or {}
    # The same choice boot.py makes
    active = state.get("active") if state.get("active") in slots else next(iter(sorted(slots)), None)
    for slot in sorted(slots):
        version = slots[slot]
        note = "  <- active" if slot == active else ""
        if state.get("trial") == slot:
            note = "  <- on trial"
        lines.append(f"  /{slot}  {version.get('version')} ({version.get('git') or 'not in git'}),"
                     f" built {version.get('built')}{note}")
    if not slots:
        lines.append("  no copy of the app: this board was not deployed with mpypublish")

    last = state.get("last")
    if isinstance(last, dict):
        lines.append(f"  last update over the network: {last.get('result')} ({last.get('version')})")
    lines.append("  PIN for the updates over the network: " + ("set" if status.get("pin") else "not set"))
    if status.get("hold"):
        lines.append("  an upload was interrupted: run `mpypublish deploy` again")
    return lines


def cmd_build(args) -> int:
    config = load_config(args)
    build_dir, package = builder.build(config, False if args.no_mpy else None, verbose=args.verbose)
    print(f"  {build_dir}")
    print(f"  {builder.write_package_zip(build_dir, package)}")
    return 0


def cmd_deploy(args) -> int:
    config = load_config(args)
    target = board.find_port(args.port)
    package, slot = deployer.deploy(config, target, False if args.no_mpy else None, args.yes, args.verbose)
    expected = {"slot": slot, "version": package["version"], "git": package["git"]}
    return start_app(target, expected, config, args.verbose)


def cmd_rollback(args) -> int:
    config = load_config(args, needed=False)
    target = board.find_port(args.port)
    version, slot = deployer.rollback(target)
    expected = {"slot": slot, "version": version.get("version"), "git": version.get("git")}
    return start_app(target, expected, config, args.verbose)


def cmd_pin(args) -> int:
    if bool(args.pin) == args.remove:
        raise Error("give a PIN, or --remove")
    if args.pin and not re.fullmatch(r"[A-Za-z0-9]{4,32}", args.pin):
        raise Error("the PIN is 4 to 32 letters and digits")

    target = board.find_port(args.port)
    if not board.write_root_file(target, board.PIN_FILE, None if args.remove else args.pin):
        return 1
    # Writing interrupted the app, and it reads the PIN when it starts
    board.reset(target)
    print("PIN removed: whoever reaches the board on the network can update it" if args.remove
          else "PIN written: updates over the network have to be signed with it")
    return 0


def cmd_push(args) -> int:
    config = load_config(args, needed=not args.zip)
    if args.zip:
        package_path = Path(args.zip)
    else:
        build_dir, package = builder.build(config, False if args.no_mpy else None, verbose=args.verbose)
        package_path = builder.write_package_zip(build_dir, package)
    prefix = args.path or (config.ota_path if config else "/ota")
    return pusher.push(args.host, package_path, prefix, args.pin, args.sabotage, args.stop_after, not args.no_wait)


def cmd_ls(args) -> int:
    target = board.find_port(args.port)
    status = board.read_status(target, tree=True)
    for line in describe(status, target):
        print(line)
    print("Files (bytes):")
    for line in status["tree"]:
        print(f"  {line}")

    if not status.get("hold"):
        # Reading interrupted the app
        board.reset(target)
        print("The app was interrupted to read the board: restarted.")
    return 0


def main(argv=None) -> int:
    # File names and what the board prints are not always in the code page of the console
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")

    parser = argparse.ArgumentParser(
        prog="mpypublish",
        description="Deploy MicroPython apps into two copies on the board, with trial boot and rollback.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("-c", "--config", metavar="FILE",
                        help="mpypublish.toml to use; without, the one in this folder or above")
    commands = parser.add_subparsers(dest="command", required=True)

    def command(name, run, help, port=True):
        sub = commands.add_parser(name, help=help)
        if port:
            sub.add_argument("-p", "--port",
                             help="serial port (COM8, /dev/ttyACM0); without, the only board connected")
        sub.add_argument("-v", "--verbose", action="store_true", help="show every file, and the boot log")
        sub.set_defaults(run=run)
        return sub

    build = command("build", cmd_build, "build the package without touching the board", port=False)
    build.add_argument("--no-mpy", action="store_true", help="keep the .py sources instead of compiling")

    deploy = command("deploy", cmd_deploy, "build and upload over USB, into the copy that is not running")
    deploy.add_argument("--no-mpy", action="store_true", help="upload the .py sources instead of compiling")
    deploy.add_argument("-y", "--yes", action="store_true",
                        help="do not ask before removing the code a board has in its root")

    command("rollback", cmd_rollback, "make the copy of before the active one, and start it")

    pin = command("pin", cmd_pin, "set the PIN the updates over the network are signed with (the app is restarted)")
    pin.add_argument("pin", nargs="?", help="4 to 32 letters and digits")
    pin.add_argument("--remove", action="store_true", help="remove the PIN: updates need no signature")

    command("ls", cmd_ls, "show the copies and the files on the board (the app is restarted)")

    push = command("push", cmd_push, "build and send over the network, then wait for the new version to confirm",
                   port=False)
    push.add_argument("host", help="address of the board: 192.168.1.50, 192.168.1.50:8080, thermostat.local")
    push.add_argument("--pin", help="PIN of the board, if it has one")
    push.add_argument("--zip", metavar="FILE", help="package already built; without, the project is built")
    push.add_argument("--no-mpy", action="store_true", help="build the package with the .py sources")
    push.add_argument("--path", help="where the updater answers; without, [ota] path of mpypublish.toml, or /ota")
    push.add_argument("--no-wait", action="store_true", help="do not wait for the restart and the confirmation")
    push.add_argument("--sabotage", choices=("crash", "hang", "corrupt"),
                      help="break the package on purpose, to see the board go back")
    push.add_argument("--stop-after", type=int, metavar="N", help="stop after N files, to see the board give up")

    args = parser.parse_args(argv)
    try:
        return args.run(args)
    except Error as e:
        print(f"error: {e}")
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
