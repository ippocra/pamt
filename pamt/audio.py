"""Audio capture for PAmt.

Two independent tracks are recorded concurrently:

* **mic**      -- the user's microphone (what *I* say)
* **system**   -- what the meeting plays through the machine (the *other*
  people, plus any local notification sounds).

Each track is **captured at 24-bit** (when the device allows it) for the best
possible signal-to-noise and headroom, then written as a **16-bit mono WAV**
at the device's *native* sample rate. 24-bit capture is the source-level fix
for two common problems: laptop mics record very quietly (24-bit gives ~18 dB
more headroom to amplify them *without* clipping), and low-quality loopback
capture carries a harsh high-frequency tail (more bits = lower noise floor).
The files stay 16-bit because Whisper wants 16-bit, so nothing downstream
changes. Different devices support different rates (WASAPI on Windows is
especially picky), so we open each stream at the rate the device actually
accepts rather than forcing one global rate; Whisper resamples at
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
CAPTURE_WIDTH = 3   # 24-bit capture (best SNR / headroom); falls back to 16
OUTPUT_WIDTH = 2    # 16-bit WAV output (Whisper-compatible)
CAPTURE_FORMATS = (pyaudio.paInt24, pyaudio.paInt16)  # try 24-bit first
CHUNK = 1024
# Fallback sample rate only if a device advertises nothing usable.
DEFAULT_RATE = 44_100
# Rates to try (most common first) when we can't open at the native rate.
_FALLBACK_RATES = (48_000, 44_100, 16_000)

# Auto-gain for the *mic* track: laptop mics record very quietly (~ -40 dB
# RMS). We lift it toward a normal speech level (target ~ -18 dB) so
# transcription has something to work with. Because we capture at 24-bit the
# boost has real headroom and won't clip. Values are in dB.
GAIN_TARGET_DB = -18.0       # aim the mic's average level here
GAIN_THRESHOLD_DB = -30.0    # only boost tracks quieter than this
GAIN_CAP_DB = 48.0           # never boost more than this (24-bit headroom)
GAIN_FLOOR_DB = 0.0          # never attenuate a loud track

# Low-pass the *system* track: loopback capture of compressed web audio often
# carries a harsh high-frequency tail (aliasing/harshness above ~4-8 kHz) that
# sounds "fuzzy". Whisper band-limits to 8 kHz anyway, so cutting the HF tail
# cleans the track and removes the fuzz. `state` carries continuity.
LOWPASS_HZ = 8_000

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
    capture_width: int = CAPTURE_WIDTH   # bits/3 -> bytes per sample at capture
    format = pyaudio.paInt24             # PyAudio format actually used


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


def _apply_gain_16(data: bytes, gain_db: float) -> bytes:
    """Multiply 16-bit PCM by a gain (dB), clamping to prevent clipping."""
    if gain_db <= 0:
        return data
    factor = 10.0 ** (gain_db / 20.0)
    n = len(data) // 2
    samples = struct.unpack(f"<{n}h", data[: n * 2])
    out = [max(-32768, min(32767, int(round(s * factor)))) for s in samples]
    return struct.pack(f"<{n}h", *out)


def _lowpass_16(data: bytes, rate: int, cutoff_hz: float,
                state: list[float]) -> bytes:
    """2-pole low-pass (steeper roll-off) for the system track.

    Removes the harsh high-frequency tail (aliasing/harshness). `state` holds
    the two previous filter outputs so the filter is continuous across chunks.
    """
    if cutoff_hz <= 0:
        return data
    r = 2.0 * 3.141592653589793 * cutoff_hz / rate
    alpha = r / (1.0 + r)
    n = len(data) // 2
    samples = struct.unpack(f"<{n}h", data[: n * 2])
    s0 = state[0] if len(state) > 0 else 0.0
    s1 = state[1] if len(state) > 1 else 0.0
    out = []
    for s in samples:
        s0 = s0 + alpha * (s - s0)
        s1 = s1 + alpha * (s0 - s1)
        out.append(int(round(max(-32768, min(32767, s1)))))
    state[0], state[1] = s0, s1
    return struct.pack(f"<{n}h", *out)


# -- 24-bit helpers -------------------------------------------------------

def _unpack_24(data: bytes) -> list[int]:
    """Decode little-endian 24-bit PCM into signed ints (range ~+-8.4M)."""
    n = len(data) // 3
    out = []
    for i in range(n):
        b0 = data[i * 3]
        b1 = data[i * 3 + 1]
        b2 = data[i * 3 + 2]
        v = b0 | (b1 << 8) | (b2 << 16)
        if v >= 0x800000:
            v -= 0x1000000
        out.append(v)
    return out


def _pack_24(samples: list[int]) -> bytes:
    """Encode signed ints back to little-endian 24-bit PCM."""
    out = bytearray()
    for v in samples:
        v &= 0xFFFFFF
        out += bytes((v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF))
    return bytes(out)


def _rms_24(data: bytes) -> float:
    """RMS of 24-bit PCM, scaled to ~0..1."""
    samples = _unpack_24(data)
    n = len(samples)
    if n == 0:
        return 0.0
    return min(1.0, ((sum(s * s for s in samples) / n) ** 0.5) / 8_388_608.0)


def _apply_gain_24(data: bytes, gain_db: float) -> bytes:
    """Multiply 24-bit PCM by a gain (dB), clamped to the 24-bit range."""
    if gain_db <= 0:
        return data
    factor = 10.0 ** (gain_db / 20.0)
    samples = _unpack_24(data)
    return _pack_24([max(-8_388_608, min(8_388_607, int(round(s * factor))))
                     for s in samples])


def _lowpass_24(data: bytes, rate: int, cutoff_hz: float,
                state: list[float]) -> bytes:
    """2-pole low-pass on 24-bit PCM (continuous across chunks via `state`)."""
    if cutoff_hz <= 0:
        return data
    r = 2.0 * 3.141592653589793 * cutoff_hz / rate
    alpha = r / (1.0 + r)
    samples = _unpack_24(data)
    s0 = state[0] if len(state) > 0 else 0.0
    s1 = state[1] if len(state) > 1 else 0.0
    out = []
    for s in samples:
        s0 = s0 + alpha * (s - s0)
        s1 = s1 + alpha * (s0 - s1)
        out.append(int(round(max(-8_388_608, min(8_388_607, s1)))))
    state[0], state[1] = s0, s1
    return _pack_24(out)


def _downmix_24_to_16(data: bytes) -> bytes:
    """Convert 24-bit PCM to 16-bit PCM (shift right 8, clip)."""
    samples = _unpack_24(data)
    return struct.pack(f"<{len(samples)}h",
                       *[max(-32768, min(32767, s >> 8)) for s in samples])


def _downmix_16_to_16(data: bytes) -> bytes:
    """16-bit passthrough (already 16-bit)."""
    return data


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
            wf.setsampwidth(OUTPUT_WIDTH)   # always write 16-bit WAV
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
        """Open an input stream for *track*.

        Tries 24-bit first (best SNR/headroom), then 16-bit, at the device's
        native rate and then common fallbacks. The first combination that
        opens wins, and we record the rate + format actually used.
        """
        pa = self._pa
        assert pa is not None
        info = pa.get_device_info_by_index(track.device_index)
        native = int(info.get("defaultSampleRate", 0) or 0)
        rates: list[int] = []
        if native and 3_000 < native < 200_000:
            rates.append(native)
        rates.extend(r for r in _FALLBACK_RATES if r not in rates)
        last_err: Exception | None = None
        for fmt in CAPTURE_FORMATS:          # 24-bit first, then 16-bit
            for rate in rates:
                try:
                    stream = pa.open(
                        format=fmt,
                        channels=CHANNELS,
                        rate=rate,
                        input=True,
                        input_device_index=track.device_index,
                        frames_per_buffer=CHUNK,
                    )
                    track.rate = rate
                    track.format = fmt
                    track.capture_width = 3 if fmt == pyaudio.paInt24 else 2
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
        bits = 24 if track.format == pyaudio.paInt24 else 16
        log.info("recording %s: %s (%d Hz, %d-bit) -> %s",
                 track.kind, track.device_name, track.rate, bits,
                 track.out_path)

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
        kind = writer.track.kind
        fmt = writer.track.format
        is24 = (fmt == pyaudio.paInt24)
        apply_gain = kind == "mic"
        apply_lp = kind == "system"
        avg: list[float] = []           # recent RMS (linear) for stable mic gain
        lp_state: list[float] = [0.0, 0.0]   # 2-pole low-pass continuity state
        rms_fn = _rms_24 if is24 else _rms
        gain_fn = _apply_gain_24 if is24 else _apply_gain_16
        lp_fn = (_lowpass_24 if is24 else _lowpass_16)
        downmix = _downmix_24_to_16 if is24 else _downmix_16_to_16
        while self._recording:
            try:
                data = stream.read(CHUNK, exception_on_overflow=False)
            except Exception:
                log.exception("read error on %s", kind)
                time.sleep(0.01)
                continue
            rms = rms_fn(data)
            self._levels[kind] = rms
            if apply_gain:
                data = self._gained(data, rms, avg, gain_fn)
            if apply_lp:
                data = lp_fn(data, writer.track.rate, LOWPASS_HZ, lp_state)
            writer.push(downmix(data))   # writer always expects 16-bit

    def _gained(self, data: bytes, rms: float, avg: list[float],
                gain_fn) -> bytes:
        """Amplify quiet mic audio toward a usable level (see GAIN_TARGET_DB).

        Uses a short moving average of RMS so the gain is steady rather than
        "pumping" with the speech envelope. Loud input (above the threshold)
        passes through untouched; we never attenuate.
        """
        avg.append(rms)
        if len(avg) > 8:
            avg.pop(0)
        recent = sum(avg) / len(avg)
        if recent <= 0.0005:            # effectively silent -> no useful level
            return data
        recent_db = 20.0 * (recent ** 0.5) - 20.0  # approx dBFS from 0..1 RMS
        if recent_db >= GAIN_THRESHOLD_DB:
            return data
        gain = min(GAIN_CAP_DB, max(GAIN_FLOOR_DB, GAIN_TARGET_DB - recent_db))
        return gain_fn(data, gain)

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
