import type { ApiMessage, EngineInterface, Register, ToolCallResult, ToolSpec } from 'claude-code'

// A bound session's charter, as a user row of its conversation; the charter tool, its one way to write that charter; its goals kept in
// step with its task list; and every session's board record. board.py builds the text, makes the writes and keeps the record's state
// machine; this module decides when each runs and forwards the events the record moves on.
// Every $-taking helper is a top-level function: the validator lets $ into no other.

const HEAD = '# Workstream charter ('
const TOOL = 'mcp__workstreams__charter'

// What the model reads to learn the tool: BOUND_RULES in board.py say when to write, this says how.
const CHARTER: ToolSpec = {
  name: 'charter',
  // One line per paragraph and per field: a source line wrapped inside one would reach the model as a break mid-sentence.
  description: [
    'Writes the charter of your workstream. The charter is the file named in the header of the charter row in your conversation. ' +
      'Use this tool for every change to the charter. Do not edit the charter file yourself.',
    '',
    'Give one or more of the fields below. One call can carry several fields. The tool applies them in the order below. ' +
      'The result has one line for each change, then the number of open goals. If the tool refuses a call, the error says why.',
    '- focus: Replaces the focus line. Say what the workstream is on now: the progress of the plan, the work in flight, and the next ' +
      'step. The tool changes each newline to a space.',
    '- record: Adds one entry at the end of the record, as a new top-level bullet. The tool puts "- " and a bold date, "**MM-DD** ", ' +
      'in front of the first line. If the first line starts with a bold date of your own, such as **10-05, Ian: "go".**, the tool adds ' +
      'no date. Put each nested bullet on a new line, indented by two spaces.',
    '- add_goals: Adds each text as an open goal at the end of the goals list. The tool also adds a task for each new goal to your task ' +
      'list. The tool skips a goal that the list already has, open or ticked.',
    '- block: Sets blocked: to the thing that the work waits on. Use it only for a wait outside this session and not on Ian, such as a ' +
      'review, feedback or a merge. The tool also sets blocked_since: to the time now. If you set a block again, its age starts again.',
    '- clear_block: true removes blocked: and blocked_since:. Do not send clear_block and block in the same call.',
    '',
    'This tool does not tick or untick a goal. To tick a goal, complete its task with TaskUpdate. This tool cannot change note:, which ' +
      'is Ian\'s line. It cannot change any other line of the charter. Only the main session can use this tool. A sub-agent cannot.',
  ].join('\n'),
  inputSchema: {
    type: 'object',
    properties: {
      focus: { type: 'string', description: 'The new focus, as one line: progress, in flight, next.' },
      record: { type: 'string', description: 'One record entry. The tool adds the bullet and the date.' },
      add_goals: { type: 'array', items: { type: 'string' }, description: 'New open goals, one text each.' },
      block: { type: 'string', description: 'What the work waits on, outside this session and not on Ian.' },
      clear_block: { type: 'boolean', description: 'true removes the block.' },
    },
    additionalProperties: false,
  },
}

type Charter = { registry?: string; name?: string | null; key?: string; text?: string }

// What `board.py event` applies to the record, each with what it carries.
type Board =
  | { event: 'start' | 'turn.start' | 'answered' }
  | { event: 'turn.complete'; reason: string; answer: string }
  | { event: 'ask'; kind: 'permission' | 'question' }
  | { event: 'child.start'; agent_id: string; name: string }
  | { event: 'child.stop'; agent_id: string }
  | { event: 'end'; reason: string }

// One board.py run, `board.py <cmd> <session id> [...args]`, its stdin built from the session's cwd as it starts; done hands what it
// printed, or why it failed, to a caller that waits for it. `what` leads its debug line when it fails.
type Job = { cmd: 'event' | 'write' | 'mirror' | 'tick'; args?: string[]; stdin?: (cwd: string) => string; sid?: string; timeoutMs: number; what: string; done: (out: Out) => void }
type Out = { stdout: string } | { failed: string }

// The registry file and name the last board.py read reported, and what is owed at the next prompt or main-loop model request.
// restart: the new session id of a /clear or a /resume owes its record a start. asks: calls that went to the mode's decider and
// have not returned. kids: the sub-agents whose run the record shows. queue: board.py runs not yet made; draining: one makes them.
type State = {
  registry?: string
  name?: string | null
  owed?: 'full' | 'start'
  restart?: boolean
  asks: Set<string>
  kids: Set<string>
  queue: Job[]
  draining: boolean
}

// How often, and how many times, to look for the session id a /clear or a /resume goes on under: 5 s in all. The live check saw
// the new id's settings SessionStart 0.12 s after session.end.
const FOLLOW_MS = 50
const FOLLOW_TRIES = 100

