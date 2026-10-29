import { expect, test, type Engine } from 'claude-code/testing'
import type { On } from 'claude-code'

const MESSAGE = {
  plugin: 'workstreams',
  component: 'AssistantMessage',
  requestId: 'm1',
  surface: 'terminal',
} as const

const OPTIONS = { options: { jiraUrl: 'https://example.atlassian.net/', jiraProjectKeys: ['PROJ', 'OPS'] } }
const LINK = '[PROJ-8541](https://example.atlassian.net/browse/PROJ-8541)'

// The hook beneath the plugin stands for the engine: it records the props the plugin passed down.
async function draw($: Engine, on: On, props: Record<string, unknown>) {
  let seen: Record<string, unknown> = {}
  on('ui.render', ($, e) => {
    seen = e.props as Record<string, unknown>
    const { Text } = $.ui.resolve(e)

    return <Text>drawn</Text>
  })
  const ui = await $.ui.mount({ ...MESSAGE, props: { isFirstOfReply: true, ...props } } as never)
  await ui.unmount()

  return seen
}

const textAfter = async ($: Engine, on: On, text: string) => (await draw($, on, { text })).text

test('a configured key links', OPTIONS, async ($, on) => {
  expect(await textAfter($, on, 'See PROJ-8541 now')).toBe(`See ${LINK} now`)
})

test('a key of a project not configured stays as written', OPTIONS, async ($, on) => {
  expect(await textAfter($, on, 'OPS-3 and DOCS-4')).toBe('[OPS-3](https://example.atlassian.net/browse/OPS-3) and DOCS-4')
})

test('a key in inline code is untouched', OPTIONS, async ($, on) => {
  const text = 'Run `grep PROJ-8541` here'
  expect(await textAfter($, on, text)).toBe(text)
})

test('a key in a fenced block is untouched', OPTIONS, async ($, on) => {
  const text = 'a\n```\nPROJ-8541\n```\nb'
  expect(await textAfter($, on, text)).toBe(text)
})

test('a key in an existing link is untouched', OPTIONS, async ($, on) => {
  const text = '[PROJ-8541](https://example.com/PROJ-8541)'
  expect(await textAfter($, on, text)).toBe(text)
})

test('a key in a URL is untouched', OPTIONS, async ($, on) => {
  const text = 'https://example.atlassian.net/browse/PROJ-8541 and https://github.com/o/r/pull/PROJ-1'
  expect(await textAfter($, on, text)).toBe(text)
})

test('UTF-8 and SHA-256 are untouched', OPTIONS, async ($, on) => {
  const text = 'UTF-8, SHA-256 and ISO-8601'
  expect(await textAfter($, on, text)).toBe(text)
})

test('isSummary passes through', OPTIONS, async ($, on) => {
  const props = await draw($, on, { text: 'OPS-12 done', isSummary: true })
  expect(props.isSummary).toBe(true)
  expect(props.text).toBe('[OPS-12](https://example.atlassian.net/browse/OPS-12) done')
})

test('with nothing configured the text is drawn as written', async ($, on) => {
  expect(await textAfter($, on, 'See PROJ-8541 now')).toBe('See PROJ-8541 now')
})

test('keys given as one string link too', { options: { jiraUrl: 'https://example.atlassian.net', jiraProjectKeys: 'PROJ, OPS' } }, async ($, on) => {
  expect(await textAfter($, on, 'OPS-1')).toBe('[OPS-1](https://example.atlassian.net/browse/OPS-1)')
})

test('a key that is not a project key shape is ignored', { options: { jiraUrl: 'https://example.atlassian.net', jiraProjectKeys: ['.*', 'PROJ'] } }, async ($, on) => {
  expect(await textAfter($, on, 'PROJ-1 and x-2')).toBe('[PROJ-1](https://example.atlassian.net/browse/PROJ-1) and x-2')
})

const REPOS = { options: { githubRepos: ['octo-org/waypoints', 'octo-org/triage-lite', 'me/my.repo'] } }
const ref = (repo: string, n: number, owner = 'octo-org') => `[${repo} #${n}](https://github.com/${owner}/${repo}/issues/${n})`

test('a listed repo ref links to its issue, which GitHub redirects for a PR', REPOS, async ($, on) => {
  expect(await textAfter($, on, 'See waypoints #197 and triage-lite #2.')).toBe(`See ${ref('waypoints', 197)} and ${ref('triage-lite', 2)}.`)
})

test('a ref without the space links too and keeps its text', REPOS, async ($, on) => {
  expect(await textAfter($, on, 'waypoints#267 (graph loader fixes)')).toBe(
    '[waypoints#267](https://github.com/octo-org/waypoints/issues/267) (graph loader fixes)',
  )
})

test('a word that is not a listed repo stays as written', REPOS, async ($, on) => {
  const text = 'issue #193, PR #198, milestone #6, goal #38 and bare #197'
  expect(await textAfter($, on, text)).toBe(text)
})

test('a ref in code, a link, a URL or a longer token is untouched', REPOS, async ($, on) => {
  const text = '`waypoints #1`, [waypoints #2](https://x.test), https://x.test/waypoints #3x, my-waypoints #4 and waypoints #5a'
  expect(await textAfter($, on, text)).toBe(text)
})

test('a dot in a repo name matches only a dot', REPOS, async ($, on) => {
  expect(await textAfter($, on, 'my.repo #3 and myXrepo #4')).toBe(`${ref('my.repo', 3, 'me')} and myXrepo #4`)
})

test('repos given as one string link too, and an entry without an owner is ignored', { options: { githubRepos: 'waypoints, octo-org/triage-lite' } }, async ($, on) => {
  expect(await textAfter($, on, 'waypoints #1 and triage-lite #2')).toBe(`waypoints #1 and ${ref('triage-lite', 2)}`)
})

test('Jira keys and repo refs link together', { options: { ...OPTIONS.options, ...REPOS.options } }, async ($, on) => {
  expect(await textAfter($, on, 'PROJ-8541 via waypoints #197')).toBe(`${LINK} via ${ref('waypoints', 197)}`)
})
