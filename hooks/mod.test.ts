import type { EngineInterface, On, PluginOptions, SessionCompactTrigger, SessionEndReason, SessionMessage, ToolSpec, TurnCompleteReason } from 'claude-code'
import { expect, mock, test } from 'claude-code/testing'
import type { Engine } from 'claude-code/testing'
import { register } from './mod'

// The kit in 2.1.289 has no store beneath a plugin's own $.session.append and lets no test hook see that call: it rejects with
// "no implementation for session.append". So the lifecycle tests drive register() through the harness below, whose $ keeps the
// rows; the kit tests after them run the module in the engine for what the kit can show.
// The harness's transcript reads as measured on 2.1.289: the rows form leaves out meta rows, the charter row among them, and the
// API form merges a turn's user rows into one message, the charter one text block after the reminders.

const CWD = '/work'
const REG = '/home/.claude/sessions/4242.json'
const CHARTERED = ['PAYMENTS_API', 'ADOPTED']   // what board.py finds a charter for
const TOOL = 'mcp__workstreams__charter'
const WROTE = JSON.stringify({ ok: true, key: 'PAYMENTS_API', summary: 'focus set\n2 goals left' })   // what board.py write prints

const charterText = (key: string, refresh: boolean) =>
  `# Workstream charter (/ws/${key}.md)\n\n# Operating rules\nRules.\n\n---\nworkstream: ${key}\n---` + (refresh ? '' : '\n\n# Record\nHandoff.')

type Hook = (...args: any[]) => any
type Entry = { role: 'user' | 'assistant'; text: string; meta?: true }
type Fail = 'exit' | 'json' | 'slow' | 'refused'
type Sent = { sid: string; ev: { event: string; [k: string]: unknown } }

const done = (stdout: string, exitCode = 0, stderr = '') => ({ exitCode, stdout, stderr, isStdoutTruncated: false, isStderrTruncated: false })

function board(w: { name: string | null; registryless: boolean; fail?: Fail }, argv: readonly string[]) {
  if (w.fail === 'exit') return done('', 1, 'Traceback (most recent call last):\nValueError: boom\n')
  if (w.fail === 'json') return done('{"text": ')
  if (w.registryless) return done('{}')
  const key = w.name ?? ''
  const bound = CHARTERED.includes(key) && { key, path: `/ws/${key}.md`, text: charterText(key, argv.includes('--refresh')) }
  return done(JSON.stringify({ registry: REG, name: w.name, ...bound }))
}