// Read in the API form: the rows form leaves out meta rows, and the charter row is one. There it is a text block of a user
// message, among the reminders and the prompt merged into it. The declarations say content is always blocks; a string is taken too.
const holdsCharter = (m: ApiMessage) => {
  const blocks = typeof m.content === 'string' ? [{ type: 'text', text: m.content }] : m.content
  return m.role === 'user' && blocks.some(b => b.type === 'text' && String(b.text).startsWith(HEAD))
}

const reason = (err: unknown) => (err instanceof Error ? err.message : String(err))
const exited = (run: { exitCode: number; stderr: string }) => new Error(`board.py exited ${run.exitCode}: ${run.stderr.trim().split('\n').at(-1) ?? ''}`)

async function inject($: EngineInterface, st: State, mode: 'full' | 'refresh'): Promise<void> {
  let key: string | undefined
  try {
    const cwd = await $.session.cwd()
    const argv = ['uv', 'run', '--no-project', `${$.plugin.root}/hooks/board.py`, 'charter', await $.session.id()]
    // CLAUDE_PROJECT_DIR set to the cwd, as a settings hook started there would see it: an inherited one could name another project.
    const run = await $.process.run(mode === 'refresh' ? [...argv, '--refresh'] : argv, { cwd, env: { CLAUDE_PROJECT_DIR: cwd }, timeoutMs: 10_000 })
    if (run.exitCode !== 0) throw exited(run)
    const out = JSON.parse(run.stdout) as Charter
    if (out.registry) Object.assign(st, { registry: out.registry, name: out.name ?? null })
    if (!out.text) return
    key = out.key
    await offer($)
    const kept = await $.session.append({ message: { type: 'user', content: [{ type: 'text', text: out.text }] } })
    if (kept.deny !== undefined) throw new Error(`append refused: ${kept.deny}`)
  } catch (err) {
    $.ui.log(`charter${key ? ` of ${key}` : ''} not appended (${mode}): ${reason(err)}`, { to: 'debug' })
  }
}

// The charter tool, for a session board.py has just found bound. Registering it again replaces it with itself, so every bound read
// registers it, a /clear's or a /rename's included; an unbound session never has it.
async function offer($: EngineInterface): Promise<void> {
  try {
    await $.tool.register(CHARTER)
  } catch (err) {
    $.ui.log(`charter tool not registered: ${reason(err)}`, { to: 'debug' })
  }
}

// Queues a board.py run; it resolves with what the run printed, or why it failed. One queue, run one at a time in the order queued,
// since two runs at once would each write the record or the charter over the other's change. It runs from a $.clock callback, so no
// hook's dispatch owns the run: one abandoned (an Esc at a dialog, an interrupt) cannot abort it.
function queue($: EngineInterface, st: State, job: Omit<Job, 'done'>): Promise<Out> {
  return new Promise<Out>(done => {
    st.queue.push({ ...job, done })
    // Each run asks for a drain while none runs, so a timer that never fires costs only the wait for the next run.
    if (!st.draining) $.clock.after(0, () => void drain($, st))
  })
}

// A board.py event the record moves on. No hook but session.end waits for one.
function record($: EngineInterface, st: State, ev: Board, sid?: string, timeoutMs = 10_000): Promise<Out> {
  return queue($, st, { cmd: 'event', stdin: cwd => JSON.stringify({ ...ev, cwd }), sid, timeoutMs, what: `board ${ev.event} not recorded` })
}

// The task list from the charter's goals, for the session that `sid` names or the one running.
function mirror($: EngineInterface, st: State, sid?: string): Promise<Out> {
  return queue($, st, { cmd: 'mirror', sid, timeoutMs: 10_000, what: 'goals not mirrored' })
}

// ponytail: a run still queued when a /clear moves the session to its new id is made under the new one; a queued run waits
// milliseconds, and a /clear never comes mid-turn
async function drain($: EngineInterface, st: State): Promise<void> {
  if (st.draining) return
  st.draining = true
  for (let job = st.queue.shift(); job; job = st.queue.shift()) {
    let out: Out = { failed: 'not run' }
    try {
      const cwd = await $.session.cwd()
      const id = job.sid ?? (await $.session.id())
      const run = await $.process.run(['uv', 'run', '--no-project', `${$.plugin.root}/hooks/board.py`, job.cmd, id, ...(job.args ?? [])],
        { cwd, env: { CLAUDE_PROJECT_DIR: cwd }, ...(job.stdin && { stdin: job.stdin(cwd) }), timeoutMs: job.timeoutMs })
      if (run.exitCode !== 0) throw exited(run)
      out = { stdout: run.stdout }
    } catch (err) {
      out = { failed: reason(err) }
      $.ui.log(`${job.what}: ${out.failed}`, { to: 'debug' })
    } finally {
      job.done(out)
    }
  }
  st.draining = false
}

