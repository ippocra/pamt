# PAmt — Private Annotator Meeting Transcriber

<p align="center">
  <img src="pamt/assets/logo.svg" alt="PAmt logo" width="120" height="120">
</p>

Records online meeting audio (Zoom, Teams, Zoho Meet, any browser-based
call) as **two separate WAV tracks** — your microphone and the meeting's
system audio — so the two can be transcribed independently and merged
into a clean, speaker-labeled transcript.

Everything runs **locally**. No cloud, no accounts, no telemetry.

## Why two tracks?

When you're on a video call, your mic and the people on the other end are
mixed together in your headset. By recording them as two distinct files,
PAmt lets the transcription step label speakers correctly ("me" vs.
"them") instead of producing one undifferentiated blob of speech.

## What it does

- **System-tray app** — a small mic icon lives in the tray; nothing else
  is on screen.
- **Global hotkey** — `Ctrl+Alt+R` toggles recording (start/stop) from
  anywhere, even with the meeting window focused.
- **Dual-track capture** — writes `mic.wav` and `system.wav` into a
  timestamped folder per meeting, in your OS's standard user-data
  location (see *Where recordings are saved*). Captured at **24-bit** for
  the best signal-to-noise, written as 16-bit WAV (transcription-ready).
- **Find your recordings from the tray** — open the latest recording, its
  mic/system tracks, or the whole recordings folder without leaving the
  tray menu.
- **Choose your devices** — right-click → **⚙ Choose audio devices** to
  pick exactly which mic and which system-audio (loopback) device to use,
  instead of relying on auto-detection.
- **Auto-gain + de-fuzz** — the mic track is automatically amplified to a
  usable speech level (no clipping, thanks to 24-bit headroom), and the
  system track's harsh high-frequency tail is filtered out at the source.
