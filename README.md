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
- **Dual-track capture** — writes `~/.pamt/YYYY-MM-DD_HHMM/mic.wav` and
  `~/.pamt/YYYY-MM-DD_HHMM/system.wav`.
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
3. Press **`Ctrl+Alt+R`** (or right-click the tray → *Record meeting*).
   The icon turns red with a red dot and a live timer.
4. When the call ends, press **`Ctrl+Alt+R`** again (or *Stop*).
5. Your two WAV files are in `~/.pamt/` (or `C:\Users\<you>\.pamt\` on
   Windows).

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

## Sample rates

Each track is recorded at the **device's native sample rate** (16-bit
mono). WASAPI on Windows is picky about rates, so PAmt opens each device
at the rate it actually accepts rather than forcing one global rate. The
two tracks can end up at different rates (e.g. 44.1 kHz mic, 48 kHz
system) — that's fine, the transcription step resamples as needed.

## From WAV to transcript

This release focuses on **recording**. Transcription is the next phase:
the two WAVs are designed to be fed to a local ASR engine (e.g.
whisper.cpp) independently, then merged into a labeled transcript. That
keeps PAmt light and dependency-free for now.

## Build from source

Requires Python 3.10+ and PortAudio (`libportaudio2` / `brew install
portaudio`).

```bash
pip install -e .
pamt            # launch the tray app
```

## Privacy

- 100% local. PAmt never phones home.
- Audio is written only to your disk, under `~/.pamt/`.
- No network access at all.

## Roadmap

- [x] Dual-track local recording (mic + system audio)
- [x] System tray + global hotkey
- [x] Cross-platform binaries via GitHub Actions
- [ ] Local transcription (whisper.cpp) with speaker labels
- [ ] Automatic transcript merge + export (Markdown / SRT)
- [ ] Per-meeting folders + auto-naming

## License

MIT
