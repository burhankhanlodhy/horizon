'use strict';

// Browser sign-in for one computer (POST /code/cli/start, then /code/cli/poll),
// and the device calls the installers make: the plan note, the usage summary
// and the release on uninstall. Device keys are sent only as bearer tokens and
// never printed.

const { request, ApiError, detailOf } = require('./api');
const { CsError, codeSiteUrlOf } = require('./code-env');

const VERIFY_URL_SHAPE = /^https?:\/\/[A-Za-z0-9._~:/?=%-]+$/;
const DEVICE_KEY_SHAPE = /^cs_dev_[A-Za-z0-9_-]{16,200}$/;
const EXPIRED = 'The sign-in code expired before this computer was approved. Run this command again. Nothing was changed.';

/** A sign-in that did not finish. Nothing was changed. */
class LoginError extends CsError {}

/** The account already has its devices. The message names them; the exit code is 3. */
class DeviceLimitError extends CsError {
  constructor(devices, commandName, siteUrl) {
    super(limitMessage(devices, commandName, siteUrl), 3);
    this.devices = devices;
  }
}

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// Device names come from the server. Control characters and long names are cut.
function cleanName(value) {
  const text = String(value || '').replace(/[\x00-\x1f\x7f]/g, ' ').trim().slice(0, 60);
  return text || 'unnamed device';
}

function limitMessage(devices, commandName, siteUrl) {
  const names = devices.map((device) => cleanName(device && device.name)).join(', ');
  return (
    `This account already has ${devices.length} devices connected: ${names}.\n` +
    `Disconnect one first: run "npx ${commandName} uninstall" on that device, ` +
    `or remove it at ${siteUrl}/account. Then run this command again.`
  );
}

function seconds(value, fallback, max) {
  const number = Number(value);
  if (!Number.isFinite(number) || number < 0) return fallback;
  return Math.min(number, max);
}

/**
 * Starts a sign-in, shows the link and code, opens the browser, and polls until
 * this computer is approved. Resolves { device_id, device_key, api_url }.
 * Throws LoginError, DeviceLimitError, or ApiError when the server refuses.
 */
async function runBrowserLogin(ctx, { apiUrl, tool, deviceHash, deviceName, commandName, openBrowser, sleep = delay, env }) {
  const siteUrl = codeSiteUrlOf(env || {});
  const start = await request(apiUrl, '/code/cli/start', {
    method: 'POST',
    body: { tool, device_hash: deviceHash, device_name: cleanName(deviceName).slice(0, 60), platform: 'windows' },
  });
  if (start.status !== 200 || !start.body) throw new ApiError(detailOf(start, 'Could not start the sign-in'), start.status);
  const { request_id: requestId, poll_secret: pollSecret, user_code: userCode, verify_url: verifyUrl } = start.body;
  if (
    typeof requestId !== 'string' ||
    typeof pollSecret !== 'string' ||
    typeof userCode !== 'string' ||
    typeof verifyUrl !== 'string' ||
    !VERIFY_URL_SHAPE.test(verifyUrl)
  ) {
    throw new LoginError('ContextShrink sent an unexpected sign-in response. Nothing was changed.');
  }
  const interval = seconds(start.body.interval, 3, 30);
  const deadline = Date.now() + seconds(start.body.expires_in, 600, 3600) * 1000;

  ctx.out('');
  ctx.out('To connect this computer, approve it in your browser:');
  ctx.out(`  ${verifyUrl}`);
  ctx.out(`Code: ${userCode}`);
  try {
    await openBrowser(verifyUrl);
  } catch {
    ctx.out('Could not open the browser. Open the link above yourself.');
  }
  ctx.out('Waiting for approval...');

  for (;;) {
    const poll = await request(apiUrl, '/code/cli/poll', {
      method: 'POST',
      body: { request_id: requestId, poll_secret: pollSecret },
    });
    if (poll.status === 404) throw new LoginError(EXPIRED);
    if (poll.status !== 200 || !poll.body) throw new ApiError(detailOf(poll, 'Sign-in failed'), poll.status);
    const body = poll.body;
    if (body.status === 'approved') {
      if (typeof body.device_id !== 'string' || !DEVICE_KEY_SHAPE.test(body.device_key || '')) {
        throw new LoginError('ContextShrink sent an unexpected sign-in response. Nothing was changed.');
      }
      return {
        device_id: body.device_id,
        device_key: body.device_key,
        api_url: typeof body.api_url === 'string' && body.api_url ? body.api_url : apiUrl,
      };
    }
    if (body.status === 'device_limit') {
      throw new DeviceLimitError(Array.isArray(body.devices) ? body.devices : [], commandName, siteUrl);
    }
    if (body.status === 'denied') throw new LoginError('The sign-in was not approved. Nothing was changed.');
    if (body.status === 'expired') throw new LoginError(EXPIRED);
    if (body.status !== 'pending') throw new LoginError('ContextShrink sent an unexpected sign-in response. Nothing was changed.');
    if (Date.now() >= deadline) throw new LoginError(EXPIRED);
    await sleep(interval * 1000);
  }
}

/** The plan note for this device. Returns { status, body }; 200 is entitled, 402 is not, 401/403 mean re-sign-in. */
async function fetchPlanNote(apiUrl, deviceKey, deviceHash) {
  const res = await request(apiUrl, '/code/entitlement', {
    method: 'POST',
    token: deviceKey,
    body: { device_hash: deviceHash },
  });
  return { status: res.status, body: res.body };
}

/** Today's usage for this device's account. Throws ApiError when it is not available. */
async function fetchUsageSummary(apiUrl, deviceKey, { timeoutMs } = {}) {
  const res = await request(apiUrl, '/code/usage/summary', { token: deviceKey, timeoutMs });
  if (res.status !== 200 || !res.body) throw new ApiError('Usage is not available right now.', res.status);
  return res.body;
}

/** Revokes this device on the server. An unknown device counts as released. */
async function releaseDevice(apiUrl, deviceKey) {
  const res = await request(apiUrl, '/code/device', { method: 'DELETE', token: deviceKey });
  if (res.status === 200 || res.status === 204 || res.status === 404) return;
  throw new ApiError(detailOf(res, 'Could not release this device'), res.status);
}

module.exports = {
  LoginError,
  DeviceLimitError,
  cleanName,
  limitMessage,
  runBrowserLogin,
  fetchPlanNote,
  fetchUsageSummary,
  releaseDevice,
  DEVICE_KEY_SHAPE,
};
