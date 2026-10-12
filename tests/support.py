"""What the tests share: the path of the sources and a small project to build."""

import contextlib
import io
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from mpypublish import build, config

TOML = """
[app]
name = "demo"
version = "1.2.0"

[[source]]
from = "src"
exclude = ["*.md", "tests/"]

[[source]]
from = "settings.example.json"

[board]
keep = ["settings.json", "data/"]
ready = "Main loop started"
"""

FILES = {
    "src/main.py": "import app\napp.run()\n",
    "src/app.py": "from lib import sensor\n\ndef run():\n    print('Main loop started')\n",
    "src/lib/sensor.py": "def read():\n    return 20.5\n",
    "src/pages/index.html": "<h1>demo</h1>\n",
    "src/notes.md": "not for the board\n",
    "src/tests/test_app.py": "assert True\n",
    "src/__pycache__/app.cpython-312.pyc": "cache",
    "src/.env": "hidden",
    "settings.example.json": "{}\n",
}


def make_project(test, toml=TOML, files=FILES) -> Path:
    """A project in a temporary folder; returns the path of its mpypublish.toml."""
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    root = Path(os.path.realpath(tmp.name))
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
    (root / config.CONFIG_FILE).write_text(toml, encoding="utf-8")
    return root / config.CONFIG_FILE


def build_package(test, use_mpy=False, **project):
    """Builds the project of make_project: (folder of the build, manifest)."""
    settings = config.load(make_project(test, **project))
    with contextlib.redirect_stdout(io.StringIO()):
        return build.build(settings, use_mpy)
