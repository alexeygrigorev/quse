"""Helpers for reading credentials stored by OpenCode."""

import json
import os
from pathlib import Path


_DEFAULT_AUTH_PATH = Path.home() / ".local" / "share" / "opencode" / "auth.json"


def default_auth_path() -> Path:
    data_home = os.environ.get("XDG_DATA_HOME")
    if isinstance(data_home, str) and data_home.strip():
        return Path(data_home) / "opencode" / "auth.json"
    return _DEFAULT_AUTH_PATH


def read_auth_token(provider: str, auth_path: Path | None = None) -> str | None:
    path = auth_path or default_auth_path()
    try:
        with path.open(encoding="utf-8") as file:
            data = json.load(file)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None

    if not isinstance(data, dict):
        return None
    entry = data.get(provider)
    if not isinstance(entry, dict):
        return None
    token = entry.get("key")
    if not isinstance(token, str) or not token.strip():
        return None
    return token.strip()
