"""PAmt — Private Annotator Meeting Transcriber.

Records meeting audio (microphone + system audio) as two WAV tracks for
offline transcription. Fully local: no network, no cloud, nothing leaves
the machine.
"""

from __future__ import annotations

from pathlib import Path

# Version is the single source of truth in .version (repo root). It is
# read at import time so the same bump updates:
#   * the Python package (pamt.__version__)
#   * the PyInstaller binary name (pamt-<version>-<platform>)
#   * the GitHub release tag/notes (CI reads the same file)
_VERSION_FILE = Path(__file__).resolve().parent.parent / ".version"

try:
    __version__ = _VERSION_FILE.read_text(encoding="utf-8").strip()
except OSError:
    __version__ = "0.0.0-dev"


def _pkg_version() -> str:
    return __version__


__all__ = ["__version__", "_pkg_version"]
