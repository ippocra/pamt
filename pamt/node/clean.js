#!/usr/bin/env node
// PAmt "clean audio" helper — runs Desert Ant Labs' Clear model on a WAV file.
//
// Invoked by pamt/clean.py (via ./run.js, which just re-exports this file so
// Node can resolve the SDK from this directory's node_modules):
//   node run.js --clean <input.wav> --preset meeting|podcast|video|voiceover
//               [--cache-root <dir>]
//
// Prints a single JSON object on stdout:
//   ok: { ok: true, input, output, durationSec, processingSec, preset, note? }
//   err: { ok: false, error: "<message>" }
// Exit code 0 on success, 1 on any failure. Progress/diagnostics go to stderr.
//
// The SDK lives in ./node_modules (installed by setup.js, baked into the
// wheel/binary), so this file must stay inside pamt/node/: ESM resolves
// packages from the importing file's own directory, and a helper one level up
// would not see node_modules.
//
// Node needs the *native* build of Clear (`/native` subpath); the default
// import is the WebAssembly/browser runtime and refuses to load() in Node.
//
// --cache-root: where the model weights (~24 MB) live. PAmt passes its own
// per-user cache dir (XDG_CACHE_HOME aware); when omitted the SDK uses its
// own default (~/.cache).

import { readFileSync, writeFileSync } from "node:fs";
import { Clear } from "@desert-ant-labs/clear/native";
import { decodeWav } from "@desert-ant-labs/core/audio/node";

// PAmt presets -> Clear loudness targets (integrated LUFS). The SDK ships
// applePodcasts/podcast (-19), spotify/youtube (-14), broadcast (-23).
// "meeting" skips mastering (the model's own level is already a clean,
// balanced speech level and the safest choice for listening back and for
// feeding transcription); "video" uses the YouTube target; "voiceover"
// uses the loudest shipped preset (spotify, -14 LUFS).
const PRESETS = {
  meeting: { lufs: null, note: "no mastering (Clear's native level)" },
  podcast: { lufs: "podcast", note: "podcast loudness (-19 LUFS)" },
  video: { lufs: "youtube", note: "video loudness (-14 LUFS)" },
  voiceover: { lufs: "spotify", note: "voiceover loudness (-14 LUFS)" },
};

function fail(msg) {
  process.stdout.write(JSON.stringify({ ok: false, error: msg }) + "\n");
  process.exit(1);
}

function encodeWav16(samples, sampleRate) {
  const n = samples.length;
  const out = new Uint8Array(44 + n * 2);
  const dv = new DataView(out.buffer);
  const tag = (o, s) => { for (let i = 0; i < s.length; i++) out[o + i] = s.charCodeAt(i); };
  tag(0, "RIFF"); dv.setUint32(4, 36 + n * 2, true); tag(8, "WAVE"); tag(12, "fmt ");
  dv.setUint32(16, 16, true); dv.setUint16(20, 1, true); dv.setUint16(22, 1, true);
  dv.setUint32(24, sampleRate, true); dv.setUint32(28, sampleRate * 2, true);
  dv.setUint16(32, 2, true); dv.setUint16(34, 16, true); tag(36, "data");
  dv.setUint32(40, n * 2, true);
  for (let i = 0; i < n; i++) {
    const v = Math.max(-32768, Math.min(32767, Math.round(samples[i] * 32767)));
    dv.setInt16(44 + i * 2, v, true);
  }
  return out;
}

export async function run(argv) {
  if (argv[0] === "--warm-up") {
    // Download the model weights once (~24 MB) and cache them, so the first
    // real clean run is fast. Exits 0 when the weights are cached, 1 on a
    // download failure (e.g. no network) -- PAmt treats this as non-fatal.
    const cacheRootIdx = argv.indexOf("--cache-root");
    const cacheRoot = cacheRootIdx >= 0 ? argv[cacheRootIdx + 1] : null;
    const clear = await Clear.load(cacheRoot ? { cacheRoot } : {});
    try { clear.dispose(); } catch { /* ignore */ }
    // `clear.isDownloaded()` reflects the state at load time (false if the
    // weights were fetched during this run), so report the file on disk
    // instead: PAmt's model-ready check looks for a non-empty cache dir.
    process.stdout.write(JSON.stringify({
      ok: true, warmedUp: true,
    }) + "\n");
    process.exit(0);
  }
  if (argv[0] !== "--clean" || !argv[1]) {
    fail("usage: run.js --clean <input.wav> --preset <name> | --warm-up");
  }
  const input = argv[1];
  const presetIdx = argv.indexOf("--preset");
  const preset = presetIdx >= 0 ? argv[presetIdx + 1] : "meeting";
  const cacheRootIdx = argv.indexOf("--cache-root");
  const cacheRoot = cacheRootIdx >= 0 ? argv[cacheRootIdx + 1] : null;
  const spec = PRESETS[preset] || PRESETS.meeting;

  const bytes = new Uint8Array(readFileSync(input));
  let decoded;
  try {
    decoded = decodeWav(bytes);
  } catch (e) {
    return fail("could not decode WAV: " + (e && e.message || e));
  }
  process.stderr.write(
    `clear: ${(decoded.samples.length / decoded.sampleRate).toFixed(1)}s @ ` +
    `${decoded.sampleRate} Hz, ${decoded.channels} ch; ` +
    `loading model (first run downloads ~24 MB)...\n`
  );

  const clear = await Clear.load(cacheRoot ? { cacheRoot } : {});
  const options = {};
  if (spec.lufs != null) options.targetLUFS = spec.lufs;

  try {
    const result = await clear.enhance(decoded.samples, decoded.sampleRate, options);
    const outPath = input.replace(/\.wav$/i, "") + "_clean.wav";
    writeFileSync(outPath, encodeWav16(result.samples, result.sampleRate));
    process.stdout.write(JSON.stringify({
      ok: true,
      input,
      output: outPath,
      durationSec: result.durationSec,
      processingSec: result.processingSec,
      realtimeFactor: Number(result.realtimeFactor.toFixed(1)),
      inLufs: result.measuredLUFS,
      truePeakDBFS: result.measuredTruePeakDBFS,
      preset,
      note: spec.note + (result.measuredLUFS != null ?
        `; input was ${Number(result.measuredLUFS).toFixed(1)} LUFS` : ""),
    }) + "\n");
    process.exit(0);
  } catch (e) {
    fail(String(e && e.message || e));
  } finally {
    try { clear.dispose(); } catch { /* ignore */ }
  }
}
