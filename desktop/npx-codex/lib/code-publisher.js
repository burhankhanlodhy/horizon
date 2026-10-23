'use strict';

// The publisher pin for ContextShrink's signed Windows files: the launcher (Code
// tab) and the engine. A file is trusted only when its Authenticode status is
// Valid, its signer is the ContextShrink publisher, and its certificate comes from
// Microsoft's ID Verified authority. Shared by both npx packages (identical copies).

const path = require('node:path');
const { execFile } = require('node:child_process');
const files = require('./files');

const SIGNER_SUBJECT = /(^|,\s*)CN=Muhammad Burhan Khan Lodhi(,|$)/;
const SIGNER_ISSUER = 'Microsoft ID Verified';
const CHECK_TIMEOUT_MS = 60000;

// The publisher check. Status must be Valid, the signer must be the ContextShrink
// publisher and the certificate must come from Microsoft's ID Verified authority.
function signatureProblem(info) {
  if (!files.isPlainObject(info) || info.Status !== 'Valid') {
    const status = files.isPlainObject(info) && info.Status ? info.Status : 'unknown';
    return `signature status is ${status}`;
  }
  if (!SIGNER_SUBJECT.test(String(info.Subject || ''))) return 'the signer is not the expected publisher';
  if (!String(info.Issuer || '').includes(SIGNER_ISSUER)) return 'the certificate is not from the expected authority';
  return null;
}

// The file path goes in an environment variable, never in the script text.
const SIGNATURE_SCRIPT = [
  "$ErrorActionPreference = 'Stop'",
  '$sig = Get-AuthenticodeSignature -LiteralPath $env:CONTEXTSHRINK_CHECK_PATH',
  "$subject = ''",
  "$issuer = ''",
  '$cert = $sig.SignerCertificate',
  'if ($null -ne $cert) { $subject = $cert.Subject; $issuer = $cert.Issuer }',
  '[pscustomobject]@{ Status = [string]$sig.Status; Subject = $subject; Issuer = $issuer } | ConvertTo-Json -Compress',
].join('\n');

function powerShellPath() {
  const root = process.env.SystemRoot || process.env.windir || 'C:\\Windows';
  return path.join(root, 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe');
}

/** { Status, Subject, Issuer } of the file's Authenticode signature. Rejects when it cannot be read. */
function readSignature(file) {
  const encoded = Buffer.from(SIGNATURE_SCRIPT, 'utf16le').toString('base64');
  return new Promise((resolve, reject) => {
    execFile(
      powerShellPath(),
      ['-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
      {
        env: { ...process.env, CONTEXTSHRINK_CHECK_PATH: file },
        windowsHide: true,
        timeout: CHECK_TIMEOUT_MS,
        maxBuffer: 1 << 20,
      },
      (error, stdout) => {
        const line = String(stdout || '').trim().split(/\r?\n/).pop() || '';
        let info = null;
        try {
          info = JSON.parse(line);
        } catch {
          info = null;
        }
        if (error || !files.isPlainObject(info)) {
          reject(new Error('could not check the signature of a file'));
        } else {
          resolve(info);
        }
      }
    );
  });
}

module.exports = { signatureProblem, readSignature };