function harness(name: string | null = 'PAYMENTS_API') {
  const w = {
    sid: 's1',
    name,
    registryless: false,
    unreadable: false,
    fail: undefined as Fail | undefined,
    skip: false,
    strings: false,   // the API form gives a one-block message its text as a string
    transcript: [] as Entry[],
    runs: [] as { argv: readonly string[]; init: unknown }[],   // board.py charter
    rows: [] as string[],
    events: [] as string[],   // rows appended, prompts entered and model steps sent, in order
    logs: [] as { text: string; to: unknown }[],
    summarized: [] as SessionMessage[][],
    sent: [] as Sent[],   // board.py event: the session id and the event on stdin
    order: [] as string[],   // board events, prompts and model steps, in order
    jobs: [] as string[],   // every queued board.py run, events, writes and goal syncs, in the order they ran
    eventInit: [] as unknown[],
    eventFail: undefined as 'exit' | 'slow' | undefined,
    writes: [] as { sid: string; fields: unknown; timeoutMs?: number }[],   // board.py write: the session id, the fields on stdin, its timeout
    wrote: WROTE,
    writeFail: undefined as 'exit' | 'slow' | undefined,
    tools: [] as ToolSpec[],   // $.tool.register, each call
    unregistrable: false,   // $.tool.register rejects
    core: [] as string[],   // tool calls that reached core
    agents: [{ id: 'a1', type: 'Explore', description: 'look', status: 'running' }] as { id: string; type: string; description: string; status: string }[],
    unlisted: false,   // $.agent.list rejects
    budget: 1500,   // what session.end's next.budget leaves
    flying: 0,
    overlap: false,   // two board.py events ran at once
    now: 0,   // the harness's clock, in ms: a timer runs only when tick() reaches it
    timers: [] as { at: number; fn: () => void }[],
    dropTimers: 0,   // how many of the next $.clock.after timers never fire
    inHook: false,   // a hook's dispatch is under way: from the hook's call until a timer of the harness's clock runs
    abandon: false,   // that dispatch is abandoned: a $.process.run it makes rejects, as the engine aborts it
    tick: async (ms = 0) => {},
  }
  // Runs every timer due by now + ms, earliest first, and lets the work they start run until nothing is due and no run is out.
  w.tick = async (ms = 0) => {
    const until = w.now + ms
    for (let idle = 0, i = 0; idle < 200 && i < 20_000; i++) {
      const due = w.timers.filter(t => t.at <= until).sort((a, b) => a.at - b.at)[0]
      if (due) (w.timers.splice(w.timers.indexOf(due), 1), (w.now = Math.max(w.now, due.at)), (w.inHook = false), due.fn())
      idle = due || w.flying > 0 ? 0 : idle + 1
      await Promise.resolve()
    }
    w.now = until
  }
  // Every registration on an event, with its matcher when it has one, in the order made.
  const hooks = new Map<string, { matcher?: Record<string, unknown>; hook: Hook }[]>()
  register(((event: string, ...args: unknown[]) => {
    const [matcher, hook] = args.length > 1 ? [args[0] as Record<string, unknown>, args[1] as Hook] : [undefined, args[0] as Hook]
    hooks.set(event, [...(hooks.get(event) ?? []), { matcher, hook }])
    return { catch() {} }
  }) as unknown as On, {} as PluginOptions)
  const $ = {
    plugin: { name: 'workstreams', root: '/plugin' },
    session: {
      id: async () => w.sid,
      cwd: async () => CWD,
      messages: async (args?: { as?: string }) => (args?.as === 'api' ? api(w.transcript, w.strings) : w.transcript.filter(r => !r.meta).map(r => row(r.role, r.text))),
      append: async ({ message }: { message: { content: { text: string }[] } }) => {
        if (w.fail === 'refused') return { deny: 'a plugin above refused it' }
        const text = message.content[0]?.text ?? ''
        w.rows.push(text)
        w.events.push(`${text.includes('\n# Record\n') ? 'full' : 'refresh'} ${/^workstream: (\S+)$/m.exec(text)?.[1]}`)
        return { message, uuid: `u${w.rows.length}` }
      },
    },
    process: {
      run: async (argv: readonly string[], init: { stdin?: string; timeoutMs?: number }) => {
        const [cmd, sid = ''] = [argv[4], argv[5]]
        if (cmd === 'charter') {
          w.runs.push({ argv, init })
          if (w.fail === 'slow') throw new Error('process.run: still running at 10000 ms')
          return board(w, argv)
        }
        if (w.abandon && w.inHook) throw new Error('workstreams: $.process.run(uv) aborted')
        if (w.flying++ > 0) w.overlap = true
        try {
          for (let i = 0; i < 5; i++) await Promise.resolve()   // a run takes a while: anything not waiting for it overlaps it
          if (cmd === 'event') {
            const ev = JSON.parse(init.stdin ?? '{}') as Sent['ev']
            w.sent.push({ sid, ev })
            w.order.push(`board ${ev.event} ${sid}`)
            w.jobs.push(`board ${ev.event} ${sid}`)
            w.eventInit.push(init)
            if (w.eventFail === 'slow') throw new Error('process.run: still running at 10000 ms')
            return w.eventFail === 'exit' ? done('', 1, 'Traceback (most recent call last):\nValueError: unknown event\n') : done('')
          }
          w.jobs.push(argv.slice(4).join(' '))
          if (cmd !== 'write') return done('')
          w.writes.push({ sid, fields: JSON.parse(init.stdin ?? '{}'), timeoutMs: init.timeoutMs })
          if (w.writeFail === 'slow') throw new Error('process.run: still running at 10000 ms')
          return w.writeFail === 'exit' ? done('', 1, 'Traceback (most recent call last):\nKeyError: boom\n') : done(w.wrote)
        } finally {
          w.flying--
        }
      },
    },
    tool: {
      register: async (spec: ToolSpec) => {
        if (w.unregistrable) throw new Error('tool.register: refused')
        w.tools.push(spec)
        return { tool: `mcp__workstreams__${spec.name}` }
      },
    },
    agent: {
      list: async () => {
        if (w.unlisted) throw new Error('agent.list: refused')
        return w.agents
      },
    },
    fs: {
      read: async (path: string) => {
        if (path !== REG || w.unreadable) throw new Error(`no such file: ${path}`)
        return JSON.stringify({ pid: 4242, sessionId: w.sid, name: w.name })
      },
    },
    ui: { log: (text: string, options?: { to?: string }) => void w.logs.push({ text, to: options?.to }) },
    clock: {
      after: (ms: number, fn: () => void) => {
        const t = { at: w.now + ms, fn }
        if (w.dropTimers > 0) w.dropTimers--
        else w.timers.push(t)
        return { cancel: () => void (w.timers = w.timers.filter(x => x !== t)) }
      },
    },
  } as unknown as EngineInterface
  // The hooks on `event` whose matcher the input meets, nested first outermost, over `next`, whose fields (session.end's budget) each
  // hook's own next carries too.
  const call = (event: string, e: unknown, next: Hook) => {
    const chain = (hooks.get(event) ?? []).filter(r => Object.entries(r.matcher ?? {}).every(([k, v]) => (e as Record<string, unknown>)[k] === v))
    const at = (i: number): Hook => (i === chain.length ? next : Object.assign((ev: unknown) => chain[i]!.hook($, ev, at(i + 1)), next))
    return at(0)(e)
  }
  // A hook's dispatch, then what its timers start: the fire helpers resolve with what the hook returned once both are done.
  // `waits`: the hook may wait for its own board.py run, as the charter tool's does, so its timers run while it is under way.
  const dispatch = async <T>(run: () => Promise<T>, waits = false): Promise<T> => {
    w.inHook = true
    try {
      const r = run()
      if (waits) await w.tick()
      return await r
    } finally {
      await w.tick()
      w.inHook = false
    }
  }
  const fire = {
    start: () => dispatch(() => call('session.start', { cwd: CWD, surface: 'terminal', isInteractive: true }, async (e: { cwd: string }) => ({ cwd: e.cwd }))),
    // session.end's hook waits for its own run, so its timers run while it is under way.
    end: async (reason: SessionEndReason) => {
      const r = call('session.end', { reason, sessionId: w.sid, resume: { id: w.sid } },
        Object.assign(async (e: { sessionId: string }) => ({ sessionId: e.sessionId }), { budget: { ms: 1500, get remainingMs() { return w.budget } } }))
      await w.tick()
      return r
    },
    prompt: (text: string, turnId?: string) =>
      dispatch(() => call('prompt.submit', { text, wait: false, origin: { kind: 'composer' }, ...(turnId === undefined ? {} : { turnId }) },
        async (e: { text: string }) => (w.events.push(`prompt ${e.text}`), w.order.push(`prompt ${e.text}`), { text: e.text }))),
    turn: (text = 'go') => dispatch(() => call('turn.start', { text, turnId: 't1' }, async (e: { turnId: string }) => ({ turnId: e.turnId }))),
    complete: (reason: TurnCompleteReason, answer = '', agentId?: string) =>
      dispatch(() => call('turn.complete', { answer, durationMs: 5, isAborted: reason === 'aborted', turnId: 't1', reason, ...(agentId === undefined ? {} : { agentId }) },
        async (e: { answer: string }) => ({ text: e.answer }))),
    check: (tool: string, id: string | undefined, decision: 'allow' | 'ask' | 'deny') =>
      dispatch(() => call('tool.check', { tool, input: {}, ...(id === undefined ? {} : { tool_use_id: id }) }, async () => ({ decision, reason: 'the rule' }))),
    // A call with the tool's arguments in `fields`, beside tool and tool_use_id as the engine spreads them, and core answering `out`.
    call: (tool: string, id: string | undefined, fields: Record<string, unknown> = {}, out: unknown = { result: 'ran', text: 'ran', ref: 1 }) =>
      dispatch(() => call('tool.call', { tool, ...(id === undefined ? {} : { tool_use_id: id }), ...fields }, async () => (w.core.push(tool), out)), true),
    compact: (trigger: SessionCompactTrigger, messages: SessionMessage[], agentId?: string) =>
      dispatch(() => call('session.compact', { trigger, messages, ...(agentId === undefined ? {} : { agentId }) }, async (e: { messages: SessionMessage[] }) => {
        w.summarized.push([...e.messages])
        return w.skip ? { skip: 'nothing to compact' } : { messages: [row('user', 'Summary.')] }
      })),
    step: (agentId?: string) => dispatch(async () => {
      const s = call('turn.step', { turnId: 't1', index: 0, model: 'm', messageCount: 1, ...(agentId === undefined ? {} : { agentId }) },
        async function* (e: { agentId?: string }) {
          w.events.push(`step ${e.agentId ?? 'main'}`)
          w.order.push(`step ${e.agentId ?? 'main'}`)
          yield { kind: 'text', index: 0, text: 'ok' }
          return 'stepped'
        })
      let r = await s.next()
      while (r.done !== true) r = await s.next()
      return r.value
    }),
  }
  return { w, fire, call }
}

