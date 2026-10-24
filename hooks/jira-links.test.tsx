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
