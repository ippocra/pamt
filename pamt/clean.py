"""Post-recording "clean audio" for PAmt (Desert Ant Labs Clear).

Takes a meeting WAV (mic or system track) and produces a studio-cleaned,
loudness-normalized version: background noise and room reverb pulled down,
level normalized to a broadcast-style target. Everything runs on this
machine via Desert Ant Labs' Clear model (https://desertant.com/models/clear/).
The original is never modified -- the cleaned file is written beside it
(``<name>_clean.wav``), so you can always compare or re-run.

How it is wired up (kept deliberately thin):

* The Clear model runs on the **Node** build of the Desert Ant SDK
  (``@desert-ant-labs/clear``, native entry). PAmt is a Python app, so we
  shell out to a small helper script (``node/clean.js``, invoked via
  ``node/run.js``) with ``node``. The helper is the only thing that knows
  the SDK's API; Python just feeds it file paths and reads back JSON.
* ``node`` and the SDK are **optional** at install time. If they are not
  present, PAmt still works exactly as before -- recording is untouched --
  and the tray shows an actionable "set up clean audio" note instead of a
  broken menu item. Setup is: ``node setup.js`` from the repo (or the steps
  in the README, "Clean audio").
* Model weights (~24 MB, LiteRT/TFLite) download once on first use and
  cache in the platform model cache, shared with other Desert Ant apps.
  Nothing else leaves the machine except the SDK's minimal usage ping (a
  device id, no audio) -- see the README's Privacy section.

Loudness presets
----------------
Clear enhances (denoise + dereverb) and then optionally masters to a
target loudness; the presets below map to the targets the SDK ships
(``LoudnessPreset``):

* **meeting** (default) -- enhancement only, no mastering: Clear's native
  level is already clean, balanced speech, the safest choice for listening
  back and for feeding transcription.
* **podcast** -- mastered to the podcast target (-19 LUFS).
* **video** -- mastered to the YouTube target (-14 LUFS), for voice-over
  picture.
* **voiceover** -- mastered to the loudest shipped preset (spotify,
  -14 LUFS), VO/announcer-style.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import __version__  # noqa: F401  (kept for parity with other modules)

log = logging.getLogger(__name__)

_PKG_DIR = Path(__file__).resolve().parent

# Where the Node SDK lives. We install into the package directory itself so
# the helper script and its node_modules sit together and PyInstaller can
# collect them if a build chooses to ship the binary with clean audio on.
NODE_DIR = _PKG_DIR / "node"
# The helper and its launcher live inside pamt/node/ (next to node_modules,
# because ESM resolves packages from the importing file's own directory).
_HELPER_JS = NODE_DIR / "clean.js"
_LAUNCHER_JS = NODE_DIR / "run.js"

# Preset metadata for the tray/status UI. The actual preset -> LUFS mapping
# lives in node/clean.js (the helper is the only thing that knows the SDK);
# here we keep display labels and the timeout cap.
PRESETS: dict[str, dict] = {
    "meeting": {"label": "Meeting (default)"},
    "podcast": {"label": "Podcast"},
    "video": {"label": "Video"},
    "voiceover": {"label": "Voiceover"},
}
DEFAULT_PRESET = "meeting"

# Cap so a very long meeting doesn't burn CPU for minutes on a cold model.
# Clear runs ~100x realtime once warm; 2h of audio is well under a minute.
MAX_INPUT_SECONDS = 6 * 3600


@dataclass
class CleanResult:
    input_path: Path
    output_path: Optional[Path]
    duration_sec: Optional[float]
    processing_sec: Optional[float]
    preset: str
    note: str = ""


class CleanUnavailable(RuntimeError):
    """Raised when node / the SDK is not set up, with an actionable message."""


def node_binary() -> Optional[str]:
    """Return the path to a usable ``node`` executable, or None."""
    # 1) PAmt-owned Node (shipped with the app / installed by setup.js).
    candidates = [
        NODE_DIR / "node" / "bin" / "node",          # bundled, per-platform
        NODE_DIR / ".node" / "bin" / "node",
    ]
    for c in candidates:
        if c.is_file() and os.access(c, os.X_OK):
            return str(c)
    # 2) System Node on PATH.
    return shutil.which("node")


def _sdk_ready() -> bool:
    """True if the helper + SDK modules are importable from NODE_DIR."""
    if not _HELPER_JS.is_file():
        return False
    if not _LAUNCHER_JS.is_file():
        return False
    return (NODE_DIR / "node_modules" / "@desert-ant-labs" / "clear").is_dir()


def status() -> dict:
    """Report whether clean audio is usable, with a setup hint if not.

    Returns a dict with keys: ``available`` (bool), ``node`` (str|None),
    ``sdk`` (bool), ``hint`` (str -- empty when available).
    """
    node = node_binary()
    sdk = _sdk_ready()
    if node and sdk:
        return {"available": True, "node": node, "sdk": True, "hint": ""}
    missing = []
    if not node:
        missing.append("Node.js (node on PATH, or run the bundled setup)")
    if not sdk:
        missing.append(
            "the Clear SDK (install with `npm i --prefix "
            f"{NODE_DIR} @desert-ant-labs/clear`)"
        )
    hint = (
            "Clean audio needs: " + " and ".join(missing) + ".\n"
            "Run the setup script in this repo (setup.js) or the steps in the "
            "README ('Clean audio' section), then restart PAmt."
        )
    return {"available": False, "node": node, "sdk": sdk, "hint": hint}


def _run_node(argv: list[str], timeout: float):
    """Run the helper. Returns (proc, stderr_text).

    The SDK's native lib logs (LiteRT INFO/WARNING lines) write straight to
    the real stderr fd; we keep that text but only surface it on failure, so
    successful runs stay quiet in PAmt's logs.
    """
    node = node_binary()
    if not node:
        raise CleanUnavailable(
            "Node.js is not available. See 'Clean audio' in the README."
        )
    env = dict(os.environ)
    proc = subprocess.run(
        [node, str(_LAUNCHER_JS), *argv],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        env=env,
        cwd=str(NODE_DIR) if NODE_DIR.is_dir() else None,
    )
    return proc, (proc.stderr or "").strip()


def clean_wav(path: str | Path, preset: str = DEFAULT_PRESET) -> CleanResult:
    """Enhance one WAV with Clear. Returns a CleanResult (output beside input).

    Raises CleanUnavailable if node/SDK is missing, and RuntimeError with the
    SDK's stderr if the run itself fails.
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"not a file: {p}")
    if p.suffix.lower() not in (".wav", ".wave"):
        raise ValueError(f"only WAV input is supported (got {p.suffix!r})")

    st = status()
    if not st["available"]:
        raise CleanUnavailable(st["hint"])

    preset = preset if preset in PRESETS else DEFAULT_PRESET
    # Generous timeout: first run also downloads ~24 MB of weights.
    timeout = 600.0 + (MAX_INPUT_SECONDS / 60.0) * 30.0
    try:
        proc, stderr_text = _run_node(
            ["--clean", str(p), "--preset", preset],
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(
            f"clean audio timed out after {timeout:.0f}s. "
            "The recording may be too long; try a shorter clip."
        ) from e

    # The helper prints a single JSON object on stdout on success.
    try:
        out = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"clean audio: helper returned no JSON (exit {proc.returncode}). "
            f"stderr: {stderr_text[-400:]}"
        ) from e

    if proc.returncode != 0 or not out.get("ok"):
        err = out.get("error") or stderr_text[-400:] or "unknown error"
        raise RuntimeError(f"clean audio failed: {err[:500]}")

    out_path = Path(out["output"])
    return CleanResult(
        input_path=p,
        output_path=out_path if out_path.is_file() else None,
        duration_sec=out.get("durationSec"),
        processing_sec=out.get("processingSec"),
        preset=preset,
        note=out.get("note") or "",
    )


def clean_meeting(rec_dir: str | Path,
                 preset: str = DEFAULT_PRESET,
                 tracks: tuple[str, ...] = ("mic", "system")) -> list[CleanResult]:
    """Clean every available track in a recording folder.

    ``rec_dir`` is a PAmt meeting folder containing ``<ts>_mic.wav`` and/or
    ``<ts>_system.wav``. Missing tracks are skipped. Returns one result per
    cleaned track.
    """
    d = Path(rec_dir)
    if not d.is_dir():
        raise FileNotFoundError(f"not a recording folder: {d}")
    results: list[CleanResult] = []
    for kind in tracks:
        matches = sorted(m for m in d.glob(f"*_{kind}.wav")
                         if m.suffix.lower() == ".wav")
        if not matches:
            continue
        src = matches[-1]  # newest if the folder somehow has several
        results.append(clean_wav(src, preset=preset))
    if not results:
        raise FileNotFoundError(
            f"no {(' or '.join(tracks))!r} track WAV found in {d}"
        )
    return results