const row = (role: 'user' | 'assistant', text: string): SessionMessage => ({ role, text, toolUses: [] })
const meta = (text: string): Entry => ({ role: 'user', text, meta: true })
const REMINDER = meta('<system-reminder>\nToday is 2026-10-04.\n</system-reminder>')

function api(transcript: Entry[], strings: boolean) {
  const out: { role: string; content: { type: string; text: string }[] }[] = []
  for (const r of transcript) {
    const last = out.at(-1)
    if (last?.role === r.role) last.content.push({ type: 'text', text: r.text })
    else out.push({ role: r.role, content: [{ type: 'text', text: r.text }] })
  }
  return out.map(m => (strings && m.content.length === 1 ? { role: m.role, content: m.content[0]?.text } : m))
}

test('a fresh start appends one full row', async () => {
  const { w, fire } = harness()
  expect(await fire.start()).toEqual({ cwd: CWD })
  expect(w.rows).toEqual([charterText('PAYMENTS_API', false)])
  expect(w.runs).toEqual([{
    argv: ['uv', 'run', '--no-project', '/plugin/hooks/board.py', 'charter', 's1'],
    init: { cwd: CWD, env: { CLAUDE_PROJECT_DIR: CWD }, timeoutMs: 10_000 },
  }])
  expect(w.logs).toEqual([])
})

test('a start over a transcript holding a charter row appends a refresh', async () => {
  const { w, fire } = harness()
  w.transcript = [REMINDER, meta(charterText('PAYMENTS_API', false)), row('user', 'go'), row('assistant', 'Done.')]
  await fire.start()
  expect(w.rows).toEqual([charterText('PAYMENTS_API', true)])
  expect(w.runs[0]?.argv.at(-1)).toBe('--refresh')
})

test('a charter row the API form gives as a string is found too', async () => {
  const { w, fire } = harness()
  w.strings = true
  w.transcript = [meta(charterText('PAYMENTS_API', false)), row('assistant', 'Done.')]
  await fire.start()
  expect(w.events).toEqual(['refresh PAYMENTS_API'])
})

test('a reply restating the charter, or a prompt quoting it, is no charter row', async () => {
  const { w, fire } = harness()
  w.transcript = [REMINDER, row('user', `Read this: ${charterText('PAYMENTS_API', false)}`), row('assistant', charterText('PAYMENTS_API', false))]
  await fire.start()
  expect(w.events).toEqual(['full PAYMENTS_API'])
})

test('/clear then a prompt appends one full row ahead of the prompt, and the prompt after it appends nothing', async () => {
  const { w, fire } = harness()
  await fire.start()
  await fire.end('clear')
  w.sid = 's2'
  await fire.prompt('first')
  await fire.prompt('second')
  expect(w.events).toEqual(['full PAYMENTS_API', 'full PAYMENTS_API', 'prompt first', 'prompt second'])
  expect(w.runs.map(r => r.argv[5])).toEqual(['s1', 's2'])
})

test('a /resume or /branch appends the refresh ahead of the next prompt', async () => {
  const { w, fire } = harness()
  await fire.start()
  w.transcript = [REMINDER, meta(w.rows[0] ?? ''), row('user', 'go')]
  await fire.end('resume')
  await fire.prompt('back')
  await fire.prompt('again')
  expect(w.events).toEqual(['full PAYMENTS_API', 'refresh PAYMENTS_API', 'prompt back', 'prompt again'])
})

test('an exit owes nothing', async () => {
  const { w, fire } = harness()
  await fire.start()
  await fire.end('prompt_input_exit')
  await fire.prompt('late')
  expect(w.events).toEqual(['full PAYMENTS_API', 'prompt late'])
})

test('a /rename appends once', async () => {
  const { w, fire } = harness('loose')
  await fire.start()
  await fire.prompt('a')
  w.name = 'ADOPTED'
  await fire.prompt('b')
  await fire.prompt('c')
  expect(w.events).toEqual(['prompt a', 'full ADOPTED', 'prompt b', 'prompt c'])
  expect(w.runs).toHaveLength(2)   // at the start and on the new name: an unchanged name is read in process
  w.name = 'PAYMENTS_API'
  await fire.prompt('d')
  w.name = 'loose'
  await fire.prompt('e')
  expect(w.events.slice(4)).toEqual(['full PAYMENTS_API', 'prompt d', 'prompt e'])
})

test('a /rename whose board.py read fails is not tried again on every prompt', async () => {
  const { w, fire } = harness('loose')
  await fire.start()
  w.name = 'ADOPTED'
  w.fail = 'exit'
  await fire.prompt('a')
  await fire.prompt('b')
  expect(w.runs).toHaveLength(2)
  expect(w.logs).toHaveLength(1)
})

