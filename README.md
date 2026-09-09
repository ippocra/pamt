# PAmt — Private Annotator Meeting Transcriber

Records online meeting audio (Zoom, Teams, Zoho Meet, any browser-based
call) as **two local WAV tracks** — so *you* can keep track of what was
said and relisten to the call:

- `*_mic.wav` — your microphone (what you say)
- `*_system.wav` — system audio (everyone else on the call)

Everything is local. No cloud, no account, no audio ever leaves the
machine. Two tracks means a later transcript can label "you" vs. "them"
for free.

> ## ⚠️ Recording etiquette / legal notice
>
> PAmt records audio **on your own computer**. It is intended for personal
> use: keeping track of what you said, relistening to a call, or building
> your own notes.
>
> **If you use it during a meeting with other people, you must make sure
> the other participants know the conversation is being recorded.**
> Recording a call without the knowledge and consent of all parties may be
> illegal in your jurisdiction and is always a breach of trust. The
> responsibility for compliant, courteous use is entirely yours.

## Status

**Phase 1 (recording) — done.** System-tray app with a global hotkey.
**Phase 2 (transcription) — planned.** CPU-friendly, model-agnostic
(faster-whisper / whisper.cpp), run after the call.

## Requirements

- Python 3.10+
- **PyAudio** (build deps per platform):
  - **Ubuntu**: `sudo apt install portaudio19-dev` then `pip install pyaudio`
  - **Windows 11**: `pip install pyaudio` (wheels ship prebuilt)
- `pystray`, `Pillow`, `pynput` (installed from `pyproject.toml`)

### System-audio capture device

PAmt records *system audio* (what the meeting plays) through a virtual
loopback input. Which one exists depends on your OS:

| OS      | What to use                                                        |
|---------|--------------------------------------------------------------------|
| Ubuntu  | A PulseAudio/PipeWire **Monitor** source (e.g. `alsa_output.pci-0000_00_1f.3.analog-stereo.monitor`). PAmt auto-detects names containing `monitor`. |
| Win 11  | **Stereo Mix** (enable in Sound Settings → More sound options → Recording) **or** install [VB-Cable](https://vb-audio.com/Cable/) |
| macOS   | Install [BlackHole](https://github.com/ExistentialAudio/BlackHole) |

If PAmt can't find one it will tell you exactly what to install.

## Install

```bash
git clone <this repo> && cd pamt
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .
```

## Usage

1. `pamt` — starts the tray icon (grey mic = idle, red mic + timer = recording).
2. Join your meeting in the browser.
3. Press **Ctrl+Alt+R** (or right-click the tray icon → *Record meeting*).
4. When the call ends, press **Ctrl+Alt+R** again.
5. The WAVs land in `~/meetings/` — the message box shows the paths.

The tray icon also shows a live level meter (`mic:3 sys:7`) while recording.

## File layout

```
~/meetings/
  2026-09-09_1435/
    2026-09-09_1435_mic.wav
    2026-09-09_1435_system.wav
```

## Roadmap

- [x] Phase 1: dual-track recording, tray icon, hotkey, level meter
- [ ] Phase 2: post-call transcription (faster-whisper, CPU/GPU optional),
      merge tracks into a speaker-labeled transcript
- [ ] Phase 3: feed transcript to ILAI for summarization / action items
- [ ] Auto-name session from the focused meeting window title
- [ ] Windows auto-start / service install helper

## License

MIT — see `LICENSE`.
