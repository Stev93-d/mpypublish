"""
mpypublish.toml: what an app is made of and what a deploy leaves alone.

    [app]
    name = "thermostat"
    version = "2.2.0"
    micropython = "1.27.0"      # optional: boards and mpy-cross of another one are refused
    mpy = true

    [[source]]
    from = "src"                # a folder, or a single file
    to = ""                     # folder inside the slot; "" is the top
    exclude = ["*.md", "tests/"]

    [board]
    keep = ["config.json", "data/"]     # never touched by a deploy
    ready = "Main loop started"         # line of the boot log that says the app is up
    ready_timeout = 60

    [ota]
    path = "/ota"

Paths are relative to the folder the file is in.
"""

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import Error

CONFIG_FILE = "mpypublish.toml"


@dataclass
class Source:
    path: Path
    to: str = ""
    exclude: list = field(default_factory=list)


@dataclass
class Config:
    root: Path
    name: str
    version: str = "dev"
    micropython: str = None
    mpy: bool = False
    sources: list = field(default_factory=list)
    keep: list = field(default_factory=list)
    ready: str = None
    ready_timeout: float = 60
    ota_path: str = "/ota"

    def kept(self, path: str) -> bool:
        """True for a file of the board that belongs to the app's data, not to its code"""
        for entry in self.keep:
            if entry.endswith("/"):
                if path.startswith(entry):
                    return True
            elif path == entry:
                return True
        return False


def find(start: Path = None) -> Path:
    """The mpypublish.toml of the project: in this folder or in one above, the way git finds .git"""
    folder = (start or Path.cwd()).resolve()
    for candidate in (folder, *folder.parents):
        if (candidate / CONFIG_FILE).is_file():
            return candidate / CONFIG_FILE
    raise Error(f"no {CONFIG_FILE} here or in the folders above")


def _table(data: dict, name: str, known: tuple) -> dict:
    table = data.get(name, {})
    if not isinstance(table, dict):
        raise Error(f"[{name}] must be a table")
    # A misspelled key would otherwise be ignored without a word
    unknown = sorted(set(table) - set(known))
    if unknown:
        raise Error(f"[{name}]: unknown key '{unknown[0]}' (known: {', '.join(known)})")
    return table


def _value(table: dict, section: str, key: str, kind, default=None):
    value = table.get(key, default)
    if value is not None and (not isinstance(value, kind) or isinstance(value, bool) and kind is not bool):
        raise Error(f"[{section}] {key}: wrong type")
    return value


def _strings(table: dict, section: str, key: str) -> list:
    values = table.get(key, [])
    if not isinstance(values, list) or not all(isinstance(value, str) and value for value in values):
        raise Error(f"[{section}] {key}: must be a list of strings")
    return values


def _inside(path: str, section: str, key: str) -> str:
    """A path on the board, without a leading slash and without a way out of its folder"""
    path = path.replace("\\", "/").strip("/")
    if ".." in path.split("/"):
        raise Error(f"[{section}] {key}: '..' is not allowed")
    return path


def load(path: Path) -> Config:
    path = Path(path).resolve()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise Error(f"{path}: {e.strerror}")
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise Error(f"{path}: {e}")

    unknown = sorted(set(data) - {"app", "source", "board", "ota"})
    if unknown:
        raise Error(f"{path.name}: unknown section [{unknown[0]}]")

    app = _table(data, "app", ("name", "version", "micropython", "mpy"))
    name = _value(app, "app", "name", str)
    # It ends up in file names, and the board compares it with its own
    if not name or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", name):
        raise Error("[app] name: needed, letters, digits, '-' and '_' only")
    version = _value(app, "app", "version", str, "dev")
    if not re.fullmatch(r"[A-Za-z0-9._+-]{1,32}", version):
        raise Error("[app] version: letters, digits and '.', '-', '+', '_' only")

    sources = []
    entries = data.get("source", [])
    if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
        raise Error("[[source]] must be written with double brackets")
    for entry in entries:
        entry = _table({"source": entry}, "source", ("from", "to", "exclude"))
        origin = _value(entry, "source", "from", str)
        if not origin:
            raise Error("[[source]] from: needed")
        sources.append(Source(
            path=path.parent / origin,
            to=_inside(_value(entry, "source", "to", str, ""), "source", "to"),
            exclude=_strings(entry, "source", "exclude"),
        ))
    if not sources:
        raise Error(f"{path.name}: no [[source]], nothing to put on the board")

    board = _table(data, "board", ("keep", "ready", "ready_timeout"))
    keep = []
    for entry in _strings(board, "board", "keep"):
        folder = entry.replace("\\", "/").endswith("/")
        keep.append(_inside(entry, "board", "keep") + ("/" if folder else ""))

    ota = _table(data, "ota", ("path",))
    ota_path = "/" + _inside(_value(ota, "ota", "path", str, "/ota"), "ota", "path")

    return Config(
        root=path.parent,
        name=name,
        version=version,
        micropython=_value(app, "app", "micropython", str),
        mpy=_value(app, "app", "mpy", bool, False),
        sources=sources,
        keep=keep,
        ready=_value(board, "board", "ready", str),
        ready_timeout=float(_value(board, "board", "ready_timeout", (int, float), 60)),
        ota_path=ota_path,
    )
