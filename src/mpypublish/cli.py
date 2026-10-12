"""
Command line of mpypublish.

    mpypublish build        # only the package, the board is not touched
"""

import argparse
import sys
from pathlib import Path

from . import Error, __version__
from . import build as builder
from . import config as configuration


def load_config(args):
    return configuration.load(Path(args.config) if args.config else configuration.find())


def cmd_build(args) -> int:
    config = load_config(args)
    build_dir, package = builder.build(config, False if args.no_mpy else None, verbose=args.verbose)
    print(f"  {build_dir}")
    print(f"  {builder.write_package_zip(build_dir, package)}")
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

    args = parser.parse_args(argv)
    try:
        return args.run(args)
    except Error as e:
        print(f"error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
