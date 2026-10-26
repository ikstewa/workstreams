import type { On, PluginOptions } from 'claude-code'

// Jira issue keys and GitHub refs (`repo #12`) in the model's replies, drawn as links to their issues. The drawing alone changes:
// the stored message and the model's context keep the bare text.

// Spans that must stay as written come first in the alternation; a key or ref outside them is one of the last alternatives. The
// lookbehind keeps one inside a URL path or a longer token (`…/browse/PROJ-1`, `X-PROJ-1`, `my-repo #1`) from matching.
const spans = (keys: readonly string[], repos: readonly string[]) =>
  new RegExp(
    [
      String.raw`(?:\`{3,}|~{3,})[\s\S]*?(?:\`{3,}|~{3,}|$)`, // fenced block
      String.raw`(\`+)[^\n]*?\1`, // inline code
      String.raw`\[[^\]]*\]\([^)]*\)`, // markdown link
      String.raw`<[^>\s]*>`, // autolink
      String.raw`https?://[^\s<>)\]]*`, // bare URL
      ...(keys.length ? [String.raw`(?<![\w/.-])(?<key>(?:${keys.join('|')})-\d+)\b`] : []),
      ...(repos.length ? [String.raw`(?<![\w/.-])(?<repo>${repos.map(r => r.replaceAll('.', '\\.')).join('|')}) #(?<num>\d+)\b`] : []),
    ].join('|'),
    'g',
  )

const listed = (given: unknown) => (Array.isArray(given) ? given : String(given ?? '').split(/[\s,]+/)).map(k => String(k).trim())

// It hooks nothing until Jira (a site and keys) or GitHub repos are configured. Each list comes as a list, or as one string with
// commas or spaces between them. An entry not of its expected shape is dropped, so it never reaches the pattern as regex syntax.
// Only a listed repo's name links, so `issue #193` or `PR #198` stays as written.
export function issueLinks(on: On, options: PluginOptions): void {
  const site = String(options.jiraUrl ?? '').trim().replace(/\/+$/, '')
  const keys = site ? listed(options.jiraProjectKeys).filter(k => /^[A-Z][A-Z0-9_]*$/.test(k)) : []
  const owners = new Map(
    listed(options.githubRepos)
      .filter(r => /^[\w.-]+\/[\w.-]+$/.test(r))
      .map(r => r.split('/').reverse() as [string, string]),
  )
  if (keys.length === 0 && owners.size === 0) return
  const pattern = spans(keys, [...owners.keys()])

  on('ui.render', { component: 'AssistantMessage' }, ($, e, next) => {
    const text = e.props.text.replace(pattern, (...args) => {
      const { key, repo, num } = args.at(-1) as { key?: string; repo?: string; num?: string }
      if (key !== undefined) return `[${key}](${site}/browse/${key})`
      if (repo !== undefined) return `[${repo} #${num}](https://github.com/${owners.get(repo)}/${repo}/issues/${num})`
      return args[0] as string
    })
    return next({ ...e, props: { ...e.props, text } })
  })
}
