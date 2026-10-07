"""Load config.toml and resolve relative paths against the project folder."""

import tomllib
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PROJECT_DIR / "config.toml"


def load_config(path=None):
    path = Path(path) if path else DEFAULT_CONFIG
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except UnicodeDecodeError:
        raise SystemExit(f"{path} is not valid UTF-8. Save it as UTF-8 and try again.")


def resolve(path_str):
    """Relative paths are relative to the project folder. Bare program names
    such as "ffmpeg" are returned unchanged so they are looked up on PATH."""
    p = Path(path_str)
    if p.is_absolute():
        return str(p)
    if "/" not in path_str and "\\" not in path_str and not (PROJECT_DIR / p).exists():
        return path_str
    return str(PROJECT_DIR / p)


def read_text(path_str):
    """Read a prompt file. Accepts UTF-8 (with or without BOM) or Windows-1252."""
    data = Path(resolve(path_str)).read_bytes()
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252")


def save_string(section, key, value, path=None):
    """Set key = "value" in [section] of config.toml, editing only that line so
    the comments in the file stay. The key must already be in the file."""
    import json
    import re

    path = Path(path) if path else DEFAULT_CONFIG
    lines = path.read_text(encoding="utf-8").split("\n")
    current = None
    for i, line in enumerate(lines):
        header = re.match(r"\s*\[([^\]]+)\]\s*$", line)
        if header:
            current = header.group(1).strip()
        elif current == section and re.match(rf"\s*{re.escape(key)}\s*=", line):
            # A JSON string is a valid TOML basic string.
            lines[i] = f"{key} = {json.dumps(value, ensure_ascii=False)}"
            path.write_text("\n".join(lines), encoding="utf-8")
            return
    raise KeyError(f"{section}.{key} is not in {path}")