test('a compaction is summarized as it stands, and a full row follows it', async () => {
  const { w, fire } = harness()
  await fire.start()
  const said = [row('user', 'go'), row('assistant', 'Done.')]
  await fire.compact('manual', said)
  expect(w.summarized).toEqual([said])
  expect(w.rows).toHaveLength(1)   // never inside the compaction: its conversation is about to be replaced
  await fire.prompt('next')
  await fire.prompt('after')
  expect(w.events).toEqual(['full PAYMENTS_API', 'full PAYMENTS_API', 'prompt next', 'prompt after'])
})

test('a compaction in the middle of a turn appends at the next main-loop step', async () => {
  const { w, fire } = harness()
  await fire.start()
  await fire.compact('auto', [row('user', 'go')])
  expect(await fire.step('a1')).toBe('stepped')
  expect(await fire.step()).toBe('stepped')
  await fire.step()
  expect(w.events).toEqual(['full PAYMENTS_API', 'step a1', 'full PAYMENTS_API', 'step main', 'step main'])
})

test('a subagent compaction appends nothing', async () => {
  const { w, fire } = harness()
  await fire.start()
  const theirs = [row('user', 'task'), row('assistant', 'Done.')]
  await fire.compact('auto', theirs, 'a1')
  expect(w.summarized).toEqual([theirs])
  await fire.step()
  await fire.prompt('next')
  expect(w.events).toEqual(['full PAYMENTS_API', 'step main', 'prompt next'])
})

test('a precompute or a skipped compaction appends nothing', async () => {
  const { w, fire } = harness()
  await fire.start()
  const said = [row('user', 'go')]
  await fire.compact('precompute', said)
  w.skip = true
  await fire.compact('manual', said)
  expect(w.summarized).toEqual([said, said])
  await fire.prompt('next')
  expect(w.events).toEqual(['full PAYMENTS_API', 'prompt next'])
})

test('an unbound session appends nothing', async () => {
  const { w, fire } = harness('loose')
  await fire.start()
  await fire.prompt('a')
  await fire.end('clear')
  await fire.prompt('b')
  await fire.compact('manual', [row('user', 'go')])
  await fire.step()
  await fire.prompt('c')
  expect(w.rows).toEqual([])
  expect(w.logs).toEqual([])
})

test('with no registry file at the start, the first prompt looks once more', async () => {
  const { w, fire } = harness()
  w.registryless = true
  await fire.start()
  w.registryless = false
  await fire.prompt('a')
  await fire.prompt('b')
  expect(w.events).toEqual(['full PAYMENTS_API', 'prompt a', 'prompt b'])
  expect(w.runs).toHaveLength(2)
})

test('with no registry file at all, board.py runs twice and nothing is appended', async () => {
  const { w, fire } = harness()
  w.registryless = true
  await fire.start()
  await fire.prompt('a')
  await fire.prompt('b')
  expect(w.events).toEqual(['prompt a', 'prompt b'])
  expect(w.runs).toHaveLength(2)
})

for (const [fail, why] of [['exit', 'board.py exited 1: ValueError: boom'], ['json', 'JSON'], ['slow', 'still running at 10000 ms'],
                           ['refused', 'append refused: a plugin above refused it']] as const) {
  test(`a failure (${fail}) appends nothing, does not throw, logs one debug line and leaves the mod working`, async () => {
    const { w, fire } = harness()
    w.fail = fail
    await fire.start()
    expect(w.rows).toEqual([])
    expect(w.logs).toEqual([{ to: 'debug', text: expect.stringContaining(why) }])
    expect(w.logs[0]?.text).toStartWith(fail === 'refused' ? 'charter of PAYMENTS_API not appended (full): ' : 'charter not appended (full): ')
    w.fail = undefined
    await fire.end('clear')
    await fire.prompt('go')
    expect(w.events).toEqual(['full PAYMENTS_API', 'prompt go'])
  })
}

test('an unreadable registry file on a prompt logs one debug line and appends nothing', async () => {
  const { w, fire } = harness()
  await fire.start()
  w.unreadable = true
  await fire.prompt('go')
  expect(w.events).toEqual(['full PAYMENTS_API', 'prompt go'])
  expect(w.logs).toEqual([{ to: 'debug', text: expect.stringContaining('registry not read: no such file') }])
})


// --- the board record: what the mod forwards to board.py event ---

const events = (w: { sent: Sent[] }) => w.sent.map(s => s.ev.event)

test('a start records the session', async () => {
  const { w, fire } = harness()
  await fire.start()
  expect(w.sent).toEqual([{ sid: 's1', ev: { event: 'start', cwd: CWD } }])
  expect(w.eventInit).toEqual([{ cwd: CWD, env: { CLAUDE_PROJECT_DIR: CWD }, stdin: JSON.stringify({ event: 'start', cwd: CWD }), timeoutMs: 10_000 }])
  expect(w.rows).toHaveLength(1)
})

test('a hook returns before its board.py run, which a $.clock callback makes', async () => {
  const { w, call } = harness()
  expect(await call('turn.start', { text: 'go', turnId: 't1' }, async (e: { turnId: string }) => ({ turnId: e.turnId }))).toEqual({ turnId: 't1' })
  expect(w.sent).toEqual([])
  expect(w.timers.map(t => t.at)).toEqual([0])
  await w.tick()
  expect(events(w)).toEqual(['turn.start'])
})

test('a call answered as its dispatch is abandoned is still recorded', async () => {
  const { w, fire } = harness()
  w.abandon = true   // a $.process.run made inside a hook's dispatch rejects, as it did on an Esc at a dialog
  await fire.check('Bash', 't1', 'ask')
  await fire.call('Bash', 't1')
  expect(events(w)).toEqual(['ask', 'answered'])
  expect(w.logs).toEqual([])
})

test("a drain timer that never fires costs only the wait for the next event's", async () => {
  const { w, fire } = harness()
  w.dropTimers = 1
  await fire.turn()
  expect(w.sent).toEqual([])
  await fire.complete('answer', 'Done.')
  expect(events(w)).toEqual(['turn.start', 'turn.complete'])
})

test('every main-loop turn records its start, a prompt typed or not', async () => {
  const { w, fire } = harness()
  expect(await fire.turn('go')).toEqual({ turnId: 't1' })
  await fire.turn('')
  expect(w.sent).toEqual([{ sid: 's1', ev: { event: 'turn.start', cwd: CWD } }, { sid: 's1', ev: { event: 'turn.start', cwd: CWD } }])
})

