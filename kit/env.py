"""A tiny .env loader. Reads KEY=VALUE lines into os.environ.

Values already set in the environment win. Nothing is ever printed.
"""
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def load(path=None):
    """Load the repo's .env (or the given file). Returns the names it set."""
    path = Path(path) if path else ROOT / ".env"
    if not path.is_file():
        return []
    names = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _LINE.match(line)
        if not m:
            continue
        name, value = m.group(1), m.group(2)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if name not in os.environ:
            os.environ[name] = value
            names.append(name)
    return names


def get(name, default=""):
    return os.environ.get(name, default).strip() or default
