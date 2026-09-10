"""PAmt system-tray application.

Run:  pamt
"""

from __future__ import annotations

import logging
import platform
import re
import sys
import threading
import time
from pathlib import Path

from PIL import Image, ImageDraw

from . import __version__
from .audio import Recorder
from . import clean as clean_audio

log = logging.getLogger(__name__)

# Per-OS user-data location, via the platformdirs standard:
#   Windows -> %LOCALAPPDATA%\PAmt\meetings  (C:\Users\<you>\AppData\Local\PAmt)
#   macOS   -> ~/Library/Application Support/PAmt/meetings
#   Linux   -> ~/.local/share/PAmt/meetings   (respects XDG_DATA_HOME)
try:
    from platformdirs import user_data_dir
    _DATA_ROOT = Path(user_data_dir("PAmt"))
except Exception:  # pragma: no cover - platformdirs should always be present
    _DATA_ROOT = Path.home() / ".pamt"
MEETINGS_DIR = _DATA_ROOT / "meetings"
_ASSETS = Path(__file__).resolve().parent / "assets"
_ICON_SRC = _ASSETS / "icon_64.png"

# Global hotkey: Ctrl+Alt+R  (start/stop)
HOTKEY_MODS = ("ctrl", "alt")
HOTKEY_KEY = "r"

APP_NAME = "PAmt"
GITHUB_URL = "https://github.com/ippocra/pamt"

# Recording folders look like 2026-09-09_1835  (YYYY-MM-DD_HHMM).
_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{4}$")

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


def _show_message(title: str, msg: str) -> None:
    """Show a (non-blocking) message box, on any OS, with a console fallback."""
    log.info("%s: %s", title, msg)
    try:
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, msg, title, 0x40)  # MB_ICONINFORMATION
        elif sys.platform == "darwin":
            import subprocess
            esc = msg.replace('"', '\\"')
            subprocess.Popen(["osascript", "-e",
                              f'display dialog "{esc}" with title "{title}"'])
        else:
            import subprocess
            subprocess.Popen(
                ["zenity", "--info", f"--title={title}", f"--text={msg}"],
                stderr=subprocess.DEVNULL,
            )
    except Exception:
        print(f"\n{title}:\n{msg}\n", file=sys.stderr, flush=True)


def _show_error(msg: str) -> None:
    """Show a blocking error box, on any OS, with a console fallback."""
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
        print(f"\nPAmt error:\n{msg}\n", file=sys.stderr, flush=True)


def _recording_dirs(base: Path) -> list[Path]:
    """Timestamped recording subfolders, newest first."""
    if not base.is_dir():
        return []
    dirs = [d for d in base.iterdir()
            if d.is_dir() and _TS_RE.match(d.name)]
    return sorted(dirs, key=lambda d: d.name, reverse=True)


def _latest_recording(base: Path) -> Path | None:
    return _recording_dirs(base)[0] if _recording_dirs(base) else None


