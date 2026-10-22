'use strict';

// Asking the user for input. Prompts go to stderr, so stdout stays clean.
//
// In a terminal, a line editor reads each answer and the password is not
// echoed. When stdin is a pipe, the answers are read in order, one line each:
// email, then password (or the key for `login --key`). Nothing is echoed.

class Cancelled extends Error {
  constructor() {
    super('Cancelled.');
  }
}

function createPrompter({ stdin = process.stdin, stderr = process.stderr } = {}) {
  let pipedLines = null;

  async function readPipedLines() {
    if (pipedLines) return pipedLines;
    const chunks = [];
    for await (const chunk of stdin) chunks.push(Buffer.from(chunk));
    const lines = Buffer.concat(chunks).toString('utf8').split(/\r?\n/);
    if (lines.length && lines[lines.length - 1] === '') lines.pop();
    pipedLines = lines;
    return pipedLines;
  }

  function readTerminal(label, hidden) {
    return new Promise((resolve, reject) => {
      stderr.write(label);
      const wasRaw = Boolean(stdin.isRaw);
      let line = '';
      const finish = (ok) => {
        stdin.removeListener('data', onData);
        stdin.setRawMode(wasRaw);
        stdin.pause();
        stderr.write('\n');
        if (ok) resolve(line);
        else reject(new Cancelled());
      };
      const onData = (chunk) => {
        for (const ch of String(chunk)) {
          if (ch === '\r' || ch === '\n' || ch === '\u0004') {
            finish(true);
            return;
          }
          if (ch === '\u0003') {
            finish(false);
            return;
          }
          if (ch === '\u007f' || ch === '\b') {
            if (line.length) {
              line = line.slice(0, -1);
              if (!hidden) stderr.write('\b \b');
            }
            continue;
          }
          if (ch < ' ') continue;
          line += ch;
          if (!hidden) stderr.write(ch);
        }
      };
      stdin.setRawMode(true);
      stdin.setEncoding('utf8');
      stdin.on('data', onData);
      stdin.resume();
    });
  }

  /**
   * Asks one question. `hidden` suppresses echo (passwords and keys).
   * Throws Cancelled on Ctrl+C in a terminal.
   */
  async function ask(label, { hidden = false } = {}) {
    if (stdin.isTTY) return readTerminal(label, hidden);
    const lines = await readPipedLines();
    stderr.write(label + '\n');
    const value = lines.length ? lines.shift() : '';
    return value.replace(/\r$/, '');
  }

  return { ask };
}

module.exports = { Cancelled, createPrompter };
