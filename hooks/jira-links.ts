import type { On, PluginOptions } from 'claude-code'

// Jira issue keys in the model's replies, drawn as links to their issues. The drawing alone changes: the stored message and the
// model's context keep the bare key.

// Spans that must stay as written come first in the alternation; a key outside them is the last alternative. The lookbehind keeps
// a key inside a URL path or a longer token (`…/browse/PROJ-1`, `X-PROJ-1`) from matching.
const spans = (keys: readonly string[]) =>
  new RegExp(
    [
      String.raw`(?:\`{3,}|~{3,})[\s\S]*?(?:\`{3,}|~{3,}|$)`, // fenced block
      String.raw`(\`+)[^\n]*?\1`, // inline code
      String.raw`\[[^\]]*\]\([^)]*\)`, // markdown link
      String.raw`<[^>\s]*>`, // autolink
      String.raw`https?://[^\s<>)\]]*`, // bare URL
      String.raw`(?<![\w/.-])(?<key>(?:${keys.join('|')})-\d+)\b`,
    ].join('|'),
    'g',
  )

export const linkify = (text: string, pattern: RegExp, browse: string): string =>
  text.replace(pattern, (...args) => {
    const key = (args.at(-1) as { key?: string }).key
    return key === undefined ? (args[0] as string) : `[${key}](${browse}${key})`
  })

// With no site or no keys configured it hooks nothing. Keys come as a list, or as one string with commas or spaces between them.
// A key that is not a Jira project key's shape is dropped, so it never reaches the pattern as regex syntax.
export function jiraLinks(on: On, options: PluginOptions): void {
  const site = String(options.jiraUrl ?? '').trim().replace(/\/+$/, '')
  const given = options.jiraProjectKeys
  const keys = (Array.isArray(given) ? given : String(given ?? '').split(/[\s,]+/))
    .map(k => String(k).trim())
    .filter(k => /^[A-Z][A-Z0-9_]*$/.test(k))
  if (!site || keys.length === 0) return
  const pattern = spans(keys)
  const browse = `${site}/browse/`

  on('ui.render', { component: 'AssistantMessage' }, ($, e, next) =>
    next({ ...e, props: { ...e.props, text: linkify(e.props.text, pattern, browse) } }),
  )
}