test("a main-loop turn's end records why it ended and its answer", async () => {
  const { w, fire } = harness()
  expect(await fire.complete('answer', 'Done.')).toEqual({ text: 'Done.' })
  await fire.complete('aborted')
  await fire.complete('refusal')
  await fire.complete('error')
  expect(w.sent.map(s => s.ev)).toEqual(['answer', 'aborted', 'refusal', 'error'].map((reason, i) =>
    ({ event: 'turn.complete', reason, answer: i === 0 ? 'Done.' : '', cwd: CWD })))
})

test('a call put to the decider records an ask, a question for AskUserQuestion; nothing else does', async () => {
  const { w, fire } = harness()
  expect(await fire.check('Bash', 't1', 'ask')).toEqual({ decision: 'ask', reason: 'the rule' })
  await fire.check('AskUserQuestion', 't2', 'ask')
  expect(await fire.check('Bash', 't3', 'allow')).toEqual({ decision: 'allow', reason: 'the rule' })
  await fire.check('Bash', 't4', 'deny')
  await fire.check('Bash', undefined, 'ask')   // a query: no call, no one asked
  expect(w.sent.map(s => s.ev)).toEqual([{ event: 'ask', kind: 'permission', cwd: CWD }, { event: 'ask', kind: 'question', cwd: CWD }])
})

test('a call that asked records answered when it returns, once no other call that asked is out', async () => {
  const { w, fire } = harness()
  await fire.check('Bash', 't1', 'ask')
  await fire.check('Edit', 't2', 'ask')
  await fire.check('Read', 't3', 'allow')
  expect(await fire.call('Bash', 't1')).toEqual({ result: 'ran', text: 'ran', ref: 1 })
  await fire.call('Read', 't3')
  expect(events(w)).toEqual(['ask', 'ask'])
  await fire.call('Edit', 't2')
  await fire.call('Edit', 't2')
  await fire.call('Bash', undefined)
  expect(events(w)).toEqual(['ask', 'ask', 'answered'])
})

test("a main-loop turn's end forgets the calls that asked and are still out", async () => {
  const { w, fire } = harness()
  await fire.check('Bash', 't1', 'ask')
  await fire.complete('aborted')
  await fire.call('Bash', 't1')
  expect(events(w)).toEqual(['ask', 'turn.complete'])
})

test("a sub-agent shows from its run's first model request, by its type, and leaves with its run's end", async () => {
  const { w, fire } = harness()
  expect(await fire.step('a1')).toBe('stepped')
  await fire.step('a1')
  await fire.step()
  expect(await fire.complete('answer', 'Found it.', 'a1')).toEqual({ text: 'Found it.' })
  await fire.step('a1')   // resumed: a run of its own
  expect(w.sent.map(s => s.ev)).toEqual([{ event: 'child.start', agent_id: 'a1', name: 'Explore', cwd: CWD },
                                         { event: 'child.stop', agent_id: 'a1', cwd: CWD },
                                         { event: 'child.start', agent_id: 'a1', name: 'Explore', cwd: CWD }])
})

test('an agent the session does not list, such as a compaction fork, records nothing', async () => {
  const { w, fire } = harness()
  await fire.step('fork1')
  await fire.complete('answer', 'Summary.', 'fork1')
  expect(w.sent).toEqual([])
  w.unlisted = true
  expect(await fire.step('a1')).toBe('stepped')
  expect(w.sent).toEqual([])
  expect(w.logs).toEqual([{ to: 'debug', text: 'agents not listed: agent.list: refused' }])
})

test('a session end records the ending id, inside what the bound leaves', async () => {
  const { w, fire } = harness()
  w.budget = 1200
  expect(await fire.end('prompt_input_exit')).toEqual({ sessionId: 's1' })
  w.sid = 's2'
  w.budget = 0
  await fire.end('other')
  w.budget = Infinity
  await fire.end('logout')
  expect(w.sent).toEqual([{ sid: 's1', ev: { event: 'end', reason: 'prompt_input_exit', cwd: CWD } }, { sid: 's2', ev: { event: 'end', reason: 'other', cwd: CWD } },
                          { sid: 's2', ev: { event: 'end', reason: 'logout', cwd: CWD } }])
  expect(w.eventInit.map(i => (i as { timeoutMs: number }).timeoutMs)).toEqual([1200, 100, 10_000])
})

for (const reason of ['clear', 'resume'] as const) {
  test(`a /${reason} records the new id's start without a prompt, and the next prompt starts nothing more`, async () => {
    const { w, fire } = harness()
    await fire.start()
    await fire.end(reason)
    expect(w.order).toEqual(['board start s1', 'board end s1'])
    await w.tick(50)   // the id has not moved yet: look again
    w.sid = 's2'
    await w.tick(49)
    expect(w.order).toEqual(['board start s1', 'board end s1'])
    await w.tick(1)
    expect(w.order).toEqual(['board start s1', 'board end s1', 'board start s2'])
    await fire.prompt('first')
    await fire.prompt('second')
    expect(w.order).toEqual(['board start s1', 'board end s1', 'board start s2', 'prompt first', 'prompt second'])
    expect(w.events).toEqual(['full PAYMENTS_API', 'full PAYMENTS_API', 'prompt first', 'prompt second'])   // the charter is still owed to the prompt
    expect(w.timers).toEqual([])
  })
}

test("a prompt that comes before the new id is seen starts its record, and the look starts nothing more", async () => {
  const { w, fire } = harness()
  await fire.end('clear')
  w.sid = 's2'
  await fire.prompt('quick')
  await w.tick(5_000)
  expect(w.sent.map(s => `${s.ev.event} ${s.sid}`)).toEqual(['end s1', 'start s2'])
})

test('a look that never sees a new id gives up after 5 s, and the next prompt starts the record', async () => {
  const { w, fire } = harness()
  await fire.end('resume')
  await w.tick(4_950)
  expect(w.timers).toHaveLength(1)
  await w.tick(50)
  expect(w.timers).toEqual([])
  w.sid = 's2'
  await w.tick(1_000)
  expect(events(w)).toEqual(['end'])
  await fire.prompt('late')
  expect(w.sent.map(s => `${s.ev.event} ${s.sid}`)).toEqual(['end s1', 'start s2'])
})

