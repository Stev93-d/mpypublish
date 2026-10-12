"""
Command line of mpypublish.

    mpypublish build        # only the package, the board is not touched
    mpypublish deploy       # build and upload to the board connected over USB
"""

import argparse
import sys
from pathlib import Path

from . import Error, __version__, board
from . import build as builder
from . import config as configuration
from . import deploy as deployer


def load_config(args):
    return configuration.load(Path(args.config) if args.config else configuration.find())


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
    if not board.reset(target):
        board.board_is_stuck()
        return 1
    print(f"Uploaded: version {package['version']} ({package['git'] or 'not in git'}) in /{slot}")
    return 0


def main(argv=None) -> int:
    # File names and what the board prints are not always in the code page of the console
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")

    parser = argparse.ArgumentParser(prog="mpypublish", description=__doc__.strip().splitlines()[0])
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("-c", "--config", metavar="FILE",
                        help="mpypublish.toml to use; without, the one in this folder or above")
    commands = parser.add_subparsers(dest="command", required=True)

    build = commands.add_parser("build", help="build the package without touching the board")
    build.add_argument("--no-mpy", action="store_true", help="keep the .py sources instead of compiling")
    build.add_argument("-v", "--verbose", action="store_true", help="list every file")
    build.set_defaults(run=cmd_build)

    deploy = commands.add_parser("deploy", help="build and upload over USB, into the copy that is not running")
    deploy.add_argument("-p", "--port", help="serial port (COM8, /dev/ttyACM0); without, the only board connected")
    deploy.add_argument("--no-mpy", action="store_true", help="upload the .py sources instead of compiling")
    deploy.add_argument("-y", "--yes", action="store_true",
                        help="do not ask before removing the code a board has in its root")
    deploy.add_argument("-v", "--verbose", action="store_true", help="list every file")
    deploy.set_defaults(run=cmd_deploy)

    args = parser.parse_args(argv)
    try:
        return args.run(args)
    except Error as e:
        print(f"error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
