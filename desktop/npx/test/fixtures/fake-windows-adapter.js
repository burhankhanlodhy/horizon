'use strict';

// Test stand-in for the Windows adapter (browser, Authenticode check, unzip,
// engine start, Startup shortcut). Nothing runs for real: every call is appended
// to CONTEXTSHRINK_NPX_ADAPTER_LOG, and the answers are canned.
//
// CONTEXTSHRINK_NPX_STUB_SIGNATURE sets the Authenticode status (default "Valid").
// CONTEXTSHRINK_NPX_STUB_SIGNER sets the signer subject (default: the ContextShrink publisher).
// CONTEXTSHRINK_NPX_STUB_ENGINE=fail makes `code-engine ensure` fail.
// CONTEXTSHRINK_NPX_STUB_BROWSER=fail makes the browser open fail.

const fs = require('node:fs');
const path = require('node:path');

const PUBLISHER_SUBJECT = 'CN=Muhammad Burhan Khan Lodhi, O=Muhammad Burhan Khan Lodhi, L=Somewhere, C=GB';

module.exports = (env) => {
  const log = (entry) => {
    if (env.CONTEXTSHRINK_NPX_ADAPTER_LOG) fs.appendFileSync(env.CONTEXTSHRINK_NPX_ADAPTER_LOG, JSON.stringify(entry) + '\n');
  };
  return {
    async openBrowser(url) {
      log({ fn: 'openBrowser', url });
      if (env.CONTEXTSHRINK_NPX_STUB_BROWSER === 'fail') throw new Error('no browser in tests');
    },
    async signatureStatus(file) {
      log({ fn: 'signatureStatus', file: path.basename(file) });
      return {
        Status: env.CONTEXTSHRINK_NPX_STUB_SIGNATURE || 'Valid',
        Subject: env.CONTEXTSHRINK_NPX_STUB_SIGNER || PUBLISHER_SUBJECT,
        Issuer: 'CN=Microsoft ID Verified CS EOC CA 01, O=Microsoft Corporation, C=US',
      };
    },
    async expandZip(zip, dest) {
      log({ fn: 'expandZip', zip: path.basename(zip), dest: path.basename(dest) });
      fs.mkdirSync(dest, { recursive: true });
      fs.writeFileSync(path.join(dest, 'horizon.exe'), 'fake engine\n');
    },
    async writeShortcut(file, spec) {
      log({ fn: 'writeShortcut', name: path.basename(file), args: spec.args });
      fs.writeFileSync(file, 'fake shortcut\n');
    },
    async runEngine(exe, args) {
      log({ fn: 'runEngine', exe: path.basename(path.dirname(exe)), args });
      if (env.CONTEXTSHRINK_NPX_STUB_ENGINE === 'fail') throw new Error('ensure failed in tests');
    },
  };
};