class PamtApp:
    def __init__(self) -> None:
        self.recorder = Recorder(MEETINGS_DIR)
        self._icon = None
        self._stop_evt = threading.Event()
        self._mic_choice: tuple[int, str] | None = None      # user-picked mic
        self._system_choice: tuple[int, str] | None = None   # user-picked system
        # Menu items whose labels we update as state changes. pystray
        # MenuItem objects are immutable, so labels are updated by replacing
        # the item in the menu (see _set_item_text), not by writing .text.
        self._record_item = None
        self._latest_item = None

    # -- actions ---------------------------------------------------------

    def toggle(self) -> None:
        if self.recorder.recording:
            self.stop_recording()
        else:
            self.start_recording()

    def start_recording(self) -> None:
        if self.recorder.recording:
            return
        try:
            self.recorder.start(
                mic_device=self._mic_choice,
                system_device=self._system_choice,
            )
            log.info("recording started -> %s", self.recorder.output_paths())
            self._refresh_state()
        except Exception as e:
            log.exception("start failed")
            _show_error(f"Could not start recording:\n{e}")

    def stop_recording(self) -> None:
        if not self.recorder.recording:
            return
        paths = self.recorder.stop()
        log.info("recording stopped: %s", paths)
        self._refresh_state()
        _show_message(
            "PAmt — recording saved",
            "\n".join(str(p) for p in paths) if paths
            else "Stopped (no files written).",
        )

    def open_recordings_folder(self) -> None:
        MEETINGS_DIR.mkdir(parents=True, exist_ok=True)
        self._open_path(MEETINGS_DIR)

    def open_latest_recording(self) -> None:
        latest = _latest_recording(MEETINGS_DIR)
        if latest is None:
            _show_message("PAmt", f"No recordings yet.\nRecordings save to:\n{MEETINGS_DIR}")
            return
        self._open_path(latest)

    def open_latest_mic(self) -> None:
        self._open_latest_file("mic")

    def open_latest_system(self) -> None:
        self._open_latest_file("system")

    # -- clean audio (Desert Ant Labs Clear) ----------------------------

    def clean_audio_latest(self) -> None:
        """Run Clear on the latest recording's tracks (mic + system).

        The heavy run happens on a worker thread; a progress box is shown
        first and a result box when it finishes. If node / the SDK is not
        set up, the user gets the actionable setup hint instead of a crash.
        """
        latest = _latest_recording(MEETINGS_DIR)
        if latest is None:
            _show_message("PAmt", "No recordings yet.")
            return
        st = clean_audio.status()
        if not st["available"]:
            _show_error(st["hint"])
            return

        preset = clean_audio.DEFAULT_PRESET

        def _work() -> None:
            try:
                results = clean_audio.clean_meeting(latest, preset=preset)
                lines = []
                for r in results:
                    out = r.output_path or "(no output)"
                    sec = f"{r.processing_sec:.1f}s" if r.processing_sec else "?"
                    lines.append(f"{Path(r.input_path).name}\n  -> {out}\n     "
                                 f"({r.duration_sec:.0f}s cleaned in {sec})"
                                 if r.duration_sec else f"{Path(r.input_path).name}\n  -> {out}")
                msg = (f"Cleaned with Clear ({preset} preset).\n\n"
                       + "\n\n".join(lines)
                       + "\n\nOriginals are untouched.")
                _show_message(f"{APP_NAME} — clean audio done", msg)
            except Exception as e:
                log.exception("clean audio failed")
                _show_error(f"Clean audio failed:\n{e}")

        # Progress box is non-blocking on some OSes; start the work immediately
        # and let the result box (above) tell the user it's done.
        _show_message(
            f"{APP_NAME} — cleaning audio…",
            f"Running Clear on the latest recording ({latest.name})…\n"
            f"First run downloads the model (~24 MB) and is slower.\n\n"
            f"You can keep using the meeting; a result box appears when done.",
        )
        threading.Thread(target=_work, name="clean-audio", daemon=True).start()

    def clean_audio_status(self) -> None:
        """Show whether clean audio is usable and what to do if not."""
        st = clean_audio.status()
        if st["available"]:
            _show_message(
                f"{APP_NAME} — clean audio",
                f"Ready. node: {st['node']}\n"
                "Use 'Clean audio (latest)' from the tray.",
            )
        else:
            _show_error(st["hint"])

    def _open_latest_file(self, kind: str) -> None:
        latest = _latest_recording(MEETINGS_DIR)
        if latest is None:
            _show_message("PAmt", "No recordings yet.")
            return
        f = latest / f"{latest.name}_{kind}.wav"
        if not f.exists():
            _show_message("PAmt", f"No '{kind}' track in the latest recording.\n{latest.name}")
            return
        self._open_path(f)

    def choose_devices(self) -> None:
        """Open a dialog to explicitly pick the mic and system-audio devices.

        Lets you override the auto-detected devices -- useful when the app
        picks a weak default (e.g. "Microsoft Sound Mapper") and you want a
        better mic or a specific Stereo Mix / VB-Cable loopback device.
        """
        try:
            import tkinter as tk
            from tkinter import ttk
            from .audio import list_input_devices
        except Exception as e:
            _show_error(f"Device picker unavailable (no tkinter?):\n{e}")
            return
        devices = list_input_devices()
        if not devices:
            _show_error("No audio input devices found.")
            return

        root = tk.Tk()
        root.title(f"{APP_NAME} — Choose audio devices")
        root.resizable(False, False)

        def _label(txt: str) -> None:
            ttk.Label(root, text=txt, font=("Segoe UI", 10, "bold")).grid(
                row=0, column=0, columnspan=2, sticky="w", padx=10, pady=(8, 2))

        _label("Microphone (what I say)")
        mic_var = tk.StringVar(value=self._mic_choice[1] if self._mic_choice else "")
        mic_box = ttk.Combobox(root, textvariable=mic_var, width=46,
                               values=[n for _, n in devices])
        mic_box.grid(row=1, column=0, columnspan=2, padx=10, pady=2)

        ttk.Label(root, text="System audio (the meeting)",
                  font=("Segoe UI", 10, "bold")).grid(
            row=2, column=0, columnspan=2, sticky="w", padx=10, pady=(12, 2))
        sys_var = tk.StringVar(value=self._system_choice[1] if self._system_choice else "")
        sys_box = ttk.Combobox(root, textvariable=sys_var, width=46,
                               values=["(none — mic only)"] + [n for _, n in devices])
        sys_box.grid(row=3, column=0, columnspan=2, padx=10, pady=2)

        ttk.Label(root, text="Tip: for the clearest system audio on Windows, "
                             "install VB-Cable and select it above. "
                             "Route your meeting output to the VB-Cable.",
                  foreground="#666", wraplength=330).grid(
            row=4, column=0, columnspan=2, padx=10, pady=4)

        def _save() -> None:
            def _pick(devs: list[tuple[int, str]], val: str) -> tuple[int, str] | None:
                val = val.strip()
                for i, n in devs:
                    if n == val:
                        return (i, n)
                return None
            self._mic_choice = _pick(devices, mic_var.get()) or (devices[0][0], devices[0][1])
            sv = sys_var.get().strip()
            self._system_choice = _pick(devices, sv) if sv and sv != "(none — mic only)" else None
            root.destroy()
            _show_message(
                f"{APP_NAME} — devices",
                f"Microphone: {self._mic_choice[1] if self._mic_choice else '(auto)'}\n"
                f"System: {self._system_choice[1] if self._system_choice else '(auto/none)'}",
            )

        btns = ttk.Frame(root); btns.grid(row=5, column=0, columnspan=2, pady=10)
        ttk.Button(btns, text="Save", command=_save).pack(side="left", padx=6)
        ttk.Button(btns, text="Cancel", command=root.destroy).pack(side="left", padx=6)
        root.mainloop()

    def about(self) -> None:
        """Show the About box (name, version, repo link) and open the repo."""
        self._show_about()

    def _show_about(self) -> None:
        text = (
            f"{APP_NAME}  v{__version__}\n"
            "Private Annotator Meeting Transcriber\n\n"
            "Clean audio: Powered by Desert Ant Labs (Clear)\n"
            "https://desertant.com\n\n"
            f"Source: {GITHUB_URL}\n"
        )
        _show_message(f"{APP_NAME} — About", text)
        # Also open the repo in the default browser (best-effort).
        try:
            import webbrowser
            webbrowser.open(GITHUB_URL)
        except Exception:
            log.exception("could not open browser")

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

    def _set_item_text(self, item: "pystray.MenuItem", text: str) -> None:
        """Update a menu item's label.

        pystray MenuItem objects are immutable (``text`` is a read-only
        property), so the label is changed by replacing the item with a new
        one carrying the same action/flags, then redrawing the menu. The
        tracked reference (``self._record_item`` / ``self._latest_item``) is
        updated to the new item so subsequent refreshes keep finding it.
        """
        if item is None or self._icon is None:
            return
        import pystray
        menu = self._icon.menu
        if not isinstance(menu, pystray.Menu):
            return
        new = pystray.MenuItem(text, item._action,  # noqa: SLF001
                               checked=None, radio=False,
                               default=item.default, visible=item.visible)
        for i, it in enumerate(menu.items):
            if it is item:
                items = list(menu.items)
                items[i] = new
                menu._items = tuple(items)  # noqa: SLF001
                break
        self._icon.update_menu()
        if self._record_item is item:
            self._record_item = new
        if self._latest_item is item:
            self._latest_item = new

    def _refresh_state(self) -> None:
        """Update the live menu labels after a state change (best-effort)."""
        try:
            if self._record_item is not None and self._icon is not None:
                if self.recorder.recording:
                    self._set_item_text(
                        self._record_item,
                        f"⏹ Stop recording  ({_fmt(self.recorder.duration)})")
                else:
                    self._set_item_text(self._record_item, "⏺ Start recording")
            self._refresh_latest_label()
        except Exception:
            log.exception("menu refresh failed")

    def _refresh_latest_label(self) -> None:
        if self._latest_item is None:
            return
        latest = _latest_recording(MEETINGS_DIR)
        self._set_item_text(
            self._latest_item,
            f"Open latest recording  ({latest.name})" if latest
            else "Open latest recording  (none yet)")

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

        record_item = pystray.MenuItem(
            "⏺ Start recording", self.toggle, default=True,
        )
        latest_item = pystray.MenuItem("Open latest recording  (none yet)",
                                       self.open_latest_recording)
        self._record_item = record_item
        self._latest_item = latest_item
        self._refresh_latest_label()

        icon = pystray.Icon(
            "pamt",
            _icon_image(False, 0),
            "PAmt - idle (Ctrl+Alt+R to record)",
            pystray.Menu(
                record_item,
                pystray.Menu.SEPARATOR,
                latest_item,
                pystray.MenuItem("  ↳ mic track", self.open_latest_mic),
                pystray.MenuItem("  ↳ system track", self.open_latest_system),
                pystray.MenuItem("  ✨ Clean audio (latest)…", self.clean_audio_latest),
                pystray.MenuItem("  ⚙ Clean audio status", self.clean_audio_status),
                pystray.MenuItem("Open recordings folder", self.open_recordings_folder),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("⚙ Choose audio devices…", self.choose_devices),
                pystray.MenuItem("About PAmt", self.about),
                pystray.MenuItem(f"v{__version__}  ·  hotkey Ctrl+Alt+R", None, enabled=False),
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
                if self._record_item is not None:
                    self._set_item_text(
                        self._record_item,
                        f"⏹ Stop recording  ({_fmt(rec.duration)})")
            else:
                icon.title = "PAmt - idle (Ctrl+Alt+R to record)"
                if self._record_item is not None:
                    self._set_item_text(self._record_item, "⏺ Start recording")
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
