"""Audio capture for PAmt.

Two independent tracks are recorded concurrently:

* **mic**      -- the user's microphone (what *I* say)
* **system**   -- what the meeting plays through the machine (the *other*
  people, plus any local notification sounds).

Each track is written as a 16-bit mono WAV at the device's *native* sample
rate. Different devices support different rates (WASAPI on Windows is
especially picky), so we open each stream at the rate the device actually
accepts rather than forcing one global rate. Whisper resamples at
transcription time, so the per-track rates need not match.

Device hints:
    Linux   -- system audio: a Pulse/PipeWire "Monitor" source
    macOS   -- system audio: a virtual cable (BlackHole) or loopback device
    Windows -- system audio: "Stereo Mix" or a VB-Cable virtual device
"""

from __future__ import annotations

import logging
import struct
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path

import pyaudio

log = logging.getLogger(__name__)

CHANNELS = 1
SAMPLE_WIDTH = 2  # 16-bit
CHUNK = 1024
# Fallback sample rate only if a device advertises nothing usable.
DEFAULT_RATE = 44_100
# Rates to try (most common first) when we can't open at the native rate.
_FALLBACK_RATES = (48_000, 44_100, 16_000)

# Keywords that identify a system-output (loopback) capture device.
_SYSTEM_HINTS = (
    "monitor",
    "stereo mix",
    "st\u00e9reo mix",
    "what u hear",
    "what-u-hear",
    "virtual",
    "cable",
    "blackhole",
    "vb-audio",
    "loopback",
)
# Keywords that identify a microphone.
_MIC_HINTS = (
    "mic",
    "microphone",
    "headset",
    "usb audio",
    "array",
    "reclass",
)


@dataclass
class Track:
    """A single recording target: one device, one WAV file."""

    kind: str            # "mic" or "system"
    device_index: int
    device_name: str
    out_path: Path
    rate: int = DEFAULT_RATE


def list_input_devices() -> list[tuple[int, str]]:
    """Return [(index, name), ...] for all input-capable devices."""
    pa = pyaudio.PyAudio()
    try:
        out: list[tuple[int, str]] = []
        for i in range(pa.get_device_count()):
            info = pa.get_device_info_by_index(i)
            if int(info["maxInputChannels"]) > 0:
                out.append((int(info["index"]), str(info["name"])))
        return out
    finally:
        pa.terminate()


def _matches(name: str, hints: tuple[str, ...]) -> bool:
    n = name.lower()
    return any(h in n for h in hints)


def guess_devices() -> tuple[tuple[int, str] | None, tuple[int, str] | None]:
    """Best-effort pick of (mic, system) as (index, name) tuples.

    Either element may be None when no plausible candidate exists.
    """
    devices = list_input_devices()
    mic: tuple[int, str] | None = None
    system: tuple[int, str] | None = None
    for idx, name in devices:
        if _matches(name, _SYSTEM_HINTS):
            system = (idx, name)
        elif _matches(name, _MIC_HINTS) and mic is None:
            mic = (idx, name)
    if mic is None and devices:
        mic = devices[0]  # fallback: first input device
    return mic, system


def _rms(data: bytes) -> float:
    """RMS of 16-bit PCM, scaled to ~0..1."""
    n = len(data) // 2
    if n == 0:
        return 0.0
    samples = struct.unpack(f"<{n}h", data[: n * 2])
    return min(1.0, ((sum(s * s for s in samples) / n) ** 0.5) / 8000.0)


class _WavWriter(threading.Thread):
    """Drains a queue of PCM frames into a WAV file at the track's rate."""

    def __init__(self, track: Track):
        super().__init__(daemon=True, name=f"wav-{track.kind}")
        self.track = track
        self._q: list[bytes] = []
        self._lock = threading.Lock()
        self._finished = threading.Event()  # (not named _stop: that shadows Thread._stop)

    def push(self, data: bytes) -> None:
        with self._lock:
            self._q.append(data)

    def finish(self) -> None:
        """Signal the writer to flush and exit."""
        self._finished.set()

    def run(self) -> None:
        self.track.out_path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(self.track.out_path), "wb") as wf:
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(SAMPLE_WIDTH)
            wf.setframerate(self.track.rate)
            while True:
                with self._lock:
                    if self._q:
                        frames = b"".join(self._q)
                        self._q.clear()
                    else:
                        frames = b""
                if frames:
                    wf.writeframes(frames)
                else:
                    if self._finished.is_set():
                        break
                    time.sleep(0.005)
        log.info("wrote %s (%d Hz)", self.track.out_path, self.track.rate)


