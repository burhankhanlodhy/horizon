import { expect, mock, test } from 'claude-code/testing'

// What Claude Code passes to a ui.render hook for the band above the prompt
const BAND = {
  plugin: 'contextshrink',
  component: 'AbovePrompt',
  requestId: 'above-prompt',
  viewport: { columns: 100, rows: 30 },
  props: {
    hasSurvey: false,
    isWorking: false,
    maxRows: 4,
    bodyColumns: 100,
    scroll: { offset: 0, bodyRows: 4 },
    view: {},
  },
} as const

const LIVE = 'ContextShrink \u2713 trimmed 22% \u00b7 $1.84 of tokens at list price \u00b7 saved $0.41 verified \u00b7 18 req'
const LOCAL = 'C:\Users\Test\AppData\Local'

test('the band shows the helper line at session start', async ($, on) => {
  mock.clock(on)
  mock.env(on, { LOCALAPPDATA: LOCAL })
  // Stands for the band's drawing that Claude Code makes beneath this mod
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['below'] }))
  on('session.start', () => ({ cwd: '/work' }))
  on('process.run', ($, e) => {
    expect(e.argv[0]).toMatch(/contextshrink-status\.exe$/)
    return { value: { exitCode: 0, stdout: LIVE + '\n', stderr: '' } }
  })

  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  const band = await $.ui.mount({ ...BAND, surface: 'terminal' })
  expect(await band.find({ type: 'Text', text: LIVE })).toBeDefined()
  await band.unmount()
})

test('the band reads off when the helper cannot run', async ($, on) => {
  mock.clock(on)
  mock.env(on, { LOCALAPPDATA: LOCAL })
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['below'] }))
  on('session.start', () => ({ cwd: '/work' }))
  on('process.run', () => ({ deny: 'helper missing' }))

  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  const band = await $.ui.mount({ ...BAND, surface: 'terminal' })
  expect(await band.find({ type: 'Text', text: 'ContextShrink off' })).toBeDefined()
  await band.unmount()
})

test('the band refreshes after a turn completes', async ($, on) => {
  mock.clock(on)
  mock.env(on, { LOCALAPPDATA: LOCAL })
  let current = 'ContextShrink off'
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['below'] }))
  on('session.start', () => ({ cwd: '/work' }))
  on('turn.complete', () => ({ text: '' }))
  on('process.run', () => ({ value: { exitCode: 0, stdout: current + '\n', stderr: '' } }))

  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  current = LIVE
  await $.turn.complete({ turnId: 't1', answer: 'done', durationMs: 1, isAborted: false, usage: null })
  const band = await $.ui.mount({ ...BAND, surface: 'terminal' })
  expect(await band.find({ type: 'Text', text: LIVE })).toBeDefined()
  await band.unmount()
})

test('the band refreshes every 30 seconds', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { LOCALAPPDATA: LOCAL })
  let current = 'ContextShrink off'
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['below'] }))
  on('session.start', () => ({ cwd: '/work' }))
  on('process.run', () => ({ value: { exitCode: 0, stdout: current + '\n', stderr: '' } }))

  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  current = LIVE
  await clock.advance(29000)
  let band = await $.ui.mount({ ...BAND, surface: 'terminal' })
  expect(await band.find({ type: 'Text', text: 'ContextShrink off' })).toBeDefined()
  await band.unmount()

  await clock.advance(1000)
  band = await $.ui.mount({ ...BAND, surface: 'terminal' })
  expect(await band.find({ type: 'Text', text: LIVE })).toBeDefined()
  await band.unmount()
})

test('a session end hands its id to the helper on stdin and does nothing else', async ($, on) => {
  mock.clock(on)
  mock.env(on, { LOCALAPPDATA: LOCAL })
  const runs: { argv: readonly string[]; stdin?: string }[] = []
  on('session.start', () => ({ cwd: '/work' }))
  on('session.end', (_, e) => ({ sessionId: e.sessionId }))
  on('process.run', (_, e) => {
    runs.push({ argv: e.argv, stdin: e.init?.stdin })
    return { value: { exitCode: 0, stdout: '', stderr: '' } }
  })

  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  runs.length = 0
  await $.session.end({
    reason: 'prompt_input_exit',
    sessionId: 'sess-42',
    resume: { id: 'sess-42' },
  })

  expect(runs.length).toBe(1)
  expect(runs[0].argv[0]).toMatch(/contextshrink-status\.exe$/)
  expect(runs[0].argv[1]).toBe('--session-end')
  expect(JSON.parse(runs[0].stdin ?? '{}')).toEqual({ session_id: 'sess-42' })
})

test('a session end with no helper still ends cleanly', async ($, on) => {
  mock.clock(on)
  mock.env(on, { LOCALAPPDATA: LOCAL })
  on('session.start', () => ({ cwd: '/work' }))
  on('session.end', (_, e) => ({ sessionId: e.sessionId }))
  on('process.run', () => ({ deny: 'helper missing' }))

  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  await $.session.end({ reason: 'clear', sessionId: 'sess-43', resume: { id: 'sess-43' } })
})