- **Clean audio (optional)** — one click runs [Desert Ant Labs' Clear
  model](https://desertant.com/models/clear/) on the latest recording:
  background noise and room reverb pulled down, loudness normalized.
  Runs 100% on your machine; the original files are never touched.
  Opt-in setup — see *Clean audio* below.
- **Live level meter** — the tray tooltip shows a running timer and per-
  track audio levels while recording.
- **Graceful degradation** — if the system-audio (loopback) device is
  missing or broken, PAmt records the mic instead of failing, and tells
  you what to fix.

## Getting the binary (no Python needed)

Pre-built, single-file binaries for Windows / Linux / macOS are published
on every release:

- 🪟 **Windows** → download `pamt-windows.exe`
- 🐧 **Linux** → download `pamt-linux`, then `chmod +x pamt-linux`
- 🍎 **macOS** → download `pamt-macos`, then `chmod +x pamt-macos` (and
  right-click → Open the first time to allow it)

Grab the latest from the [Releases page](https://github.com/ippocra/pamt/releases).

### Running the Windows exe

From PowerShell, in the folder where you saved the exe:

```powershell
.\pamt-windows.exe
```

The mic icon appears in the system tray. Use `Ctrl+Alt+R` to start/stop,
or right-click the tray icon. If the exe exits immediately, run
`.\pamt-windows.exe -x` to see the output.

## Using it

1. Launch PAmt — the mic icon appears in your system tray (look in the
   overflow area, `^`, if you don't see it).
2. Join your meeting.
3. **Start / stop** — either:
   - right-click the tray icon → **⏺ Start recording** (the label flips to
     **⏹ Stop recording** with a live timer while recording), or
   - press the global hotkey **`Ctrl+Alt+R`** from anywhere.

   The tray icon turns red with a live `MM:SS` timer and per-track level
   meter while recording.
4. When the call ends, stop the same way. A dialog lists the exact file
   paths that were saved.
5. **Find your recordings** from the tray menu:
   - **Open latest recording** — opens the most recent meeting's folder.
   - **↳ mic track** / **↳ system track** — opens that track's WAV directly
     in your default audio player.
   - **Open recordings folder** — opens the folder containing *all* meetings.

## Where recordings are saved

PAmt uses each OS's standard user-data location (via `platformdirs`):

| OS      | Path |
|---------|------|
| Windows | `C:\Users\<you>\AppData\Local\PAmt\meetings` |
| macOS   | `~/Library/Application Support/PAmt/meetings` |
| Linux   | `~/.local/share/PAmt/meetings` (respects `XDG_DATA_HOME`) |

Each meeting is its own timestamped folder (`YYYY-MM-DD_HHMM`) holding
`mic.wav` and `system.wav`. You don't need to navigate there manually —
the tray menu's "Open latest recording" / "Open recordings folder" items
take you straight there.

## System-audio (loopback) setup

The mic track works out of the box. The **system** track needs a
loopback capture device on each OS:

- **Windows** — enable **Stereo Mix** (Settings → System → Sound → More
  sound settings → *Recording* tab → right-click → *Show Disabled
  Devices* → enable *Stereo Mix*), or install
  [VB-Cable](https://vb-audio.com/Cable/) and select it.
- **Linux** — PAmt auto-detects a PulseAudio/PipeWire **Monitor** source.
  If none is found, create one with `pactl load-module module-null-sink
  monitor_source_name=PAmtLoopback`.
- **macOS** — install [BlackHole](https://existentialaudio.com/blackhole/)
  and select it as a system-input device.

If the system track can't be opened, PAmt records the mic only and tells
you what to enable.

## Sample rates & bit depth

Each track is **captured at 24-bit** (falling back to 16-bit if a device
can't open 24-bit) and written as a **16-bit mono WAV**. Capturing at
24-bit is the source-level quality fix: it gives ~18 dB more headroom, so
quiet laptop mics can be amplified *without* clipping and the noise floor
drops. The files stay 16-bit because the transcription engine (whisper.cpp)
wants 16-bit, so nothing downstream changes.

Each track is recorded at the **device's native sample rate**. WASAPI on
Windows is picky about rates, so PAmt opens each device at the rate it
actually accepts rather than forcing one global rate. The two tracks can
end up at different rates (e.g. 44.1 kHz mic, 48 kHz system) — that's fine,
the transcription step resamples as needed.

## Choosing the best devices

PAmt auto-detects a mic and a system-audio (loopback) device, but the
auto-pick can land on a weak default (e.g. Windows' generic
"Microsoft Sound Mapper"). If a track sounds bad, right-click the tray icon
→ **⚙ Choose audio devices** and pick explicitly:

- **Microphone** — choose your actual mic/headset, not a generic mapper.
- **System audio** — choose the best loopback source. For the clearest
  meeting capture on Windows, install
  [VB-Cable](https://vb-audio.com/Cable/), set your meeting output to play
  through the VB-Cable, then select it here. VB-Cable captures exactly what
  plays in the browser at full fidelity — this is the single biggest
  quality win for the system track.

## From WAV to transcript

This release focuses on **recording**. Transcription is the next phase:
the two WAVs are designed to be fed to a local ASR engine (e.g.
whisper.cpp) independently, then merged into a labeled transcript. That
keeps PAmt light and dependency-free for now.

## Clean audio (Desert Ant Labs Clear)

Optional, opt-in. Runs [Desert Ant Labs' Clear
model](https://desertant.com/models/clear/) — on-device speech
enhancement (denoise, dereverb, loudness-normalize) — on the latest
recording's tracks. Trained for exactly this use case: meeting recorders,
laptop mics, untreated rooms.

**What you get:** a `<name>_clean.wav` beside each original (the originals
are never modified), normalized to a broadcast-style speech level. A
dialog reports the output paths and how long it took.

**Presets** (loudness targets, from the Clear SDK):

- **meeting** (default) — balanced speech, good for listening back and
  for feeding transcription.
- **podcast** — a touch louder, broadcast-style.
- **video** — for voice-over picture.
- **voiceover** — loudest, VO/announcer-style.

The tray uses the default (meeting) preset; to use another, call the
library directly:

```python
from pamt.clean import clean_wav
res = clean_wav("2026-09-09_1835_mic.wav", preset="podcast")
print(res.output_path)   # ..._mic_clean.wav
```

**Setup (once, per machine):** PAmt's core stays dependency-free; clean
audio needs Node.js 18+ and the Clear SDK:

```bash
node setup.js        # from this repo; installs the SDK into pamt/node/
```

Then restart PAmt. The tray shows **✨ Clean audio (latest)…** and
**⚙ Clean audio status** (the status item tells you exactly what's
missing if anything is). First clean run downloads the model (~24 MB,
ONNX) into the shared Desert Ant cache; later runs are ~100x realtime.

**Privacy & license.** Inference is fully local — no audio leaves your
machine. The SDK/weights are under the [Desert Ant Labs Source-Available
License](https://license.desertant.com/1.0) (free under 100k monthly
active devices per model per platform). Clear's SDK sends a minimal
device-count telemetry ping (no audio, no content) unless disabled; PAmt
passes through whatever the SDK does by default. PAmt credits Desert Ant
Labs in its About box, as the license requires.

## Build from source

Requires Python 3.10+ and PortAudio (`libportaudio2` / `brew install
portaudio`).

```bash
pip install -e .
pamt            # launch the tray app
```

## Privacy

- 100% local inference. PAmt never phones home, and Clear runs entirely on
  your machine — the audio never leaves it.
- Audio is written only to your disk, in your OS's standard user-data
  location (see *Where recordings are saved*).
- One exception: if you use the optional **Clean audio** feature, its first
  run downloads the Clear model (~24 MB) once and caches it, and the Desert
  Ant SDK sends a minimal usage ping (a device id for their usage
  attribution — no audio, no content) as its license requires. PAmt itself
  still makes no network calls.

## Roadmap

- [x] Dual-track local recording (mic + system audio)
- [x] System tray + global hotkey
- [x] Cross-platform binaries via GitHub Actions
- [ ] Local transcription (whisper.cpp) with speaker labels
- [ ] Automatic transcript merge + export (Markdown / SRT)
- [ ] Per-meeting folders + auto-naming
- [x] Clean audio (Desert Ant Labs Clear, opt-in)
- [ ] Clean-audio presets in the tray (podcast / video / voiceover)

## License

MIT
