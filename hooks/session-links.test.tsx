import { expect, test, type Engine } from 'claude-code/testing'
import type { On } from 'claude-code'

const MESSAGE = { plugin: 'workstreams', component: 'AssistantMessage', requestId: 'm1', surface: 'terminal' } as const
const SID = '694b33c4-1e2e-4a5b-9c6d-7e8f90a1b2c3'
const HREF = `https://workstreams.invalid/session/${SID}`
const ROWS = { 1: { key: 'control', session_id: SID }, 2: { key: 'IDLE', session_id: null }, 9: { key: 'PROJ', goal: 'g' } }

// The engine beneath the plugin: the session's cwd, the sidebar's click map, board.py runs, and its own drawing of a reply.
function engine(on: On, rows: unknown = ROWS) {
  const k = { read: [] as string[], runs: [] as { argv: readonly string[]; init: unknown }[], engineText: undefined as string | undefined }
  on('session.cwd', () => ({ value: '/Users/x/dev/my.project/.worktrees/repo/branch' }))
  on('env.get', () => ({ value: '/home' }))
  on('fs.read', ($, e) => {
    k.read.push(e.path)
    if (rows instanceof Error) throw rows
    return { value: JSON.stringify(rows) }
  })
  on('process.run', ($, e) => (k.runs.push({ argv: e.argv, init: e.init }), { value: { exitCode: 0, stdout: '', stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }))
  on('ui.render', ($, e) => {
    k.engineText = (e.props as { text: string }).text
    const { Text } = $.ui.resolve(e)

    return <Text>engine</Text>
  })
  return k
}

const mount = ($: Engine, text: string, extra: Record<string, unknown> = {}) =>
  $.ui.mount({ ...MESSAGE, props: { isFirstOfReply: true, text, ...extra } } as never)

test('an id in inline code of a sidebar session links, and a click opens it from the sidebar', async ($, on) => {
  const k = engine(on)
  const ui = await mount($, 'Worker `694b33c4` is done.')
  const md = await ui.find({ key: 'session-links' })
  expect(md?.props.text).toBe(`Worker [\`694b33c4\`](${HREF}) is done.`)
  expect(md?.props.pressableLinks).toEqual([HREF])
  expect(k.read).toEqual(['/home/.claude/projects/-Users-x-dev-my-project/board/tree-rows.json'])
  await ui.press({ key: 'session-links', link: { href: HREF } })
  expect(k.runs).toEqual([{
    argv: ['uv', 'run', '--no-project', expect.stringMatching(/\/hooks\/board\.py$/), 'open-session', SID],
    init: { cwd: '/Users/x/dev/my.project/.worktrees/repo/branch', env: { CLAUDE_PROJECT_DIR: '/Users/x/dev/my.project/.worktrees/repo/branch' }, timeoutMs: 120_000 },
  }])
  await ui.unmount()
})

test('a bare id and a full id link too', async ($, on) => {
  engine(on)
  const ui = await mount($, `694b33c4 and ${SID}.`)
  expect((await ui.find({ key: 'session-links' }))?.props.text).toBe(`[694b33c4](${HREF}) and [${SID}](${HREF}).`)
  await ui.unmount()
})

test('ids no sidebar row names, and ids in code, links and URLs, stay as the engine draws them', async ($, on) => {
  const k = engine(on)
  const text = 'deadbeef, `claude attach 694b33c4`, [694b33c4](https://x.dev/694b33c4), https://x.dev/694b33c4 and\n```\n694b33c4\n```'
  const ui = await mount($, text)
  expect(await ui.find({ key: 'session-links' })).toBeUndefined()
  expect(k.engineText).toBe(text)
  await ui.unmount()
})

test('with no click map the engine draws the reply', async ($, on) => {
  const k = engine(on, new Error('ENOENT'))
  const ui = await mount($, '`694b33c4`')
  expect(k.engineText).toBe('`694b33c4`')
  await ui.unmount()
})

test('a summary is left to the engine', async ($, on) => {
  const k = engine(on)
  const ui = await mount($, '`694b33c4` done', { isSummary: true })
  expect(k.engineText).toBe('`694b33c4` done')
  await ui.unmount()
})

test('a Jira key in the same reply still links', { options: { jiraUrl: 'https://example.atlassian.net', jiraProjectKeys: ['PROJ'] } }, async ($, on) => {
  engine(on)
  const ui = await mount($, 'PROJ-1 on `694b33c4`')
  expect((await ui.find({ key: 'session-links' }))?.props.text)
    .toBe(`[PROJ-1](https://example.atlassian.net/browse/PROJ-1) on [\`694b33c4\`](${HREF})`)
  await ui.unmount()
})
