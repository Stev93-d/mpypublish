"""
Builds the package of an app: the files that go into a slot on the board.

build/<name>/ gets the sources (compiled to .mpy when asked), the device side
of mpypublish, version.json and manifest.json, the list of the files with
their SHA-256. Next to it the same package goes into a .zip, which is what
`mpypublish push` sends over the network.
"""

import fnmatch
import hashlib
import json
import re
import shutil
import subprocess
import time
import zipfile
from pathlib import Path

from . import Error
from .config import CONFIG_FILE, Config

# List of the files of the package, with SHA-256: in the build and on the board
PACKAGE_MANIFEST = "manifest.json"
VERSION_FILE = "version.json"

# MicroPython only starts these as sources
NOT_COMPILED = ("main.py", "boot.py")

# The device side of mpypublish, added to every package
RUNTIME_SOURCE = Path(__file__).resolve().parent / "device" / "mpypublish"
RUNTIME_FOLDER = "mpypublish"


def posix(path) -> str:
    """Path with the slashes of the board, on Windows too"""
    return str(path).replace("\\", "/")


def excluded(relative: str, patterns: list) -> bool:
    """
    A pattern is matched against the path inside the source and against the
    file name; with a trailing slash it takes a whole folder, at any depth.
    """
    name = relative.rsplit("/", 1)[-1]
    for pattern in patterns:
        if pattern.endswith("/"):
            if ("/" + relative).find("/" + pattern) >= 0:
                return True
        elif fnmatch.fnmatchcase(relative, pattern) or fnmatch.fnmatchcase(name, pattern):
            return True
    return False


def source_files(path: Path) -> list:
    """Files of a source as (file, path inside the source), without caches and hidden files"""
    if path.is_file():
        return [(path, path.name)]
    if not path.is_dir():
        raise Error(f"source not found: {path}")

    files = []
    for file in sorted(path.rglob("*")):
        parts = file.relative_to(path).parts
        if file.is_file() and not any(part.startswith(".") or part == "__pycache__" for part in parts):
            files.append((file, "/".join(parts)))
    return files


def runtime_files() -> list:
    return [(file, f"{RUNTIME_FOLDER}/{file.name}") for file in sorted(RUNTIME_SOURCE.glob("*.py"))]


def collect_files(config: Config) -> list:
    """
    Files of the package, as (source, path inside the slot) pairs.

    Raises:
        Error: two files with the same destination, no main.py, a boot.py
    """
    # With `from = "."` the project folder is the source: its own files are not the app
    own = (config.root / CONFIG_FILE, config.root / "build")

    files = {}
    for source in config.sources:
        for file, relative in source_files(source.path):
            if excluded(relative, source.exclude) or file == own[0] or own[1] in file.parents:
                continue
            dest = f"{source.to}/{relative}" if source.to else relative
            if dest in files:
                raise Error(f"{files[dest]} and {file} both end up in :{dest}")
            if dest.split("/")[0] == RUNTIME_FOLDER:
                raise Error(f"{file}: the folder {RUNTIME_FOLDER}/ of the package is the one mpypublish puts there")
            files[dest] = file

    if "main.py" not in files:
        raise Error("no main.py at the top of the package: nothing would start")
    if "boot.py" in files:
        # MicroPython runs only the one in the root, which chooses the slot
        raise Error(f"{files['boot.py']} would never run from a slot: move what it does into main.py, or exclude it")

    for file, dest in runtime_files():
        files[dest] = file
    return sorted((file, dest) for dest, file in files.items())


# ============================================================================
# mpy-cross
# ============================================================================

def series(version) -> tuple:
    """(1, 27) for "1.27.0": .mpy files are good for the whole series"""
    numbers = re.findall(r"\d+", version or "")
    return tuple(int(n) for n in numbers[:2])


def mpy_cross_path():
    """The mpy-cross installed with pip; None if there is none"""
    try:
        import mpy_cross
    except (ImportError, SystemExit):
        return None
    return mpy_cross.mpy_cross


def mpy_cross_version(exe) -> str:
    """The MicroPython mpy-cross compiles for; None if it does not say"""
    try:
        result = subprocess.run([exe, "--version"], capture_output=True, text=True)
    except OSError:
        return None
    match = re.search(r"MicroPython v(\d+(?:\.\d+)+)", result.stdout + result.stderr)
    return match.group(1) if match else None