// What a write printed, as the tool's answer: its summary, or a refusal the model reads as an error.
function answer(out: Out): ToolCallResult {
  if ('failed' in out) return { deny: `the charter was not written: ${out.failed}` }
  try {
    const said = JSON.parse(out.stdout) as { ok?: boolean; summary?: string; error?: string }
    if (said.ok === true) return { result: said.summary ?? '' }
    return { deny: said.error ?? 'board.py answered neither ok nor error' }
  } catch (err) {
    return { deny: `the charter write answered no JSON: ${reason(err)}` }
  }
}

// The record starts over with the session, and the sub-agents and waits this module tracks go with the run they belonged to.
function begin($: EngineInterface, st: State, sid?: string): Promise<Out> {
  st.kids.clear()
  st.asks.clear()
  return record($, st, { event: 'start' }, sid)
}

// No event fires on the id a /clear or a /resume goes on under, so look for it from $.clock, which outlives session.end's dispatch,
// and start its record once it shows. A prompt that comes first starts it instead, and so does the next prompt after the last look.
async function follow($: EngineInterface, st: State, ended: string, tries: number): Promise<void> {
  let id: string | undefined
  try { id = await $.session.id() } catch {}
  if (!st.restart) return
  if (id !== undefined && id !== ended) {
    st.restart = false
    void begin($, st, id)
    void mirror($, st, id)
  } else if (tries > 1) $.clock.after(FOLLOW_MS, () => void follow($, st, ended, tries - 1))
}

// A sub-agent shows from its run's first model request. Only an agent the session lists is one: the engine's own forks (compaction,
// memory) make requests under ids no list names. Its name is its type, as SubagentStart's agent_type was.
async function child($: EngineInterface, st: State, agentId: string): Promise<void> {
  let type: string | undefined
  try {
    type = (await $.agent.list()).find(a => a.id === agentId)?.type
  } catch (err) {
    $.ui.log(`agents not listed: ${reason(err)}`, { to: 'debug' })
  }
  if (type === undefined) return
  st.kids.add(agentId)
  void record($, st, { event: 'child.start', agent_id: agentId, name: type })
}

// A transcript that already holds a charter row is a resume, a reopen or a reload: the session keeps the record, so only the refresh.
async function start($: EngineInterface, st: State): Promise<void> {
  let held = false
  try { held = (await $.session.messages({ as: 'api' })).some(holdsCharter) } catch {}
  await inject($, st, held ? 'refresh' : 'full')
}

async function settle($: EngineInterface, st: State): Promise<void> {
  const owed = st.owed
  st.owed = undefined
  await (owed === 'full' ? inject($, st, 'full') : start($, st))
}

// A /rename shows as a new name in the session's registry file, read in process; board.py runs only once it changed.
async function renamed($: EngineInterface, st: State): Promise<boolean> {
  if (!st.registry) return false
  try {
    const name = (JSON.parse(await $.fs.read(st.registry)) as { name?: string | null }).name ?? null
    if (name === st.name) return false
    st.name = name
    await inject($, st, 'full')
    return true
  } catch (err) {
    $.ui.log(`registry not read: ${reason(err)}`, { to: 'debug' })
    return false
  }
}

