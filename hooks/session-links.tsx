import type { EngineInterface, On } from 'claude-code'

// Session ids in the model's replies that name a row of the board's sidebar, drawn as links: a click opens the row's session in the
// right pane, as a click on the row does. Only a Markdown the mod draws itself hears a click, so such a block is drawn by the mod,
// with the engine's reply bullet, and every other block as the engine draws it.

const ID = '[0-9a-f]{8}(?:-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})?'
const WHOLE = new RegExp(`^${ID}$`)
// A link the surface would open on a modified click: .invalid names no host, so that opens nothing.
const HREF = 'https://workstreams.invalid/session/'

// As jira-links.ts reads a reply, except that an inline code span holding only an id links too: most ids are written as one.
const SPANS = new RegExp(
  [
    String.raw`(?:\`{3,}|~{3,})[\s\S]*?(?:\`{3,}|~{3,}|$)`, // fenced block
    String.raw`(\`+)(?<code>[^\n]*?)\1`, // inline code
    String.raw`\[[^\]]*\]\([^)]*\)`, // markdown link
    String.raw`<[^>\s]*>`, // autolink
    String.raw`https?://[^\s<>)\]]*`, // bare URL
    String.raw`(?<![\w/.-])(?<id>${ID})(?![\w-])`,
  ].join('|'),
  'g',
)

// The text with each id of a session in `sessions` (full ids) linked, and the links it made.
export function linkify(text: string, sessions: readonly string[]): { text: string; hrefs: string[] } {
  const hrefs = new Set<string>()
  const out = text.replace(SPANS, (span, ...args) => {
    const { code, id } = args.at(-1) as { code?: string; id?: string }
    const token = id ?? code?.trim() ?? ''
    const sid = WHOLE.test(token) ? sessions.find(s => s === token || (token.length === 8 && s.startsWith(token))) : undefined
    if (sid === undefined) return span
    hrefs.add(HREF + sid)
    return `[${span}](${HREF}${sid})`
  })
  return { text: out, hrefs: [...hrefs] }
}

// The session ids of the sidebar's rows, from the click map board.py's watch writes, where board.py's board_dir() keeps it.
async function onBoard($: EngineInterface): Promise<string[]> {
  try {
    const project = (await $.session.cwd()).replace(/\/\.worktrees\/.*$/, '').replace(/[^A-Za-z0-9-]/g, '-')
    const rows = JSON.parse(await $.fs.read(`${await $.env.get('HOME')}/.claude/projects/${project}/board/tree-rows.json`)) as Record<string, { session_id?: string | null; goal?: unknown; pin?: unknown }>
    return Object.values(rows).flatMap(e => (e.session_id && e.goal === undefined && e.pin === undefined ? [e.session_id] : []))
  } catch {
    return []   // no sidebar has run for this project
  }
}

async function open($: EngineInterface, sid: string): Promise<void> {
  try {
    const cwd = await $.session.cwd()
    const run = await $.process.run(['uv', 'run', '--no-project', `${$.plugin.root}/hooks/board.py`, 'open-session', sid], { cwd, env: { CLAUDE_PROJECT_DIR: cwd }, timeoutMs: 120_000 })
    if (run.exitCode !== 0) throw new Error(`board.py exited ${run.exitCode}: ${run.stderr.trim().split('\n').at(-1) ?? ''}`)
  } catch (err) {
    $.ui.log(`session ${sid} not opened: ${err instanceof Error ? err.message : String(err)}`, { to: 'debug' })
  }
}

// A summary block is left to the engine, which marks it as one.
export function sessionLinks(on: On): void {
  on('ui.render', { component: 'AssistantMessage' }, async ($, e, next) => {
    if (e.props.isSummary) return next(e)
    const { text, hrefs } = linkify(e.props.text, await onBoard($))
    if (hrefs.length === 0) return next(e)
    const { Box, Text, Markdown } = $.ui.resolve(e)

    return (
      <Box flexDirection="row">
        <Box minWidth={2} flexShrink={0}><Text>{e.props.isFirstOfReply ? '⏺' : ''}</Text></Box>
        <Box flexGrow={1} flexShrink={1}>
          <Markdown key="session-links" text={text} pressableLinks={hrefs} onLinkPress={link => void open($, link.href.slice(HREF.length))} />
        </Box>
      </Box>
    )
  })
}
