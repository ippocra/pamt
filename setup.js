#!/usr/bin/env node
// PAmt clean-audio setup: installs the Desert Ant Clear SDK into pamt/node/
// so the tray's "Clean audio" feature works. Run once:
//
//   node setup.js            # from anywhere in this repo
//
// Requires Node.js 18+ and npm on PATH (the helper runs the SDK via Node).
// Weights (~24 MB) download on first use, not here.
//
// Note: this intentionally does NOT run the helper (it would need an audio
// file). It just installs the SDK and verifies the module resolves.

import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

// Check Node version early for a clear error message.
const [major, minor] = process.versions.node.split(".").map(Number);
if (major < 18) {
  console.error(`Node.js 18+ is required (you have ${process.versions.node}).`);
  process.exit(1);
}

const here = path.dirname(fileURLToPath(import.meta.url));
const nodeDir = path.join(here, "pamt", "node");
mkdirSync(nodeDir, { recursive: true });

const pkgJson = path.join(nodeDir, "package.json");
if (!existsSync(pkgJson)) {
  writeFileSync(pkgJson, JSON.stringify({
    name: "pamt-clean-audio",
    private: true,
    type: "module",
    dependencies: { "@desert-ant-labs/clear": "^3.1.0" }
  }, null, 2) + "\n");
  console.log(`wrote ${pkgJson}`);
}

console.log(`Installing Clear SDK into ${nodeDir} ...`);
execFileSync("npm", ["install", "--no-audit", "--no-fund"],
  { stdio: "inherit", cwd: nodeDir });

// Smoke test: resolve the SDK module (no audio, no model load).
const check = path.join(nodeDir, "node_modules", "@desert-ant-labs", "clear");
if (existsSync(check)) {
  console.log(`\nOK: Clear SDK is installed at ${check}`);
} else {
  console.error("SDK did not install correctly; see npm output above.");
  process.exit(1);
}
console.log("Restart PAmt; the tray now has 'Clean audio…' under the latest recording.");
