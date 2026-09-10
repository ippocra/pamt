# Changelog

## v0.7.0

- **Raw capture** — PAmt no longer modifies the audio it records: no
  auto-gain, no low-pass "de-fuzz" filter. The WAV files are the raw
  device signal (24-bit capture → 16-bit WAV), and the level meter is the
  only thing computed on the fly. (Fixes #3)
- **Clean audio is on by default** — the Clear model (Desert Ant Labs) is
  baked into the released binaries, so clean audio works out of the box;
  the ~24 MB of model weights download once on first use. New tray items:
  **Disable/Enable clean audio** (the choice is remembered in the config
  file) and the status item now reports SDK/model readiness. (Fixes #3)
- **Recordings folder finds old locations** — "Open latest recording"
  and the recordings-folder menu items now also scan the legacy
  locations used by older PAmt versions, so pre-0.7 recordings remain
  reachable from the tray. (Fixes #3)
- **About box with logo** — the About dialog now shows the PAmt logo,
  name and version (falls back to the plain message box where a GUI
  dialog isn't available). (Fixes #3)
- Packaging: the Clear SDK (`pamt/node/node_modules`) and its lockfile
  are now shipped with the wheel and the PyInstaller binary
  (`--collect-all pamt` already collects them); GitHub Actions installs
  the SDK before building so the binary contains it.

## v0.6.1

- Fix: tray menu label crash (pystray `MenuItem.text` is read-only).

## v0.6.0

- Clean audio: optional, opt-in integration of Desert Ant Labs' Clear
  model for denoise/dereverb/loudness-normalization of the latest
  recording.

## v0.2.0

- Recordings moved to the per-OS user-data location (platformdirs).
