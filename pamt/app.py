"""PAmt system-tray application.

Run:  pamt
"""

from __future__ import annotations

import logging
import platform
import sys
import threading
import time
from pathlib import Path

from PIL import Image, ImageDraw

from . import __version__
from .audio import Recorder

log = logging.getLogger(__name__)

MEETINGS_DIR = Path.home() / "meetings"
_ASSETS = Path(__file__).resolve().parent / "assets"
_ICON_SRC = _ASSETS / "icon_64.png"

# Global hotkey: Ctrl+Alt+R  (start/stop)
HOTKEY_MODS = ("ctrl", "alt")
HOTKEY_KEY = "r"

_LOGO: Image.Image | None = None  # lazy-cached logo


def _base_icon() -> Image.Image:
    """Load the packaged logo, falling back to a drawn glyph if missing."""
    try:
        return Image.open(_ICON_SRC).convert("RGBA")
    except OSError:
        return _fallback_glyph(False, 0)


def _fallback_glyph(recording: bool, elapsed: int) -> Image.Image:
    """A simple drawn mic used only when the logo file is unavailable."""
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    color = (220, 60, 60, 255) if recording else (150, 150, 155, 255)
    d.rounded_rectangle([4, 4, 59, 59], radius=12, fill=(40, 40, 45, 235))
    d.rounded_rectangle([26, 14, 38, 38], radius=6, fill=color)
    d.line([32, 38, 32, 46], fill=color, width=3)
    d.line([24, 46, 40, 46], fill=color, width=3)
    if recording:
        d.ellipse([44, 8, 54, 18], fill=color)
    return img


def _icon_image(recording: bool, elapsed: int) -> Image.Image:
    """Compose the tray icon: logo + red dot/timer overlay while recording."""
    global _LOGO
    if _LOGO is None:
        _LOGO = _base_icon().resize((64, 64), Image.LANCZOS)

    img = _LOGO.copy()
    d = ImageDraw.Draw(img)
    if recording:
        d.ellipse([44, 6, 56, 18], fill=(225, 59, 59, 255),
                  outline=(255, 255, 255, 255), width=1)
        txt = f"{elapsed // 60:02d}:{elapsed % 60:02d}"
        d.rounded_rectangle([2, 46, 61, 61], radius=6, fill=(20, 20, 24, 235))
        d.text((6, 49), txt, fill=(255, 255, 255, 255))
    return img


def _fmt(s: float) -> str:
    s = int(s)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def _show_error(msg: str) -> None:
    """Show a blocking message box, on any OS, with a console fallback."""
    log.error(msg)
    try:
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, msg, "PAmt", 0)
        elif sys.platform == "darwin":
            import subprocess
            esc = msg.replace('"', '\\"')
            subprocess.Popen(["osascript", "-e", f'display dialog "{esc}"'])
        else:
            import subprocess
            subprocess.Popen(
                ["zenity", "--error", "--title=PAmt", f"--text={msg}"],
                stderr=subprocess.DEVNULL,
            )
    except Exception:
        # Last resort: write to stderr so it's visible in a terminal.
        print(f"\nPAmt error:\n{msg}\n", file=sys.stderr, flush=True)


class PamtApp:
    def __init__(self) -> None:
        self.recorder = Recorder(MEETINGS_DIR)
        self._icon = None
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
            _show_error(f"Could not start recording:\n{e}")

    def stop_recording(self) -> None:
        paths = self.recorder.stop()
        log.info("recording stopped: %s", paths)
        _show_error(
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
            try:
                self._icon.stop()
            except Exception:
                pass

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

    # -- UI loop ---------------------------------------------------------

    def run(self) -> None:
        # pystray is imported lazily so a missing tkinter (common on a
        # minimal Windows Python install) produces a clear, actionable
        # error instead of an opaque import failure.
        try:
            import pystray
        except Exception as e:
            _show_error(
                "PAmt needs a Python with tkinter for the system-tray icon.\n\n"
                f"Import failed: {e}\n\n"
                "Fix: reinstall Python from python.org and make sure the\n"
                "'tcl/tk and IDLE' option is ticked, then retry."
            )
            return

        try:
            self._install_hotkey()
        except Exception as e:
            log.warning("hotkey not installed: %s", e)

        icon = pystray.Icon(
            "pamt",
            _icon_image(False, 0),
            "PAmt - idle (Ctrl+Alt+R to record)",
            pystray.Menu(
                pystray.MenuItem("Record meeting", self.toggle, default=True),
                pystray.MenuItem("Open meetings folder", self.open_folder),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(f"PAmt v{__version__}", None, enabled=False),
                pystray.MenuItem("Quit", self.quit),
            ),
        )
        self._icon = icon
        icon.run()  # blocks on the tray event loop until stop()

    def _install_hotkey(self) -> None:
        try:
            from pynput import keyboard
        except Exception as e:
            log.warning("pynput unavailable, no global hotkey: %s", e)
            return

        def on_toggle() -> None:
            self.toggle()

        listener = keyboard.GlobalHotKeys(
            {tuple(HOTKEY_MODS) + (HOTKEY_KEY,): on_toggle}  # type: ignore[dict-item]
        )
        listener.daemon = True
        listener.start()
        log.info("hotkey installed: %s+%s", *HOTKEY_MODS, HOTKEY_KEY.upper())

    def _refresh(self, icon, item) -> None:
        """Tray update callback (pystray calls this on its timer thread)."""
        rec = self.recorder
        elapsed = int(rec.duration)
        try:
            icon.icon = _icon_image(rec.recording, elapsed)
            if rec.recording:
                lvl_mic = int(rec.level("mic") * 10)
                lvl_sys = int(rec.level("system") * 10)
                icon.title = (f"PAmt REC {_fmt(rec.duration)}  "
                              f"mic:{lvl_mic} sys:{lvl_sys}")
            else:
                icon.title = "PAmt - idle (Ctrl+Alt+R to record)"
        except Exception:
            log.exception("tray refresh failed")


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    log.info("pamt v%s starting on %s", __version__, platform.platform())
    PamtApp().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
