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
* ``node`` and the SDK are **baked in**: the SDK lives in
  ``pamt/node/node_modules`` (installed by ``node setup.js``, shipped via
  package-data in the wheel and PyInstaller binary), and PAmt finds ``node``
  on PATH or bundled. In a bare source checkout the SDK may be missing; the
  tray then shows an actionable setup hint instead of a broken menu item.
* Model weights (~24 MB, LiteRT/TFLite) download once on the first clean
  run and cache in ``<user cache>/desert-ant-models`` (``XDG_CACHE_HOME``
  aware), shared with other Desert Ant apps. Nothing else leaves the machine
  except the SDK's minimal usage ping (a device id, no audio) -- see the
  README's Privacy section.

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
import threading
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
    """Return the path to a usable ``node`` executable, or None.

    Prefers the node runtime bundled inside the package (``pamt/node/node``)
    -- the official binaries ship it there and PyInstaller's
    ``--collect-all pamt`` carries it into the onefile. Falls back to a
    system ``node`` on PATH. On Windows we must return a real ``.exe``:
    ``shutil.which("node")`` can return the ``node.cmd`` shim, which
    ``subprocess.run`` cannot spawn from a PyInstaller onefile.
    """
    is_win = os.name == "nt"
    # 1) Node bundled with PAmt (installed by the CI build or setup).
    for sub in ("node", ".node"):
        base = NODE_DIR / sub
        for candidate in (
            base / "bin" / "node.exe" if is_win else base / "bin" / "node",
            base / "bin" / "node",
            base / "node.exe" if is_win else base / "node",
        ):
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
    # 2) System Node on PATH (real binary, not a .cmd shim).
    which = shutil.which("node.exe" if is_win else "node")
    if which:
        return which
    if is_win:
        return shutil.which("node")
    return None


def _sdk_ready() -> bool:
    """True if the helper + the Clear SDK are present (bundled or installed)."""
    if not _HELPER_JS.is_file():
        return False
    if not _LAUNCHER_JS.is_file():
        return False
    return (NODE_DIR / "node_modules" / "@desert-ant-labs" / "clear").is_dir()


def _model_cache_dir() -> Path:
    """Where the Clear model weights live (per-user, XDG_CACHE_HOME aware)."""
    try:
        from platformdirs import user_cache_dir
        return Path(user_cache_dir("PAmt"))
    except Exception:  # pragma: no cover
        return Path.home() / ".cache" / "PAmt"


def _model_ready() -> bool:
    """True if the Clear model weights are already cached on this machine."""
    marker = _model_cache_dir() / "desert-ant-models"
    return any(marker.rglob("*")) if marker.is_dir() else False


def warm_up_model(timeout: float = 600.0) -> None:
    """Download the Clear model weights (~24 MB) once, in the background.

    Run at app start when clean audio is enabled (the default): the first
    real clean run is then fast and never surprises the user with a network
    fetch mid-meeting. A no-op when the weights are already cached. Any
    failure (offline, missing node) is swallowed -- clean audio simply
    downloads on first use instead.
    """
    def _work() -> None:
        try:
            proc, stderr_text = _run_node(
                ["--warm-up", "--cache-root", str(_model_cache_dir())],
                timeout=timeout,
            )
            if proc.returncode == 0:
                log.info("clear model ready: %s", _model_cache_dir())
            else:
                log.warning("clear model warm-up failed (exit %s): %s",
                            proc.returncode, (stderr_text or "")[-300:])
        except Exception as e:
            log.warning("clear model warm-up failed: %s", e)

    threading.Thread(target=_work, name="clear-model-warmup",
                     daemon=True).start()


def status() -> dict:
    """Report whether clean audio is usable, with a setup hint if not.

    Returns a dict with keys: ``available`` (bool), ``node`` (str|None),
    ``sdk`` (bool), ``model`` (bool -- weights cached), ``hint`` (str --
    empty when available).
    """
    node = node_binary()
    sdk = _sdk_ready()
    model = _model_ready()
    if node and sdk:
        hint = ""
        if not model:
            hint = ("Model weights will download on first use "
                    f"(~24 MB -> {_model_cache_dir()}).")
        return {"available": True, "node": node, "sdk": True,
                "model": model, "hint": hint}
    missing = []
    if not node:
        missing.append("Node.js (node on PATH, or run the bundled setup)")
    if not sdk:
        missing.append(
            "the Clear SDK (install with `node setup.js` from this repo, "
            "or use an official PAmt binary which bundles it)"
        )
    hint = (
            "Clean audio needs: " + " and ".join(missing) + ".\n"
            "Run `node setup.js` from the repo, then restart PAmt."
        )
    return {"available": False, "node": node, "sdk": sdk,
            "model": model, "hint": hint}


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
            ["--clean", str(p), "--preset", preset,
             "--cache-root", str(_model_cache_dir())],
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
