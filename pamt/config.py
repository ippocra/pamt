"""User configuration for PAmt.

A tiny JSON config file in the per-user data directory (``platformdirs``),
e.g. ``%LOCALAPPDATA%\\PAmt\\config.json`` on Windows,
``~/.config/PAmt/config.json`` on Linux,
``~/Library/Application Support/PAmt/config.json`` on macOS.

Only sane values are kept; unknown keys are ignored and missing keys fall
back to defaults, so a partial or hand-edited file never breaks the app.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

try:
    from platformdirs import user_config_dir
    def _default_config_path() -> Path:
        return Path(user_config_dir("PAmt")) / "config.json"
except Exception:  # pragma: no cover - platformdirs should always be present
    def _default_config_path() -> Path:
        return Path.home() / ".config" / "PAmt" / "config.json"


def config_path() -> Path:
    """Location of the config file (overridable in tests)."""
    return _default_config_path()

# Defaults. `clean_audio_enabled` is ON by default: clean audio (Desert Ant
# Labs Clear) is baked into the binary, so it works out of the box.
DEFAULTS: dict[str, Any] = {
    "clean_audio_enabled": True,
}


def _load() -> dict[str, Any]:
    """Read the config file, merged over the defaults (best-effort)."""
    cfg = dict(DEFAULTS)
    path = config_path()
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, dict):
            for key in DEFAULTS:
                if key in data:
                    cfg[key] = data[key]
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("could not read config %s: %s", path, e)
    return cfg


def _save(cfg: dict[str, Any]) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")


def get(key: str) -> Any:
    """Return a config value (default when missing)."""
    return _load().get(key, DEFAULTS[key])


def set_value(key: str, value: Any) -> None:
    """Set a config value and persist it. Unknown keys are rejected."""
    if key not in DEFAULTS:
        raise KeyError(f"unknown config key: {key!r}")
    cfg = _load()
    cfg[key] = value
    _save(cfg)


def is_clean_audio_enabled() -> bool:
    """True (the default) unless the user disabled clean audio in config."""
    try:
        return bool(get("clean_audio_enabled"))
    except Exception:
        return True