def find_mpy_cross(micropython: str = None):
    """
    mpy-cross and the MicroPython it compiles for, as (executable, version).

    Raises:
        Error: not installed, or not for the MicroPython of the boards
    """
    exe = mpy_cross_path()
    if exe is None:
        raise Error("mpy-cross not found: pip install mpy-cross==<the MicroPython of the board> (or --no-mpy)")
    found = mpy_cross_version(exe)
    if micropython and series(found) != series(micropython):
        raise Error(f"mpy-cross is for MicroPython {found}, the app is for {micropython}")
    return exe, found


def compile_mpy(exe, src: Path, dest: Path, name: str):
    """Compiles src into dest. name is the path on the board: it is what tracebacks show."""
    result = subprocess.run([exe, "-s", name, "-o", str(dest), str(src)], capture_output=True, text=True)
    if result.returncode != 0:
        reason = (result.stderr or result.stdout).strip()
        raise Error(f"{src} does not compile: {reason}")


# ============================================================================
# Package
# ============================================================================

def git_revision(folder: Path):
    """Short hash of the commit, with '-dirty' if the folder has uncommitted changes. None outside git."""
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=folder, capture_output=True, text=True, check=True
        ).stdout.strip()
        changes = subprocess.run(
            ["git", "status", "--porcelain", "--", "."], cwd=folder, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    return revision + ("-dirty" if changes else "")


def build_version(config: Config) -> dict:
    """Content of version.json: what the board says it is running"""
    return {
        "name": config.name,
        "version": config.version,
        "git": git_revision(config.root),
        "built": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def file_entry(path: Path) -> dict:
    data = path.read_bytes()
    return {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}


def local_files(folder: Path) -> dict:
    """The files of a folder, as {path on the board: {"sha256", "size"}}"""
    return {
        posix(file.relative_to(folder)): file_entry(file)
        for file in sorted(folder.rglob("*")) if file.is_file()
    }


def build(config: Config, use_mpy: bool = None, build_root: Path = None, verbose: bool = False) -> tuple:
    """
    Builds the package in build/<name>/.

    Args:
        use_mpy: compile to .mpy; None = what mpypublish.toml says

    Returns:
        (folder of the build, manifest of the package)
    """
    if use_mpy is None:
        use_mpy = config.mpy
    mpy_cross, compiled_for = find_mpy_cross(config.micropython) if use_mpy else (None, None)

    files = collect_files(config)
    build_dir = (build_root or config.root / "build") / config.name
    if build_dir.exists():
        shutil.rmtree(build_dir)
    build_dir.mkdir(parents=True)

    for src, dest in files:
        target = build_dir / dest
        target.parent.mkdir(parents=True, exist_ok=True)
        if mpy_cross and src.suffix == ".py" and dest not in NOT_COMPILED:
            target = target.with_suffix(".mpy")
            compile_mpy(mpy_cross, src, target, dest)
        else:
            shutil.copy(src, target)
        if verbose:
            print(f"  {src} -> :{posix(target.relative_to(build_dir))}")

    version = build_version(config)
    (build_dir / VERSION_FILE).write_text(json.dumps(version))

    package = dict(version)
    package["micropython"] = config.micropython or compiled_for
    package["mpy"] = bool(mpy_cross)
    package["files"] = local_files(build_dir)
    # As bytes: the text is the same on every system, without the line ends of Windows
    (build_dir / PACKAGE_MANIFEST).write_bytes(json.dumps(package, indent=1).encode())

    size = sum(entry["size"] for entry in package["files"].values())
    print(f"{config.name}: {len(package['files'])} files, {size / 1024:.0f} KB"
          f" ({'.mpy' if mpy_cross else 'sources'}), version {version['version']}"
          f" ({version['git'] or 'not in git'})")
    return build_dir, package


def write_package_zip(build_dir: Path, package: dict) -> Path:
    """The package in one file, next to the folder of the build"""
    path = build_dir.parent / f"{package['name']}-{package['version']}.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(local_files(build_dir)):
            archive.write(build_dir / name, name)
    return path
