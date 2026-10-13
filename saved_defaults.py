"""Project-local startup defaults for the single and dual OECT front panels."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent
_KINDS = {"single", "dual"}


def _path(kind: str) -> Path:
    if kind not in _KINDS:
        raise ValueError(f"Unknown GUI defaults kind: {kind!r}")
    return PROJECT_DIR / f"{kind}_gui_defaults.json"


def load_defaults(kind: str) -> dict:
    """Read saved controls, or return an empty mapping on first launch."""
    path = _path(kind)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read {path.name}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return data


def save_defaults(kind: str, values: dict) -> Path:
    """Atomically save validated control values for the next GUI launch."""
    path = _path(kind)
    if not isinstance(values, dict):
        raise TypeError("GUI defaults must be a mapping")
    try:
        serialized = json.dumps(values, indent=2, sort_keys=True, allow_nan=False) + "\n"
    except (TypeError, ValueError) as exc:
        raise ValueError(f"GUI defaults contain an unsupported value: {exc}") from exc
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=PROJECT_DIR,
            prefix=f".{kind}_gui_defaults_", suffix=".tmp", delete=False,
        ) as stream:
            temporary = stream.name
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)
    return path
