'use strict';

// Paths, URLs, errors and test overrides shared by contextshrink-claude and
// contextshrink-codex. Both packages ship identical copies of lib/code-*.js.
//
// Every path and URL can be overridden through the environment, so tests never
// touch the real home folder, ~/.claude, ~/.codex, %LOCALAPPDATA% or the live
// services. The overrides that change platform checks or fixtures are honoured
// only with CONTEXTSHRINK_NPX_TEST=1.

const os = require('node:os');
const path = require('node:path');

const DEFAULT_API_URL = 'https://api.contextshrink.com';
const DEFAULT_CODE_SITE_URL = 'https://code.contextshrink.com';
// The engine's fixed local address (horizon code-engine run --port 18793).
const DEFAULT_ENGINE_URL = 'http://127.0.0.1:18793';
const LOOPBACK_HOST = '127.0.0.1';

/** A failure with a message for the user. Nothing is changed when it is thrown. */
class CsError extends Error {
  constructor(message, exitCode = 1) {
    super(message);
    this.exitCode = exitCode;
  }
}

function testOverride(env, name) {
  if (env.CONTEXTSHRINK_NPX_TEST !== '1') return '';
  return (env[name] || '').trim();
}

function platformOf(env) {
  return testOverride(env, 'CONTEXTSHRINK_NPX_TEST_PLATFORM') || process.platform;
}

function homeOf(env) {
  return (env.USERPROFILE || env.HOME || '').trim() || os.homedir();
}

// ~/.contextshrink, or CONTEXTSHRINK_HOME. Holds device.json, license.json and usage-pending.json.
function contextshrinkHome(env) {
  const raw = (env.CONTEXTSHRINK_HOME || '').trim();
  return raw ? path.resolve(raw) : path.join(homeOf(env), '.contextshrink');
}

function localAppData(env) {
  return (env.LOCALAPPDATA || '').trim() || path.join(homeOf(env), 'AppData', 'Local');
}

function appData(env) {
  return (env.APPDATA || '').trim() || path.join(homeOf(env), 'AppData', 'Roaming');
}

// https, or http on 127.0.0.1 (tests). Returns the URL without a trailing slash.
function serviceUrl(env, name, fallback) {
  const value = ((env[name] || '').trim() || fallback).replace(/\/+$/, '');
  let url;
  try {
    url = new URL(value);
  } catch {
    throw new CsError(`${name} is not a valid URL.`);
  }
  const local = url.protocol === 'http:' && url.hostname === LOOPBACK_HOST;
  if (url.username || url.password || url.search || url.hash || (url.protocol !== 'https:' && !local)) {
    throw new CsError(`${name} must be an https URL.`);
  }
  return value;
}

function apiUrlOf(env) {
  return serviceUrl(env, 'CONTEXTSHRINK_API_URL', DEFAULT_API_URL);
}

function codeSiteUrlOf(env) {
  return serviceUrl(env, 'CONTEXTSHRINK_CODE_SITE_URL', DEFAULT_CODE_SITE_URL);
}

// The engine only listens on loopback, so the address must be loopback too.
function engineUrlOf(env) {
  const value = serviceUrl(env, 'CONTEXTSHRINK_ENGINE_URL', DEFAULT_ENGINE_URL);
  if (new URL(value).hostname !== LOOPBACK_HOST) throw new CsError('CONTEXTSHRINK_ENGINE_URL must be on 127.0.0.1.');
  return value;
}

module.exports = {
  CsError,
  DEFAULT_API_URL,
  DEFAULT_CODE_SITE_URL,
  DEFAULT_ENGINE_URL,
  LOOPBACK_HOST,
  testOverride,
  platformOf,
  homeOf,
  contextshrinkHome,
  localAppData,
  appData,
  apiUrlOf,
  codeSiteUrlOf,
  engineUrlOf,
};
