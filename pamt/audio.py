"""Audio capture for PAmt.

Two independent tracks are recorded concurrently:

* **mic**      -- the user's microphone (what *I* say)
* **system**   -- what the meeting plays through the machine (the *other*
  people, plus any local notification sounds).

Each track is written as a 16 kHz 16-bit mono WAV so the two files can be
transcribed independently and merged later with per-track speaker labels.

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

SAMPLE_RATE = 16_000
CHANNELS = 1
SAMPLE_WIDTH = 2  # 16-bit
CHUNK = 1024

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
    """Drains a queue of PCM frames into a WAV file."""

    def __init__(self, track: Track):
        super().__init__(daemon=True, name=f"wav-{track.kind}")
        self.track = track
        self._q: list[bytes] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def push(self, data: bytes) -> None:
        with self._lock:
            self._q.append(data)

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        self.track.out_path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(self.track.out_path), "wb") as wf:
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(SAMPLE_WIDTH)
            wf.setframerate(SAMPLE_RATE)
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
                    if self._stop.is_set():
                        break
                    time.sleep(0.005)
        log.info("wrote %s", self.track.out_path)


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
        if system is None:
            raise RuntimeError(
                "No system-audio capture device found. "
                "Linux: pick a Pulse/PipeWire 'Monitor' source. "
                "Windows: enable 'Stereo Mix' in sound settings or "
                "install VB-Cable. "
                "macOS: install BlackHole and select it."
            )

        ts = time.strftime("%Y-%m-%d_%H%M")
        self.out_dir.mkdir(parents=True, exist_ok=True)
        tracks = [
            Track("mic", mic[0], mic[1], self.out_dir / f"{ts}_mic.wav"),
            Track("system", system[0], system[1],
                  self.out_dir / f"{ts}_system.wav"),
        ]

        self._pa = pyaudio.PyAudio()
        for track in tracks:
            stream = self._pa.open(
                format=pyaudio.paInt16,
                channels=CHANNELS,
                rate=SAMPLE_RATE,
                input=True,
                input_device_index=track.device_index,
                frames_per_buffer=CHUNK,
            )
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
            log.info("recording %s: %s -> %s",
                     track.kind, track.device_name, track.out_path)

        self._recording = True
        self._started_at = time.time()
        self._last_paths = [t.out_path for t in tracks]
        return self._last_paths

    def stop(self) -> list[Path]:
        if not self._recording:
            return []
        self._recording = False
        for s in self._streams:
            try:
                s.stop_stream()
                s.close()
            except Exception:
                log.exception("error closing stream")
        for p in self._pumps:
            p.join(timeout=2)
        for w in self._writers:
            w.stop()
            w.join(timeout=5)
        if self._pa:
            self._pa.terminate()
        self._streams.clear()
        self._pumps.clear()
        self._writers.clear()
        self._pa = None
        paths = [p for p in self._last_paths if p.exists()]
        log.info("stopped recording after %.1fs", self.duration)
        return paths

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
