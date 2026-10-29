import type { On, PluginOptions } from 'claude-code'

// Jira issue keys and GitHub refs (`owner/repo#12`) in the model's replies, drawn as links to their issues. The drawing alone
// changes: the stored message and the model's context keep the bare text.

// Spans that must stay as written come first in the alternation; a key or ref outside them is one of the last alternatives. The
// lookbehind keeps one inside a URL path or a longer token (`…/browse/PROJ-1`, `X-PROJ-1`, `a/b/c#1`) from matching.
const spans = (keys: readonly string[]) =>
  new RegExp(
    [
      String.raw`(?:\`{3,}|~{3,})[\s\S]*?(?:\`{3,}|~{3,}|$)`, // fenced block
      String.raw`(\`+)[^\n]*?\1`, // inline code
      String.raw`\[[^\]]*\]\([^)]*\)`, // markdown link
      String.raw`<[^>\s]*>`, // autolink
      String.raw`https?://[^\s<>)\]]*`, // bare URL
      ...(keys.length ? [String.raw`(?<![\w/.-])(?<key>(?:${keys.join('|')})-\d+)\b`] : []),
      String.raw`(?<![\w/.-])(?<ref>[A-Za-z0-9-]+/[\w.-]+)#(?<num>\d+)\b`,
    ].join('|'),
    'g',
  )

const listed = (given: unknown) => (Array.isArray(given) ? given : String(given ?? '').split(/[\s,]+/)).map(k => String(k).trim())

// Jira keys link once a site and keys are configured. Keys come as a list, or as one string with commas or spaces between them; one
// not of a project key's shape is dropped, so it never reaches the pattern as regex syntax. A GitHub ref names its owner, so it
// needs no configuration, and a bare `#197` or `repo #197` stays as written.
export function issueLinks(on: On, options: PluginOptions): void {
  const site = String(options.jiraUrl ?? '').trim().replace(/\/+$/, '')
  const keys = site ? listed(options.jiraProjectKeys).filter(k => /^[A-Z][A-Z0-9_]*$/.test(k)) : []
  const pattern = spans(keys)

  on('ui.render', { component: 'AssistantMessage' }, ($, e, next) => {
    const text = e.props.text.replace(pattern, (...args) => {
      const { key, ref, num } = args.at(-1) as { key?: string; ref?: string; num?: string }
      if (key !== undefined) return `[${key}](${site}/browse/${key})`
      if (ref !== undefined) return `[${ref}#${num}](https://github.com/${ref}/issues/${num})`
      return args[0] as string
    })
    return next({ ...e, props: { ...e.props, text } })
  })
}