test('a /rename records a start, queued ahead of its turn, but not over a running turn', async () => {
  const { w, fire } = harness('loose')
  await fire.start()
  w.name = 'ADOPTED'
  await fire.prompt('b')
  await fire.turn()
  w.name = 'PAYMENTS_API'
  await fire.prompt('c', 't7')   // typed over a running turn
  await fire.prompt('d')
  expect(events(w)).toEqual(['start', 'start', 'turn.start'])
  expect(w.events).toEqual(['full ADOPTED', 'prompt b', 'full PAYMENTS_API', 'prompt c', 'prompt d'])
})

test('a start forgets the sub-agents and the calls the record no longer holds', async () => {
  const { w, fire } = harness('loose')
  await fire.start()
  await fire.step('a1')
  await fire.check('Bash', 't1', 'ask')
  w.name = 'ADOPTED'
  await fire.prompt('b')
  await fire.call('Bash', 't1')
  await fire.step('a1')
  expect(events(w)).toEqual(['start', 'child.start', 'ask', 'start', 'child.start'])
})

for (const [fail, why] of [['exit', 'board.py exited 1: ValueError: unknown event'], ['slow', 'process.run: still running at 10000 ms']] as const) {
  test(`a board.py that fails (${fail}) records nothing, does not throw, logs one debug line, and the next event runs`, async () => {
    const { w, fire } = harness()
    w.eventFail = fail
    expect(await fire.turn()).toEqual({ turnId: 't1' })
    expect(await fire.check('Bash', 't1', 'ask')).toEqual({ decision: 'ask', reason: 'the rule' })
    expect(w.logs).toEqual([{ to: 'debug', text: `board turn.start not recorded: ${why}` }, { to: 'debug', text: `board ask not recorded: ${why}` }])
    w.eventFail = undefined
    await fire.call('Bash', 't1')
    expect(events(w)).toEqual(['turn.start', 'ask', 'answered'])
  })
}

test('board.py runs one event at a time', async () => {
  const { w, fire } = harness()
  await Promise.all([fire.check('Bash', 't1', 'ask'), fire.step('a1'), fire.turn(), fire.complete('answer', 'Done.')])
  expect(w.overlap).toBe(false)
  expect(events(w).sort()).toEqual(['ask', 'child.start', 'turn.complete', 'turn.start'])
})

// --- the charter tool and goal sync: board.py write, tick and mirror, on the same queue ---

const FIELDS = { focus: 'F', record: 'R', add_goals: ['G'], block: 'B', clear_block: true }

test('the charter tool is registered whenever board.py finds the session bound, and never while it is unbound', async () => {
  const { w, fire } = harness('loose')
  await fire.start()
  await fire.prompt('a')
  await fire.end('clear')
  await fire.prompt('b')
  expect(w.tools).toEqual([])
  w.name = 'ADOPTED'
  await fire.prompt('c')   // a /rename binds it
  expect(w.tools.map(t => t.name)).toEqual(['charter'])
  await fire.end('clear')
  w.sid = 's2'
  await fire.prompt('d')   // the full row after a /clear registers it again
  expect(w.tools).toHaveLength(2)
  const bound = harness()
  await bound.fire.start()
  const [spec] = bound.w.tools
  expect(Object.keys((spec?.inputSchema as { properties: object }).properties)).toEqual(Object.keys(FIELDS))
  expect(spec?.description).toContain('Do not edit the charter file yourself.')
  expect(spec?.description.split('\n').filter(l => l !== '' && !l.endsWith('.'))).toEqual([])   // no break inside a sentence
})

test('a refused registration logs one debug line, and the charter row still lands', async () => {
  const { w, fire } = harness()
  w.unregistrable = true
  await fire.start()
  expect(w.rows).toHaveLength(1)
  expect(w.logs).toEqual([{ to: 'debug', text: 'charter tool not registered: tool.register: refused' }])
})

test('a charter call reads its fields off the event, queues one write and answers with its summary, and core never runs it', async () => {
  const { w, fire } = harness()
  expect(await fire.call(TOOL, 't1', { ...FIELDS, consent: 'The user pressed "1: Yes"', note: 'mine' })).toEqual({ result: 'focus set\n2 goals left' })
  await fire.call(TOOL, 't2', { record: 'R' })
  expect(w.writes).toEqual([{ sid: 's1', fields: FIELDS, timeoutMs: 8_000 }, { sid: 's1', fields: { record: 'R' }, timeoutMs: 8_000 }])
  expect(w.jobs).toEqual(['write s1', 'write s1'])
  expect(w.core).toEqual([])
  expect(w.logs).toEqual([])
})

for (const [how, why] of [['sub-agent', 'only the main session writes the charter'], ['error', 'this session is not bound to a workstream'],
                          ['exit', 'the charter was not written: board.py exited 1: KeyError: boom'],
                          ['slow', 'the charter was not written: process.run: still running at 10000 ms'],
                          ['json', 'the charter write answered no JSON: ']] as const) {
  test(`a charter call that fails (${how}) is refused with why, never throws, and the next call is written`, async () => {
    const { w, fire } = harness()
    if (how === 'error') w.wrote = JSON.stringify({ error: why })
    if (how === 'json') w.wrote = '{"ok": '
    if (how === 'exit' || how === 'slow') w.writeFail = how
    const r = await fire.call(TOOL, 't1', { focus: 'F', ...(how === 'sub-agent' ? { agentId: 'a1' } : {}) })
    expect(r).toEqual({ deny: how === 'json' ? expect.stringContaining(why) : why })
    expect(w.writes).toHaveLength(how === 'sub-agent' ? 0 : 1)
    expect(w.logs).toEqual(how === 'exit' || how === 'slow' ? [{ to: 'debug', text: `charter not written: ${why.slice('the charter was not written: '.length)}` }] : [])
    Object.assign(w, { wrote: WROTE, writeFail: undefined })
    expect(await fire.call(TOOL, 't2', { focus: 'F' })).toEqual({ result: 'focus set\n2 goals left' })
    expect(w.core).toEqual([])
  })
}

