// Generated with stable absolute paths by savings.js; never invokes npx.
const NODE = __NODE__;
const RENDER = __RENDER__;
let line = 'ContextShrink · loading savings';
let pending;
async function refresh($) {
  if (pending) return pending;
  pending = (async () => {
    try {
      const result = await $.process.run([NODE, RENDER], { timeoutMs: 2500 });
      line = String(result.stdout || '').split('\n')[0].trim() || 'ContextShrink · savings unavailable';
    } catch { line = 'ContextShrink · savings unavailable'; }
  })();
  try { await pending; } finally { pending = null; }
}
export function register(on) {
  on('session.start', async ($, e, next) => {
    await refresh($);
    $.clock.every(30000, async () => { await refresh($); $.ui.invalidate('ui.render'); });
    return next(e);
  });
  on('turn.complete', async ($, e, next) => {
    await refresh($);
    $.ui.invalidate('ui.render');
    return next(e);
  });
  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const { Box, Text } = $.ui.resolve(e);
    const below = await next(e);
    const children = [Text({ dimColor: true, children: [line] })];
    if (below) children.push(below);
    return Box({ flexDirection: 'column', children });
  });
}
