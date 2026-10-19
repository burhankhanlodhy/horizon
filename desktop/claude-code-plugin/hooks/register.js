// ContextShrink savings line, drawn in the band above the prompt.
//
// The line comes from contextshrink-status.exe, the same helper the terminal
// status line uses, so the "off", "paused" and staleness rules are identical.
// It is refreshed at session start, after each turn, and every 30 seconds.

const REFRESH_MS = 30000
const NO_DATA_LINE = 'ContextShrink off'

let line = 'ContextShrink ...'

async function dataRoot($) {
  const override = await $.env.get('CONTEXTSHRINK_DATA_DIR')
  if (override) return override
  const local = await $.env.get('LOCALAPPDATA')
  return local ? local + '\\ContextShrink' : null
}

async function refresh($) {
  const root = await dataRoot($)
  if (!root) {
    line = NO_DATA_LINE
    return
  }
  try {
    const result = await $.process.run([root + '\\bin\\contextshrink-status.exe'])
    const first = String(result.stdout || '').split('\n')[0].trim()
    line = first || NO_DATA_LINE
  } catch (error) {
    line = NO_DATA_LINE
  }
}

export function register(on) {
  on('session.start', async ($, e, next) => {
    await refresh($)
    $.clock.every(REFRESH_MS, async () => {
      await refresh($)
      $.ui.invalidate('ui.render')
    })
    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    await refresh($)
    $.ui.invalidate('ui.render')
    return next(e)
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const { Box, Text } = $.ui.resolve(e)
    // Keep whatever later mods draw in the band, under this line
    const below = await next(e)
    const children = [Text({ dimColor: true, children: [line] })]
    if (below) children.push(below)
    return Box({ flexDirection: 'column', children })
  })
}