test('a TaskUpdate that ran queues a tick of its task, and a refused or failed one queues none', async () => {
  const { w, fire } = harness()
  const ran = { result: { success: true, taskId: '3', updatedFields: ['status'] }, text: 'Updated task #3 status', ref: 1 }
  expect(await fire.call('TaskUpdate', 't1', { taskId: '3', status: 'completed' }, ran)).toEqual(ran)
  expect(await fire.call('TaskUpdate', 't2', { taskId: '4', status: 'completed' }, { deny: 'no' })).toEqual({ deny: 'no' })
  await fire.call('TaskUpdate', 't3', { taskId: '5', status: 'completed' }, { result: 'Task not found', text: 'Task not found', ref: 2, isError: true })
  await fire.call('TaskCreate', 't4', { subject: 'x', description: 'y' })
  expect(w.jobs).toEqual(['tick s1 3'])
  expect(w.core).toEqual(['TaskUpdate', 'TaskUpdate', 'TaskUpdate', 'TaskCreate'])
})

test('two TaskUpdates in parallel tick one after the other, in order', async () => {
  const { w, fire } = harness()
  await Promise.all([fire.call('TaskUpdate', 't1', { taskId: '1', status: 'completed' }), fire.call('TaskUpdate', 't2', { taskId: '2', status: 'completed' })])
  expect(w.overlap).toBe(false)
  expect(w.jobs).toEqual(['tick s1 1', 'tick s1 2'])
})

test("the goals are mirrored at a start, at each prompt, at a main-loop turn's end and for a /clear's new id, and nowhere else", async () => {
  const { w, fire } = harness()
  await fire.start()
  await fire.prompt('go')
  await fire.turn()
  await fire.step('a1')
  await fire.check('Bash', 't1', 'ask')
  await fire.call('Bash', 't1')
  await fire.complete('answer', 'Found it.', 'a1')
  await fire.compact('auto', [row('user', 'go')])
  await fire.complete('answer', 'Done.')
  expect(w.jobs).toEqual(['board start s1', 'mirror s1', 'mirror s1', 'board turn.start s1', 'board child.start s1', 'board ask s1', 'board answered s1',
                          'board child.stop s1', 'board turn.complete s1', 'mirror s1'])
  await fire.end('clear')
  w.sid = 's2'
  await w.tick(50)
  expect(w.jobs.slice(10)).toEqual(['board end s1', 'board start s2', 'mirror s2'])
})

// --- the module in the engine ---

function engine(on: On) {
  const k = { runs: [] as { argv: readonly string[]; init: unknown }[], sent: [] as { argv: readonly string[]; init: unknown }[], logs: [] as string[],
              summarized: [] as SessionMessage[][], jobs: [] as { argv: readonly string[]; init: unknown }[], tools: [] as string[], core: [] as string[] }
  on('tool.register', ($, e) => (k.tools.push(e.name), { value: { tool: `mcp__workstreams__${e.name}` } }))
  on('tool.call', ($, e) => (k.core.push(e.tool), { result: { success: true, taskId: '3', updatedFields: ['status'] } }))
  on('session.start', () => ({ cwd: CWD }))
  on('session.end', ($, e) => ({ sessionId: e.sessionId }))
  on('session.id', () => ({ value: 's1' }))
  on('session.cwd', () => ({ value: CWD }))
  on('session.messages', () => ({ value: [] }))
  on('agent.list', () => ({ value: [{ id: 'a1', type: 'Explore', description: 'look', status: 'running' as const }] }))
  on('fs.read', () => ({ value: JSON.stringify({ name: 'PAYMENTS_API' }) }))
  on('ui.log', ($, e) => (k.logs.push(`${e.to}: ${e.text}`), { value: undefined }))
  on('process.run', ($, e) => {
    if (e.argv[4] === 'event') return (k.sent.push({ argv: e.argv, init: e.init }), { value: done('') })
    if (e.argv[4] !== 'charter') return (k.jobs.push({ argv: e.argv, init: e.init }), { value: done(e.argv[4] === 'write' ? WROTE : '') })
    k.runs.push({ argv: e.argv, init: e.init })
    return { value: board({ name: 'PAYMENTS_API', registryless: false }, e.argv) }
  })
  on('prompt.submit', ($, e) => ({ text: e.text, origin: e.origin }))
  on('session.compact', ($, e) => (k.summarized.push([...e.messages]), { messages: [row('user', 'Summary.')] }))
  on('turn.start', ($, e) => ({ turnId: e.turnId }))
  on('turn.complete', ($, e) => ({ text: e.answer }))
  on('tool.check', () => ({ decision: 'ask' as const, reason: 'asks' }))
  on('turn.step', async function* ($, e) {
    yield { kind: 'text' as const, index: 0, text: 'ok' }
    return { turnId: e.turnId, index: e.index, answer: 'ok', toolUses: [], stopReason: 'end_turn' as const, usage: null }
  })
  return Object.assign(k, { clock: mock.clock(on) })
}

const startIn = ($: Engine) => $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })

async function stepIn($: Engine, agentId?: string) {
  const s = $.turn.step({ turnId: 't1', index: 0, model: 'm', messageCount: 1, ...(agentId === undefined ? {} : { agentId }) })
  const chunks: unknown[] = []
  let r = await s.next()
  while (r.done !== true) (chunks.push(r.value), (r = await s.next()))
  return { chunks, result: r.value }
}

const sentIn = (k: { sent: { init: unknown }[] }) => k.sent.map(s => JSON.parse((s.init as { stdin: string }).stdin) as Sent['ev'])

test('in the engine, a start runs board.py for the session in its cwd and reaches session.append', async ($, on) => {
  const k = engine(on)
  expect(await startIn($)).toEqual({ cwd: CWD })
  await k.clock.settle()
  expect(k.runs).toEqual([{
    argv: ['uv', 'run', '--no-project', expect.stringMatching(/\/hooks\/board\.py$/), 'charter', 's1'],
    init: { cwd: CWD, env: { CLAUDE_PROJECT_DIR: CWD }, timeoutMs: 10_000 },
  }])
  expect(k.sent).toEqual([{
    argv: ['uv', 'run', '--no-project', expect.stringMatching(/\/hooks\/board\.py$/), 'event', 's1'],
    init: { cwd: CWD, env: { CLAUDE_PROJECT_DIR: CWD }, stdin: JSON.stringify({ event: 'start', cwd: CWD }), timeoutMs: 10_000 },
  }])
  expect(k.logs).toEqual(['debug: charter of PAYMENTS_API not appended (full): no implementation for session.append'])
})

