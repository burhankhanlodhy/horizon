'use strict';
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const { spawn } = require('node:child_process');
const files = require('./files');
const { ConfigError } = require('./toml');
async function refresh(readSnapshot, file) {
  let data;
  try {
    const raw = await readSnapshot();
    if (raw?.available !== true || !Number.isInteger(raw.pct) || raw.pct < 0 || raw.pct > 100 ||
        !Number.isSafeInteger(raw.requests) || raw.requests < 0 ||
        (raw.trialDays !== null && !(Number.isSafeInteger(raw.trialDays) && raw.trialDays >= 0)) ||
        !Number.isFinite(Date.parse(raw.updatedAt))) throw new Error('Invalid snapshot');
    data = { version: 1, available: true, pct: raw.pct, requests: raw.requests, trialDays: raw.trialDays, updatedAt: new Date(raw.updatedAt).toISOString() };
  }
  catch { data = { version: 1, available: false, updatedAt: new Date().toISOString() }; }
  // Only numeric savings and a timestamp cross into the window. Never pass keys,
  // service error text, configuration, conversation content, or bearer tokens.
  files.writePrivateJson(file, data);
}
async function show(readSnapshot, io, { platform = process.platform, spawnProcess = spawn, intervalMs = 30000 } = {}) {
  if (platform !== 'win32') throw new ConfigError('The live savings window is currently available on Windows. Use savings on other platforms.');
  const folder = fs.mkdtempSync(path.join(os.tmpdir(), 'contextshrink-codex-savings-'));
  const snapshot = path.join(folder, 'savings.json');
  let child, timer, closing = false, busy = false;
  const update = async () => {
    if (busy || closing) return;
    busy = true;
    try { await refresh(readSnapshot, snapshot); } finally { busy = false; }
  };
  const stop = () => { closing = true; child?.kill(); };
  try {
    await update();
    const powershell = path.join(process.env.SystemRoot || 'C:\\Windows', 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe');
    // Process-scoped policy permits this locally installed script. It does not
    // change the saved user/system policy or override enterprise Group Policy.
    child = spawnProcess(powershell, ['-NoProfile', '-NonInteractive', '-WindowStyle', 'Hidden', '-STA', '-ExecutionPolicy', 'RemoteSigned', '-File', path.join(__dirname, 'savings-window.ps1'),
      '-SnapshotPath', snapshot, '-OwnerPid', String(process.pid)], { windowsHide: true, stdio: 'ignore', shell: false });
    io.out('Live savings window opened. Refreshes every 30 seconds; close the window or press Ctrl+C to stop.');
    process.once('SIGINT', stop); process.once('SIGTERM', stop);
    timer = setInterval(() => { update().catch(stop); }, intervalMs);
    await new Promise((resolve, reject) => {
      child.once('error', () => reject(new ConfigError('Could not open the savings window. Check Windows PowerShell and your script execution policy.')));
      child.once('exit', code => code === 0 || closing ? resolve() : reject(new ConfigError('The savings window could not start. Check Windows PowerShell and your script execution policy.')));
    });
    return 0;
  } finally {
    closing = true; clearInterval(timer);
    process.removeListener('SIGINT', stop); process.removeListener('SIGTERM', stop);
    // An in-flight API call has a two-second deadline; wait for its atomic write
    // before removing the numeric-only temporary snapshot.
    while (busy) await new Promise(resolve => setTimeout(resolve, 20));
    fs.rmSync(snapshot, { force: true }); fs.rmdirSync(folder);
  }
}
module.exports = { show, refresh };
