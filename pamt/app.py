"""PAmt system-tray application.

Run:  pamt
"""

from __future__ import annotations

import logging
import platform
import queue
import sys
import threading
import time
from pathlib import Path

import pystray
from PIL import Image, ImageDraw

from . import __version__
from .audio import Recorder

log = logging.getLogger(__name__)

MEETINGS_DIR = Path.home() / "meetings"
_ASSETS = Path(__file__).resolve().parent / "assets"
_ICON_SRC = _ASSETS / "icon_64.png"


def _base_icon() -> Image.Image:
    """Load the packaged logo, falling back to a drawn glyph if missing."""
    try:
        return Image.open(_ICON_SRC).convert("RGBA")
    except OSError:
        return _icon_image(False, 0)

# Global hotkey: Ctrl+Alt+R  (start/stop)
HOTKEY_MODS = ("ctrl", "alt")
HOTKEY_KEY = "r"

_LOGO: Image.Image | None = None  # lazy-cached logo


def _icon_image(recording: bool, elapsed: int) -> Image.Image:
    """Compose the tray icon: logo + red dot/timer overlay while recording.

    The static logo is drawn once and cached; the recording overlay is
    composited on top each refresh.
    """
    global _LOGO
    try:
        if _LOGO is None:
            _LOGO = _base_icon().resize((64, 64), Image.LANCZOS)
    except Exception:
        _LOGO = None

    if _LOGO is not None:
        img = _LOGO.copy()
    else:
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))

    d = ImageDraw.Draw(img)
    if recording:
        # red dot top-right
        d.ellipse([44, 6, 56, 18], fill=(225, 59, 59, 255),
                  outline=(255, 255, 255, 255), width=1)
        # timer chip bottom
        txt = f"{elapsed // 60:02d}:{elapsed % 60:02d}"
        d.rounded_rectangle([2, 46, 61, 61], radius=6, fill=(20, 20, 24, 235))
        d.text((6, 49), txt, fill=(255, 255, 255, 255))
    return img


def _fmt(s: float) -> str:
    s = int(s)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


class PamtApp:
    def __init__(self) -> None:
        self.recorder = Recorder(MEETINGS_DIR)
        self._icon: pystray.Icon | None = None
        self._stop_evt = threading.Event()

    # -- actions ---------------------------------------------------------

    def toggle(self) -> None:
        if self.recorder.recording:
            self.stop_recording()
        else:
            self.start_recording()

    def start_recording(self) -> None:
        try:
            self.recorder.start()
            log.info("recording started -> %s", self.recorder.output_paths())
        except Exception as e:
            log.exception("start failed")
            self._show_error(f"Could not start recording: {e}")

    def stop_recording(self) -> None:
        paths = self.recorder.stop()
        log.info("recording stopped: %s", paths)
        self._show_error(
            "Saved:\n" + "\n".join(str(p) for p in paths) if paths
            else "Stopped (no files written)"
        )

    def open_folder(self) -> None:
        MEETINGS_DIR.mkdir(parents=True, exist_ok=True)
        self._open_path(MEETINGS_DIR)

    def quit(self) -> None:
        if self.recorder.recording:
            self.recorder.stop()
        log.info("quitting")
        self._stop_evt.set()
        if self._icon:
            self._icon.stop()

    # -- helpers ---------------------------------------------------------

    def _open_path(self, p: Path) -> None:
        if sys.platform == "win32":
            import os
            os.startfile(str(p))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            import subprocess
            subprocess.Popen(["open", str(p)])
        else:
            import subprocess
            for opener in ("xdg-open", "open"):
                try:
                    subprocess.Popen([opener, str(p)])
                    return
                except FileNotFoundError:
                    continue

    def _show_error(self, msg: str) -> None:
        try:
            if sys.platform == "win32":
                import ctypes
                ctypes.windll.user32.MessageBoxW(0, msg, "PAmt", 0)
            elif sys.platform == "darwin":
                import subprocess
                subprocess.Popen(["osascript", "-e",
                                  f'display dialog "{msg.replace(chr(34), chr(92) + chr(34))}"'])
            else:
                import subprocess
                subprocess.Popen(
                    ["zenity", "--error", "--title=PAmt", f"--text={msg}"],
                    stderr=subprocess.DEVNULL,
                )
        except Exception:
            log.warning("could not show message: %s", msg)

    # -- UI loop ---------------------------------------------------------

    def run(self) -> None:
        self._install_hotkey()
        icon = pystray.Icon(
            "pamt",
            _icon_image(False, 0),
            "PAmt - idle (Ctrl+Alt+R to record)",
            pystray.Menu(
                pystray.Menu.Item("Record meeting", self.toggle, default=True),
                pystray.Menu.Item("Open meetings folder", self.open_folder),
                pystray.Menu.SEPARATOR,
                pystray.Menu.Item(f"PAmt v{__version__}", None, enabled=False),
                pystray.Menu.Item("Quit", self.quit),
            ),
        )
        self._icon = icon
        icon.run_detached()
        while not self._stop_evt.is_set():
            elapsed = int(self.recorder.duration)
            icon.icon = _icon_image(self.recorder.recording, elapsed)
            if self.recorder.recording:
                lvl_mic = int(self.recorder.level("mic") * 10)
                lvl_sys = int(self.recorder.level("system") * 10)
                icon.title = (f"PAmt REC {_fmt(self.recorder.duration)}  "
                              f"mic:{lvl_mic} sys:{lvl_sys}")
            else:
                icon.title = "PAmt - idle (Ctrl+Alt+R to record)"
            time.sleep(1)

    def _install_hotkey(self) -> None:
        from pynput import keyboard

        def on_toggle() -> None:
            self.toggle()

        listener = keyboard.GlobalHotKeys(
            {tuple(HOTKEY_MODS) + (HOTKEY_KEY,): on_toggle}  # type: ignore[dict-item]
        )
        listener.daemon = True
        listener.start()
        log.info("hotkey installed: %s+%s", *HOTKEY_MODS, HOTKEY_KEY.upper())

    # -- tray refresh ----------------------------------------------------

    def refresh(self, icon: pystray.Icon | None, item: object) -> None:
        rec = self.recorder
        elapsed = int(rec.duration)
        icon.icon = _icon_image(rec.recording, elapsed)
        if rec.recording:
            lvl_mic = int(rec.level("mic") * 10)
            lvl_sys = int(rec.level("system") * 10)
            icon.title = (f"PAmt REC {_fmt(rec.duration)}  "
                          f"mic:{lvl_mic} sys:{lvl_sys}")
        else:
            icon.title = "PAmt - idle (Ctrl+Alt+R to record)"


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    log.info("pamt v%s starting on %s", __version__, platform.platform())
    PamtApp().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