export const register: Register = on => {
  const st: State = { asks: new Set(), kids: new Set(), queue: [], draining: false }

  // The goals are mirrored at a start, at each prompt and at a main-loop turn's end, as the settings hooks did before the mod.
  on('session.start', async ($, e, next) => {
    const r = await next(e)
    void begin($, st)
    void mirror($, st)
    await start($, st)
    // No registry file named the session yet: look once more at the first prompt.
    if (!st.registry) st.owed ??= 'start'
    return r
  })

  // No session.start follows a /clear, which starts an empty conversation, or a /resume or /branch (reason resume), which load a held one.
  on('session.end', async ($, e, next) => {
    if (e.reason === 'clear') st.owed = 'full'
    else if (e.reason === 'resume') st.owed = 'start'
    if (e.reason === 'clear' || e.reason === 'resume') st.restart = true
    const r = await next(e)
    // The ending id, not the one the process goes on under. The one hook that waits for its run: an exit ends the process after this
    // chain, which shares one short bound.
    const ended = record($, st, { event: 'end', reason: e.reason }, e.sessionId, Math.max(100, Math.min(10_000, next.budget.remainingMs)))
    if (e.reason === 'clear' || e.reason === 'resume') $.clock.after(FOLLOW_MS, () => void follow($, st, e.sessionId, FOLLOW_TRIES))
    await ended
    return r
  })

  // Ahead of next(e), so the row lands ahead of the prompt and the record's start is queued ahead of the turn's. A prompt typed over
  // a running turn starts no record over: the turn's next event brings a new name from the registry anyway.
  on('prompt.submit', async ($, e, next) => {
    let named = false
    if (st.owed) await settle($, st)
    else named = await renamed($, st)
    if ((st.restart || named) && e.turnId === undefined) {
      st.restart = false
      void begin($, st)
    }
    void mirror($, st)
    return next(e)
  })

  // Any hook may still rewrite a compaction's messages on the way up, so the conversation becomes them only after this chain returns:
  // a row appended inside it would join the conversation being replaced. The full row is owed instead, and paid at the next prompt
  // or main-loop model request, whichever comes first. A precompute installs nothing.
  // ponytail: the charter rows are summarized with the rest: e.messages is declared in the rows form, which leaves meta rows out
  // (measured on $.session.messages()), and the event offers no other form to drop them from
  on('session.compact', async ($, e, next) => {
    const r = await next(e)
    if (e.agentId === undefined && r.messages && e.trigger !== 'precompute') st.owed = 'full'
    return r
  })

  // Every main-loop turn, a prompt's or one begun without one (a task notification, a peer's message, a sub-agent's handback).
  on('turn.start', async ($, e, next) => {
    const r = await next(e)
    void record($, st, { event: 'turn.start' })
    return r
  })

  on('turn.step', async function* ($, e, next) {
    if (e.agentId !== undefined && !st.kids.has(e.agentId)) await child($, st, e.agentId)
    if (st.owed && e.agentId === undefined) await settle($, st)
    return yield* next(e)
  })

  // A sub-agent's run raises no turn.start, and its turn.complete carries its id.
  on('turn.complete', async ($, e, next) => {
    const r = await next(e)
    if (e.agentId === undefined) {
      st.asks.clear()   // the turn's end ends the record's wait: a call still out returns to none
      void record($, st, { event: 'turn.complete', reason: e.reason, answer: e.answer })
      void mirror($, st)
    } else if (st.kids.delete(e.agentId)) void record($, st, { event: 'child.stop', agent_id: e.agentId })
    return r
  })

  // The wait shows the moment a call goes to the mode's decider, a dialog or the auto-mode classifier, which then reads as a dialog
  // that answered itself. A query ($.tool.check) carries no call id and asks no one.
  on('tool.check', async ($, e, next) => {
    const r = await next(e)
    if (r.decision === 'ask' && e.tool_use_id !== undefined) {
      st.asks.add(e.tool_use_id)
      void record($, st, { event: 'ask', kind: e.tool === 'AskUserQuestion' ? 'question' : 'permission' })
    }
    return r
  })

  // A call that asked returns once it is answered: run, refused, or Esc at the dialog. The wait ends when no such call is still out.
  on('tool.call', async ($, e, next) => {
    const r = await next(e)
    if (e.tool_use_id !== undefined && st.asks.delete(e.tool_use_id) && st.asks.size === 0) void record($, st, { event: 'answered' })
    return r
  })

  // The charter tool, answered here so core never runs it: no check, no dialog. The write waits on the one queue, behind the record's
  // events, and the call waits for that write alone. That wait is no $ call, so it counts against the hook's 10 s budget, past which
  // core would answer the call with a failure of its own: the write's run stops at 8 s, so a timeout still reaches the model as its why.
  // ponytail: runs queued ahead of the write count against the same budget; each takes a fraction of a second
  on('tool.call', { tool: TOOL }, async ($, e) => {
    if (e.agentId !== undefined) return { deny: 'only the main session writes the charter' }
    const fields = { focus: e.focus, record: e.record, add_goals: e.add_goals, block: e.block, clear_block: e.clear_block }
    return answer(await queue($, st, { cmd: 'write', stdin: () => JSON.stringify(fields), timeoutMs: 8_000, what: 'charter not written' }))
  })

  // A goal task's status onto its goal once the update has run, queued, so two updates in parallel tick one after the other.
  on('tool.call', { tool: 'TaskUpdate' }, async ($, e, next) => {
    const r = await next(e)
    if (r.deny === undefined && r.isError !== true) void queue($, st, { cmd: 'tick', args: [e.taskId], timeoutMs: 10_000, what: `goal of task ${e.taskId} not ticked` })
    return r
  })
}
