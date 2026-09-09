#!/usr/bin/env node
// PAmt clean-audio launcher.
//
// pamt/clean.py invokes this (it lives inside pamt/node/, next to
// node_modules, so Node can resolve the SDK). It just re-exports ./clean.js:

import { run } from "./clean.js";
run(process.argv.slice(2)).catch((e) => {
  process.stdout.write(JSON.stringify({ ok: false, error: String(e && e.message || e) }) + "\n");
  process.exit(1);
});
