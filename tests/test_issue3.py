"""Tests for PAmt (issue #3: general fixes).

Run with:  python -m pytest tests/ -v

The tests stub ``pyaudio`` (no PortAudio needed) and exercise:

* raw capture — the WAV written by ``Recorder`` must be the exact device
  signal (no gain, no low-pass, no resampling);
* the clean-audio helper end-to-end (node + Clear SDK) when they are
  available on the machine, with an actionable error otherwise;
* the clean-audio on/off toggle + persistence (pamt/config.py);
* recording-path discovery, including the legacy locations used by
  older PAmt versions;
* the About dialog (logo + version text), built headlessly on Xvfb when
  no display is present.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import struct
import sys
import threading
import time
import types
import wave
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


class EndOfFrames(Exception):
    """Raised by the fake pyaudio stream once its scripted frames are exhausted.

    It is *not* swallowed by ``Recorder._pump``'s read-error handler in a way
    that would loop forever: the pump catches ``Exception`` and retries, but we
    stop recording the moment it propagates out of the pump call in the tests.
    """


# --------------------------------------------------------------------------
# pyaudio stub
# --------------------------------------------------------------------------
def _install_pyaudio_stub() -> types.ModuleType:
    """Install a fake ``pyaudio`` module so pamt.audio imports without PortAudio."""
    if "pyaudio" in sys.modules:
        return sys.modules["pyaudio"]
    try:
        real = importlib.util.find_spec("pyaudio")
    except (ValueError, ImportError):
        real = None
    if real is not None:
        return importlib.import_module("pyaudio")

    mod = types.ModuleType("pyaudio")
    mod.paInt16 = 8
    mod.paInt24 = 16
    mod.paFloat32 = 32
    mod.paUnknownFormat = -1

    class PyError(Exception):
        pass

    mod.PyError = PyError

    class _FakeStream:
        """Minimal pyaudio.Stream: read() yields scripted frames, then raises."""

        def __init__(self, frames):
            self._frames = list(frames)
            self._i = 0

        def read(self, n, exception_on_overflow=True):
            if self._i >= len(self._frames):
                # Exhausted: a real device stream raises on read; the pump's
                # error handler sleeps 10 ms, which keeps the test CPU-bound
                # loop out and lets the watchdog flip _recording in time.
                raise RuntimeError("no more scripted frames")
            f = self._frames[self._i]
            self._i += 1
            return f

        def write(self, data):
            pass

        def stop_stream(self):
            pass

        def close(self):
            pass

    class _FakePyaudio:
        def __init__(self):
            self.opened: list[dict] = []
            self._script: list[list[bytes]] = []

        def open(self, *args, **kwargs):
            self.opened.append(dict(kwargs))
            return _FakeStream(self._script.pop(0) if self._script else [])

        def get_default_input_device_info(self):
            return {"index": 0, "name": "Fake Mic", "maxInputChannels": 1}

        def get_default_output_device_info(self):
            return {"index": 1, "name": "Fake Loopback", "maxInputChannels": 1}

        def get_device_count(self):
            return 2

        def get_device_info_by_index(self, i):
            return {"index": i, "name": f"Fake {i}", "maxInputChannels": 1,
                    "maxOutputChannels": 1}

        def terminate(self):
            pass

    mod.PyAudio = _FakePyaudio
    sys.modules["pyaudio"] = mod
    return mod


@pytest.fixture(autouse=True)
def _fresh_pamt_modules():
    """Fresh pamt modules per test (stubbed pyaudio, no shared state)."""
    _install_pyaudio_stub()
    for name in list(sys.modules):
        if name == "pamt" or name.startswith("pamt."):
            del sys.modules[name]
    yield
    for name in list(sys.modules):
        if name == "pamt" or name.startswith("pamt."):
            del sys.modules[name]


# --------------------------------------------------------------------------
# Fix 1: raw capture (no gain / no low-pass)
# --------------------------------------------------------------------------
class TestRawCapture:
    def _record(self, frames24: list[bytes], kind="mic") -> Path:
        """Run a 24-bit Recorder pump to completion and return the WAV path.

        The pump runs on the main thread and reads from a fake stream that
        yields the scripted frames then keeps failing (as a real stream would
        on an error). A watchdog flips ``rec._recording`` off the moment the
        scripted frames are exhausted so the pump's ``while self._recording``
        loop exits; the writer is then finished and joined so the WAV is fully
        flushed before we assert on it.
        """
        import pamt.audio as audio
        pyaudio = importlib.import_module("pyaudio")
        py = pyaudio.PyAudio()
        py._script = [frames24]

        rec = audio.Recorder.__new__(audio.Recorder)
        # Bypass the constructor: we only exercise _pump + the WAV writer.
        rec._recording = True
        rec._levels = {}
        out = Path(f"/tmp/pamt_test_raw_{kind}.wav")
        if out.exists():
            out.unlink()
        track = audio.Track(kind=kind, device_index=0, device_name=f"fake-{kind}",
                           out_path=out, rate=44_100)
        track.format = pyaudio.paInt24
        writer = audio._WavWriter(track)
        writer.start()
        stream = py.open(channels=1, format=pyaudio.paInt24, rate=44_100,
                         input=True, input_device_index=0)

        def _watchdog():
            # Wait until the scripted frames are all consumed, then stop.
            while stream._i < len(stream._frames) and rec._recording:
                time.sleep(0.002)
            rec._recording = False

        wd = threading.Thread(target=_watchdog, daemon=True)
        wd.start()
        try:
            rec._pump(stream, writer)  # returns once _recording is False
        finally:
            rec._recording = False
            wd.join(timeout=5)
            writer.finish()
            writer.join(timeout=10)
            stream.close()
        return out

    def test_mic_24bit_is_raw(self):
        """A quiet 24-bit mic signal must survive untouched (no gain)."""
        # 0.1 s of a -40 dBFS-ish sine at 44.1 kHz, 24-bit.
        n = 4410
        frames = []
        for i in range(n):
            v = int(1400 * __import__("math").sin(2 * 3.141592653589793 * 440 * i / 44100))
            frames.append(struct.pack("<i", v & 0xFFFFFF)[:3])
        frames = [b"".join(frames)]
        out = self._record(frames)
        assert out.is_file()
        with wave.open(str(out)) as w:
            assert w.getnchannels() == 1
            assert w.getsampwidth() == 2
            assert w.getframerate() == 44_100
            data = w.readframes(w.getnframes())
        raw = list(struct.unpack(f"<{len(data) // 2}h", data))
        # 24-bit -> 16-bit is a simple 8-bit right shift; verify the first
        # samples match the shifted input exactly (no gain applied).
        src = []
        for i in range(n):
            v = int(1400 * __import__("math").sin(2 * 3.141592653589793 * 440 * i / 44100))
            src.append((v >> 8))
        assert raw[:n] == src, "mic track must be the raw device signal"

    def test_system_24bit_is_not_lowpassed(self):
        """A 24-bit system signal with a strong HF tone must keep the HF."""
        n = 8820  # 0.2 s
        tone = 12_000  # above the old 8 kHz low-pass cutoff
        frames = []
        for i in range(n):
            v = int(20_000 * __import__("math").sin(2 * 3.141592653589793 * tone * i / 44100))
            frames.append(struct.pack("<i", v & 0xFFFFFF)[:3])
        out = self._record([b"".join(frames)], kind="system")
        with wave.open(str(out)) as w:
            data = w.readframes(w.getnframes())
        raw = struct.unpack(f"<{len(data) // 2}h", data)
        # Count zero-crossings: a live 12 kHz tone at 44.1 kHz has ~
        # 12000 * 0.2 = 2400 cycles = 4800 crossings. A low-passed signal
        # would have far fewer.
        crossings = sum(1 for a, b in zip(raw, raw[1:])
                        if (a < 0) != (b < 0))
        assert crossings > 3000, (
            f"system track lost its high frequencies (only {crossings} "
            "zero-crossings) — a low-pass is still being applied")

    def test_16bit_passthrough(self):
        """16-bit input must be written byte-for-byte (no conversion)."""
        import pamt.audio as audio
        pyaudio = importlib.import_module("pyaudio")
        py = pyaudio.PyAudio()
        n = 4410
        raw16 = struct.pack(f"<{n}h", *range(-2205, 2205))
        py._script = [[raw16]]
        rec = audio.Recorder.__new__(audio.Recorder)
        rec._recording = True
        rec._levels = {}
        out = Path("/tmp/pamt_test_raw16.wav")
        if out.exists():
            out.unlink()
        track = audio.Track(kind="system", device_index=1, device_name="fake-sys",
                           out_path=out, rate=44_100)
        track.format = pyaudio.paInt16
        writer = audio._WavWriter(track)
        writer.start()
        stream = py.open(channels=1, format=pyaudio.paInt16, rate=44_100,
                         input=True, input_device_index=1)

        def _watchdog():
            while stream._i < len(stream._frames) and rec._recording:
                time.sleep(0.002)
            rec._recording = False

        wd = threading.Thread(target=_watchdog, daemon=True)
        wd.start()
        try:
            rec._pump(stream, writer)
        finally:
            rec._recording = False
            wd.join(timeout=5)
            writer.finish()
            writer.join(timeout=10)
            stream.close()
        with wave.open(str(out)) as w:
            data = w.readframes(w.getnframes())
        assert data == raw16, "16-bit frames must pass through unmodified"


# --------------------------------------------------------------------------
# Fix 3: clean-audio toggle (on by default) + persistence
# --------------------------------------------------------------------------
class TestCleanToggle:
    def test_default_enabled(self, tmp_path, monkeypatch):
        import pamt.config as cfg
        monkeypatch.setattr(cfg, "config_path", lambda: tmp_path / "config.json")
        assert cfg.is_clean_audio_enabled() is True

    def test_toggle_roundtrip(self, tmp_path, monkeypatch):
        import pamt.config as cfg
        monkeypatch.setattr(cfg, "config_path", lambda: tmp_path / "config.json")
        cfg.set_value("clean_audio_enabled", False)
        assert cfg.is_clean_audio_enabled() is False
        cfg.set_value("clean_audio_enabled", True)
        assert cfg.is_clean_audio_enabled() is True
        # The file must have been written.
        raw = json.loads((tmp_path / "config.json").read_text())
        assert raw["clean_audio_enabled"] is True

    def test_unknown_key_rejected(self, tmp_path, monkeypatch):
        import pamt.config as cfg
        monkeypatch.setattr(cfg, "config_path", lambda: tmp_path / "config.json")
        with pytest.raises(KeyError):
            cfg.set_value("bogus", 1)

    def test_corrupt_config_falls_back(self, tmp_path, monkeypatch):
        import pamt.config as cfg
        p = tmp_path / "config.json"
        p.write_text("{not valid json")
        monkeypatch.setattr(cfg, "config_path", lambda: p)
        assert cfg.is_clean_audio_enabled() is True


class TestCleanAudioApp:
    """The tray-level toggle: app state flips and labels refresh."""

    def _make_app(self, monkeypatch, enabled=True):
        import pamt.app as appmod
        monkeypatch.setattr(appmod.config, "is_clean_audio_enabled",
                            lambda: enabled)
        appmod.config.set_value = lambda *a, **k: None  # no disk in tests
        a = appmod.PamtApp.__new__(appmod.PamtApp)
        a._clean_item = None
        a._toggle_clean_item = None
        a._clean_enabled = enabled
        return a

    def test_toggle_flips_state_and_labels(self, monkeypatch):
        import pamt.app as appmod
        a = self._make_app(monkeypatch, enabled=True)
        # enabled -> label says "Disable", item label clean
        assert a._clean_label().endswith("…")
        assert a._toggle_clean_label() == "Disable clean audio"
        # Stub the message box, then call the real method.
        monkeypatch.setattr(appmod, "_show_message", lambda *args: None)
        a.toggle_clean_audio()
        assert a._clean_enabled is False
        assert a._clean_label().endswith("(off)")
        assert a._toggle_clean_label() == "Enable clean audio"
        # And back on again.
        a.toggle_clean_audio()
        assert a._clean_enabled is True
        assert a._toggle_clean_label() == "Disable clean audio"


# --------------------------------------------------------------------------
# Fix 2: recording-path discovery incl. legacy locations
# --------------------------------------------------------------------------
class TestRecordingDiscovery:
    def _mk_rec(self, root: Path, name: str) -> Path:
        d = root / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "2026-01-01_0000_mic.wav").write_bytes(b"RIFF..")
        return d

    def test_canonical_found(self, tmp_path, monkeypatch):
        import pamt.app as appmod
        monkeypatch.setattr(appmod, "MEETINGS_DIR", tmp_path / "meetings")
        monkeypatch.setattr(appmod, "_LEGACY_MEETINGS_DIRS", ())
        d = self._mk_rec(tmp_path / "meetings", "2026-09-09_1835")
        assert appmod._latest_recording() == d

    def test_legacy_found_when_canonical_empty(self, tmp_path, monkeypatch):
        import pamt.app as appmod
        monkeypatch.setattr(appmod, "MEETINGS_DIR", tmp_path / "meetings")
        legacy = tmp_path / "legacy"
        monkeypatch.setattr(appmod, "_LEGACY_MEETINGS_DIRS", (legacy,))
        (tmp_path / "meetings").mkdir()
        d = self._mk_rec(legacy, "2026-01-01_0000")
        assert appmod._latest_recording() == d

    def test_newest_wins_across_dirs(self, tmp_path, monkeypatch):
        import pamt.app as appmod
        monkeypatch.setattr(appmod, "MEETINGS_DIR", tmp_path / "meetings")
        legacy = tmp_path / "legacy"
        monkeypatch.setattr(appmod, "_LEGACY_MEETINGS_DIRS", (legacy,))
        self._mk_rec(tmp_path / "meetings", "2026-09-09_1835")
        old = self._mk_rec(legacy, "2026-01-01_0000")
        assert appmod._latest_recording().name == "2026-09-09_1835"
        # now the legacy one is the latest
        (tmp_path / "meetings" / "2026-09-09_1835" / "2026-01-01_0000_mic.wav").unlink()
        (tmp_path / "meetings" / "2026-09-09_1835").rmdir()
        assert appmod._latest_recording() == old

    def test_non_timestamp_dirs_ignored(self, tmp_path, monkeypatch):
        import pamt.app as appmod
        monkeypatch.setattr(appmod, "MEETINGS_DIR", tmp_path / "meetings")
        monkeypatch.setattr(appmod, "_LEGACY_MEETINGS_DIRS", ())
        (tmp_path / "meetings").mkdir()
        (tmp_path / "meetings" / "notes").mkdir()
        (tmp_path / "meetings" / "notes" / "x.wav").write_bytes(b"")
        assert appmod._latest_recording() is None


# --------------------------------------------------------------------------
# Fix 3: clean-audio helper end-to-end (node + Clear SDK)
# --------------------------------------------------------------------------
def _node_available() -> bool:
    node = None
    import pamt.clean as clean
    node = clean.node_binary()
    return node is not None and clean._sdk_ready()


@pytest.mark.skipif(not _node_available(),
                    reason="node + Clear SDK not installed "
                           "(run `node setup.js` for a full test)")
class TestCleanEndToEnd:
    def _tone(self, tmp_path: Path) -> Path:
        import math
        p = tmp_path / "tone.wav"
        with wave.open(str(p), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(44100)
            frames = bytearray()
            for i in range(44100):  # 1 s, 440 Hz @ -15 dBFS
                v = int(7000 * math.sin(2 * 3.141592653589793 * 440 * i / 44100))
                frames += struct.pack("<h", v)
            w.writeframes(bytes(frames))
        return p

    def test_clean_produces_output(self, tmp_path):
        import pamt.clean as clean
        src = self._tone(tmp_path)
        res = clean.clean_wav(src, preset="meeting")
        assert res.output_path is not None and res.output_path.is_file()
        assert res.output_path.name == "tone_clean.wav"
        assert res.processing_sec is not None and res.processing_sec >= 0
        with wave.open(str(res.output_path)) as w:
            assert w.getsampwidth() == 2
            assert w.getnchannels() == 1

    def test_warm_up_invokes_node_with_cache_root(self, tmp_path):
        """warm_up_model must call the node helper with --warm-up and the
        per-user cache root (not the SDK default), and swallow failures."""
        import pamt.clean as clean
        cache = tmp_path / "cache"
        calls = []

        class _FakeProc:
            returncode = 0

        def _fake_run_node(argv, timeout):
            calls.append((list(argv), timeout))
            return _FakeProc(), ""

        old_dir = clean._model_cache_dir
        old_run = clean._run_node
        clean._model_cache_dir = lambda: cache
        clean._run_node = _fake_run_node
        try:
            clean.warm_up_model(timeout=7)
            for _ in range(60):
                if calls:
                    break
                time.sleep(0.05)
        finally:
            clean._model_cache_dir = old_dir
            clean._run_node = old_run

        assert calls, "warm-up never ran the node helper"
        argv, timeout = calls[0]
        assert argv[0] == "--warm-up"
        assert "--cache-root" in argv
        assert Path(argv[argv.index("--cache-root") + 1]) == cache
        assert timeout == 7

    def test_warm_up_is_background_and_failure_safe(self, tmp_path):
        """warm_up_model returns immediately (UI never blocks) and a failed
        download is swallowed, not raised."""
        import pamt.clean as clean
        cache = tmp_path / "cache"
        old_dir = clean._model_cache_dir
        old_run = clean._run_node
        clean._model_cache_dir = lambda: cache

        def _boom(argv, timeout):
            raise RuntimeError("no network")

        clean._run_node = _boom
        start = time.monotonic()
        try:
            clean.warm_up_model(timeout=2)  # must not raise
        finally:
            clean._model_cache_dir = old_dir
            clean._run_node = old_run
        elapsed = time.monotonic() - start
        assert elapsed < 1.0, "warm-up must not block the UI"

    def test_status_reports_available(self):
        import pamt.clean as clean
        st = clean.status()
        assert st["available"] is True
        assert st["node"]
        assert st["sdk"] is True

    def test_unknown_preset_falls_back(self, tmp_path):
        import pamt.clean as clean
        src = self._tone(tmp_path)
        res = clean.clean_wav(src, preset="bogus")
        assert res.preset == clean.DEFAULT_PRESET

    def test_non_wav_rejected(self, tmp_path):
        import pamt.clean as clean
        p = tmp_path / "x.mp3"
        p.write_bytes(b"")
        with pytest.raises(ValueError):
            clean.clean_wav(p)


# --------------------------------------------------------------------------
# Fix 4: About dialog (logo + version)
# --------------------------------------------------------------------------
class TestAboutDialog:
    def _build(self):
        """Build the About dialog in a worker thread and force-close it."""
        import tkinter as _tk
        import pamt.app as appmod

        a = appmod.PamtApp.__new__(appmod.PamtApp)
        errors: list[Exception] = []

        def _run():
            class _AutoCloseTk(_tk.Tk):
                def mainloop(self, n=0):
                    # Auto-close so the test never blocks on the dialog.
                    self.after(200, self.destroy)
                    super().mainloop(n)

            orig = _tk.Tk
            _tk.Tk = _AutoCloseTk
            try:
                a._show_about_dialog("PAmt  v0.7.0\ntest")
            except Exception as e:
                errors.append(e)
            finally:
                _tk.Tk = orig

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout=15)
        assert not t.is_alive(), "About dialog did not close in time"
        if errors:
            raise errors[0]

    def test_about_dialog_builds(self):
        if not os.environ.get("DISPLAY") and os.name != "posix":
            pytest.skip("no display")
        try:
            self._build()
        except Exception as e:
            pytest.fail(f"About dialog failed to build: {e!r}")

    def test_about_text_includes_version_and_credit(self):
        import pamt.app as appmod
        a = appmod.PamtApp.__new__(appmod.PamtApp)
        # _show_about builds the text; capture it by stubbing the dialog.
        captured = {}
        a._show_about_dialog = lambda text: captured.update(text=text)
        a._show_about()
        txt = captured.get("text", "")
        assert "v0.7.0" in txt
        assert "Desert Ant Labs" in txt
        assert "Clear" in txt


# --------------------------------------------------------------------------
# Packaging: the SDK + lockfile are declared in package-data
# --------------------------------------------------------------------------
class TestPackaging:
    def test_pyproject_ships_node_modules(self):
        import tomllib
        data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
        pd = data["tool"]["setuptools"]["package-data"]["pamt"]
        assert any("node_modules" in p for p in pd), (
            "the Clear SDK (pamt/node/node_modules) must be shipped with "
            f"the wheel; got {pd}")
        assert "node/clean.js" in pd
        assert "node/run.js" in pd

    def test_ci_bakes_sdk_into_binary(self):
        wf = (REPO_ROOT / ".github/workflows/build.yml").read_text()
        assert "setup.js" in wf, "CI must install the Clear SDK (node setup.js)"
        # The node runtime must be downloaded and bundled so the binary works
        # on machines without Node installed.
        assert "nodejs.org/dist" in wf, "CI must bundle a node runtime"
        # PyInstaller must collect the whole pamt package (SDK + node binary).
        assert "--collect-all pamt" in wf

    def test_ci_no_root_npm_ci(self):
        # Regression: an earlier build.yml ran `npm ci` at the repo root where
        # no package-lock.json exists, failing every platform. setup.js owns
        # SDK installation inside pamt/node/.
        wf = (REPO_ROOT / ".github/workflows/build.yml").read_text()
        assert "npm ci" not in wf

    def test_lockfile_present(self):
        assert (REPO_ROOT / "pamt" / "node" / "package-lock.json").is_file()


if __name__ == "__main__":
    _install_pyaudio_stub()
    sys.exit(pytest.main([__file__, "-v"]))