class Recorder:
    """Records mic + system audio in parallel to two WAV files."""

    def __init__(self, out_dir: Path):
        self.out_dir = Path(out_dir)
        self._pa: pyaudio.PyAudio | None = None
        self._streams: list[pyaudio.Stream] = []
        self._writers: list[_WavWriter] = []
        self._pumps: list[threading.Thread] = []
        self._recording = False
        self._started_at = 0.0
        self._levels: dict[str, float] = {}
        self._last_paths: list[Path] = []

    # -- lifecycle -------------------------------------------------------

    def _open_stream(self, track: Track) -> pyaudio.Stream:
        """Open an input stream for *track* at a rate the device accepts."""
        pa = self._pa
        assert pa is not None
        info = pa.get_device_info_by_index(track.device_index)
        native = int(info.get("defaultSampleRate", 0) or 0)
        # Try the native rate first (skip it if it's 0/invalid), then fall
        # back to common rates. First one that opens wins.
        rates: list[int] = []
        if native and 3_000 < native < 200_000:
            rates.append(native)
        rates.extend(r for r in _FALLBACK_RATES if r not in rates)
        last_err: Exception | None = None
        for rate in rates:
            try:
                stream = pa.open(
                    format=pyaudio.paInt16,
                    channels=CHANNELS,
                    rate=rate,
                    input=True,
                    input_device_index=track.device_index,
                    frames_per_buffer=CHUNK,
                )
                track.rate = rate
                return stream
            except Exception as e:
                last_err = e
        raise RuntimeError(
            f"could not open '{track.device_name}' at any sample rate: {last_err}"
        )

    def start(self,
              mic_device: tuple[int, str] | None = None,
              system_device: tuple[int, str] | None = None) -> list[Path]:
        if self._recording:
            raise RuntimeError("already recording")

        guessed_mic, guessed_system = guess_devices()
        mic = mic_device or guessed_mic
        system = system_device or guessed_system
        if mic is None:
            raise RuntimeError("no microphone input device found")

        ts = time.strftime("%Y-%m-%d_%H%M")
        self.out_dir.mkdir(parents=True, exist_ok=True)

        self._pa = pyaudio.PyAudio()
        self._recording = True  # lets the pump threads run
        try:
            # The mic is required; the system track is best-effort so that a
            # missing/broken loopback device never kills the whole recording.
            mic_track = Track("mic", mic[0], mic[1], self.out_dir / f"{ts}_mic.wav")
            self._start_track(mic_track, required=True)

            if system is None:
                log.warning(
                    "no system-audio device found -- recording mic only. "
                    "Linux: pick a Monitor source. Windows: enable Stereo Mix "
                    "or install VB-Cable. macOS: install BlackHole."
                )
            else:
                sys_track = Track("system", system[0], system[1],
                                  self.out_dir / f"{ts}_system.wav")
                try:
                    self._start_track(sys_track, required=True)
                except Exception:
                    log.exception(
                        "system-audio device '%s' failed -- continuing with mic only",
                        system[1],
                    )
        except Exception:
            # The mic (or setup) failed: tear down anything we opened.
            self._recording = False
            self.stop()
            raise

        self._started_at = time.time()
        return list(self._last_paths)

    def _start_track(self, track: Track, required: bool) -> None:
        """Open, start, and wire up one recording track."""
        stream = self._open_stream(track)
        writer = _WavWriter(track)
        pump = threading.Thread(
            target=self._pump, args=(stream, writer),
            name=f"pump-{track.kind}", daemon=True,
        )
        stream.start_stream()
        writer.start()
        pump.start()
        self._streams.append(stream)
        self._writers.append(writer)
        self._pumps.append(pump)
        self._last_paths.append(track.out_path)
        log.info("recording %s: %s (%d Hz) -> %s",
                 track.kind, track.device_name, track.rate, track.out_path)

    def stop(self) -> list[Path]:
        if not self._recording:
            # Still clean up any half-opened tracks from a failed start().
            self._teardown()
            return []
        self._recording = False
        paths = self._teardown()
        log.info("stopped recording after %.1fs", self.duration)
        return paths

    def _teardown(self) -> list[Path]:
        for s in self._streams:
            try:
                s.stop_stream()
                s.close()
            except Exception:
                log.exception("error closing stream")
        for p in self._pumps:
            p.join(timeout=2)
        for w in self._writers:
            w.finish()
            w.join(timeout=5)
        if self._pa:
            self._pa.terminate()
        self._streams.clear()
        self._pumps.clear()
        self._writers.clear()
        self._pa = None
        return [p for p in self._last_paths if p.exists()]

    # -- internals -------------------------------------------------------

    def _pump(self, stream: pyaudio.Stream, writer: _WavWriter) -> None:
        while self._recording:
            try:
                data = stream.read(CHUNK, exception_on_overflow=False)
            except Exception:
                log.exception("read error on %s", writer.track.kind)
                time.sleep(0.01)
                continue
            self._levels[writer.track.kind] = _rms(data)
            writer.push(data)

    # -- introspection ---------------------------------------------------

    @property
    def recording(self) -> bool:
        return self._recording

    @property
    def duration(self) -> float:
        return max(0.0, time.time() - self._started_at) if self._recording else 0.0

    def level(self, kind: str) -> float:
        return self._levels.get(kind, 0.0)

    def output_paths(self) -> list[Path]:
        return list(self._last_paths)