test('in the engine, a prompt enters as typed', async ($, on) => {
  engine(on)
  await startIn($)
  expect(await $.prompt.submit({ text: 'carry on', wait: false, origin: { kind: 'composer' } })).toEqual({ text: 'carry on', origin: { kind: 'composer' } })
})

test("in the engine, a compaction reaches the engine as it stands, and only the main conversation's owes the full row", async ($, on) => {
  const k = engine(on)
  await startIn($)
  const said = [row('user', 'go'), row('assistant', 'Done.')]
  await $.session.compact({ trigger: 'auto', messages: said, agentId: 'a1' })
  await stepIn($)
  expect(k.runs).toHaveLength(1)
  expect(await $.session.compact({ trigger: 'manual', messages: said })).toEqual({ messages: [row('user', 'Summary.')] })
  await stepIn($)
  expect(k.runs).toHaveLength(2)
  expect(k.summarized).toEqual([said, said])
})

test('in the engine, a model step streams through, a full row owed or not', async ($, on) => {
  const k = engine(on)
  await startIn($)
  const streamed = { chunks: [expect.objectContaining({ kind: 'text', text: 'ok' })], result: expect.objectContaining({ answer: 'ok', stopReason: 'end_turn' }) }
  expect(await stepIn($)).toEqual(streamed)
  await $.session.compact({ trigger: 'auto', messages: [row('user', 'go')] })
  expect(await stepIn($)).toEqual(streamed)
  expect(k.runs).toHaveLength(2)   // the start, and the full row owed after the compaction, paid at that step
})

test("in the engine, a turn's start and end, and a sub-agent's run, pass through and reach board.py", async ($, on) => {
  const k = engine(on)
  expect(await $.turn.start({ text: 'go', turnId: 't1' })).toEqual({ turnId: 't1' })
  expect(await stepIn($, 'a1')).toEqual(expect.objectContaining({ result: expect.objectContaining({ answer: 'ok' }) }))
  expect(await $.turn.complete({ answer: 'Found it.', durationMs: 5, isAborted: false, turnId: 't2', reason: 'answer', agentId: 'a1' })).toEqual({ text: 'Found it.' })
  expect(await $.turn.complete({ answer: 'Done.', durationMs: 9, isAborted: false, turnId: 't1', reason: 'answer' })).toEqual({ text: 'Done.' })
  await k.clock.settle()
  expect(sentIn(k)).toEqual([{ event: 'turn.start', cwd: CWD }, { event: 'child.start', agent_id: 'a1', name: 'Explore', cwd: CWD },
                             { event: 'child.stop', agent_id: 'a1', cwd: CWD }, { event: 'turn.complete', reason: 'answer', answer: 'Done.', cwd: CWD }])
})

test("in the engine, a check's verdict passes through, and a query asks no one", async ($, on) => {
  const k = engine(on)
  expect(await $.tool.check({ tool: 'Bash', input: { command: 'ls' } })).toEqual({ decision: 'ask', reason: 'asks' })
  await k.clock.settle()
  expect(k.sent).toEqual([])
})

test('in the engine, a session end records the ending id', async ($, on) => {
  const k = engine(on)
  const ending = $.session.end({ reason: 'prompt_input_exit', sessionId: 's0', resume: { id: 's0' } })
  await k.clock.settle()   // its hook waits for the run, which a timer on the held clock makes
  expect(await ending).toEqual({ sessionId: 's0' })
  expect(k.sent).toEqual([{
    argv: ['uv', 'run', '--no-project', expect.stringMatching(/\/hooks\/board\.py$/), 'event', 's0'],
    init: expect.objectContaining({ stdin: JSON.stringify({ event: 'end', reason: 'prompt_input_exit', cwd: CWD }) }),
  }])
  // The kit's bound reads about 10 s and can tick a millisecond under it; the harness test above pins the clamp exactly.
  const { timeoutMs } = k.sent[0]?.init as { timeoutMs: number }
  expect(timeoutMs).toBeGreaterThan(9_000)
  expect(timeoutMs).toBeLessThanOrEqual(10_000)
})

test('in the engine, a start registers the charter tool and mirrors; the mod answers a charter call, and a TaskUpdate queues its tick', async ($, on) => {
  const k = engine(on)
  await startIn($)
  await k.clock.settle()
  expect(k.tools).toEqual(['charter'])
  const wrote = $.tool.call({ tool: TOOL, focus: 'F' })
  await k.clock.settle()   // the call waits for its write, which a timer on the held clock runs
  expect(await wrote).toEqual({ result: 'focus set\n2 goals left' })
  const updated = await $.tool.call({ tool: 'TaskUpdate', taskId: '3', status: 'completed' })
  expect(updated).toEqual({ result: { success: true, taskId: '3', updatedFields: ['status'] } })
  await k.clock.settle()
  expect(k.jobs.map(j => j.argv.slice(4))).toEqual([['mirror', 's1'], ['write', 's1'], ['tick', 's1', '3']])
  expect(JSON.parse((k.jobs[1]?.init as { stdin: string }).stdin)).toEqual({ focus: 'F' })
  expect(k.core).toEqual(['TaskUpdate'])   // the charter call never reached it
})

test('in the engine, a /clear records the id the session goes on under, from the clock', async ($, on) => {
  const k = engine(on)
  const ending = $.session.end({ reason: 'clear', sessionId: 's0', resume: { id: 's0' } })
  await k.clock.settle()
  await ending
  expect(sentIn(k)).toEqual([{ event: 'end', reason: 'clear', cwd: CWD }])
  await k.clock.advance(50)
  expect(k.sent.map(s => s.argv[5])).toEqual(['s0', 's1'])
  expect(sentIn(k)[1]).toEqual({ event: 'start', cwd: CWD })
})
