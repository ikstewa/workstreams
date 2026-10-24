#!/usr/bin/env python3
"""Workstream board hook. One script, every event. Stdlib only."""
import fcntl, json, os, re, shutil, sys, textwrap, time
from pathlib import Path

def project_root():
    return re.sub(r"/\.worktrees/.*$", "", os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())

def project_slug():
    return re.sub(r"[^A-Za-z0-9-]", "-", project_root())

def board_dir():
    d = Path.home() / ".claude" / "projects" / project_slug() / "board"
    d.mkdir(parents=True, exist_ok=True)
    return d

NOW = lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

def registered(sid):
    """(path, registry) for the registry file naming session `sid`, or None. Found by id: a mod-started process has no CLAUDE_PID,
    and an externally triggered event's (claude stop) is the caller's."""
    # ponytail: a killed session's registry file outlives it by seconds; were its id revived inside them, the first file by name wins
    return next(((p, r) for p in sorted((Path.home() / ".claude" / "sessions").glob("*.json")) if (r := reg_of(p.stem)) and r.get("sessionId") == sid), None)

def repo_branch(cwd):
    m = re.search(r"/\.worktrees/([^/]+)/(.+)$", cwd or "")
    if m: return m.group(1), m.group(2)
    return (Path(cwd).name if cwd else None), None

def load_record(sid):
    # Missing, unreadable and written to an older shape are one case: identity() rebuilds from
    # new_record(), the only thing that knows this session's current truth.
    try:
        return json.loads((board_dir() / f"{sid}.json").read_text())
    except (OSError, ValueError):
        return None

def save_record(rec):
    rec["updated_at"] = NOW()   # last event of any kind; only a turn's start and end touch last_turn_at
    p = board_dir() / f"{rec['session_id']}.json"
    tmp = p.with_suffix(".tmp"); tmp.write_text(json.dumps(rec, indent=1)); tmp.replace(p)

def new_record(event, reg):
    name = (reg or {}).get("name") or "unnamed"
    repo, branch = repo_branch(event.get("cwd"))
    kind = "worker" if " work: " in name else ("control" if name == "control" else "active")
    return {"session_id": event["session_id"], "pid": (reg or {}).get("pid"), "name": name,
            "workstream": name.split(" work: ")[0] if kind != "control" else None, "kind": kind,
            "cwd": event.get("cwd"), "repo": repo, "branch": branch, "permission_mode": event.get("permission_mode"),
            "state": "idle", "waiting_for": None, "waiting_since": None, "last_message": None, "children": [],
            "last_turn_at": None, "record_synced_at": None, "started_at": NOW(), "ended_reason": None}

IDENTITY, ASKS = ("pid", "name", "workstream", "kind"), ("permission", "question")

def identity(sid, cwd=None):
    """(the session's record, every key present, what new_record() makes of it now). Its identity follows the registry file naming
    the session when there is one: a /rename, or a record made before that file was written."""
    hit = registered(sid); fresh = new_record({"session_id": sid, "cwd": cwd}, hit and hit[1])
    rec = fresh | (load_record(sid) or {})   # the disk value wins where there is one
    if hit: rec.update({k: fresh[k] for k in IDENTITY})
    return rec, fresh

def event(sid, ev):
    """`board.py event <session-id>`: one transition the mod forwards, as JSON on stdin, onto the session's record."""
    rec, fresh = identity(sid, ev.get("cwd")); kind, now = ev.get("event"), NOW()
    if ev.get("cwd"): rec["cwd"] = ev["cwd"]; rec["repo"], rec["branch"] = repo_branch(ev["cwd"])   # a worker that enters a worktree shows its repo and branch
    if kind == "start":   # started, resumed or renamed, or a /clear's new id: all but identity and the turn times starts over
        rec.update({k: v for k, v in fresh.items() if k not in IDENTITY + ("started_at", "last_turn_at")})
    elif kind == "turn.start":
        rec.update(state="busy", waiting_for=None, waiting_since=None, last_turn_at=now)
    elif kind == "turn.complete" and ev.get("reason") == "aborted":
        rec.update(state="idle", waiting_for=None, waiting_since=None, last_turn_at=now)
    elif kind == "turn.complete":
        rec.update(state="waiting", waiting_for="replied", waiting_since=now, last_turn_at=now,
                   last_message=(ev.get("answer") or "")[:280] or rec["last_message"])
    elif kind == "ask":
        # A sub-agent's call can ask after the turn has ended, so its answer puts back what the wait covered, not busy.
        if rec["waiting_for"] not in ASKS: rec["before_ask"] = [rec["state"], rec["waiting_for"], rec["waiting_since"]]
        rec.update(state="waiting", waiting_for="question" if ev.get("kind") == "question" else "permission", waiting_since=rec["waiting_since"] or now)
    elif kind == "answered":
        if rec["waiting_for"] in ASKS:
            state, waiting_for, since = rec.pop("before_ask", None) or ("busy", None, None)
            rec.update(state=state, waiting_for=waiting_for, waiting_since=since)
    elif kind == "child.start":
        # A resumed sub-agent starts again under the same id: update its row, never add a second.
        known = next((c for c in rec["children"] if c["agent_id"] == ev.get("agent_id")), None)
        if known: known["state"] = "running"
        else: rec["children"].append({"agent_id": ev.get("agent_id"), "name": ev.get("name"), "state": "running", "last_message": None})
    elif kind == "child.stop":
        # The board shows only working sub-agents: a finished one leaves, and a resumed one comes back through child.start.
        rec["children"] = [c for c in rec["children"] if c["agent_id"] != ev.get("agent_id")]
    elif kind == "end":
        rec.update(state="ended", ended_reason=ev.get("reason"))
    else: raise ValueError(f"unknown event: {kind!r}")
    save_record(rec)
    if kind == "ask": snooze(rec["workstream"])   # a permission or a question wants Ian: it ends the workstream's snooze for good

def ws_dir():
    return Path.home() / ".claude" / "projects" / project_slug() / "workstreams"

FRONT = re.compile(r"---\n(.*?)\n---\n?(.*)", re.S)   # a charter file: frontmatter, then the record

def find_charter(name):
    if not name: return None
    for p in sorted(ws_dir().glob("*.md")):
        m = FRONT.match(p.read_text())
        if m and re.search(rf"^workstream:\s*{re.escape(name)}\s*$", m.group(1), re.M):
            return p, m.group(1), m.group(2)
    return None

def goals(fm):
    """[(done, text, offset of its checkbox in fm)] from the `goals:` block list of "[x] …" / "[ ] …", quoted or not, a double-quoted one
    unescaped as scalar() unescapes. The one reading of a goal: the board counts these and the task list mirrors them."""
    gl = re.search(r"^goals:[ \t]*\n((?:[ \t]+-.*\n?)*)", fm, re.M)
    return [(m.group(2) != " ", unescape(m.group(3)) if m.group(1) == '"' else m.group(3), gl.start(1) + m.start(2))
            for m in re.finditer(r"^[ \t]+-[ \t]*([\"']?)\[([ xX])\][ \t]*(.*?)[ \t]*\1?[ \t]*$", gl.group(1), re.M)] if gl else []

unescape = lambda s: re.sub(r"\\(.)", r"\1", s)
esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')   # a YAML double-quoted string, as unescape() reads it back

def atomic_write(p, text):
    # The dot name keeps a half-written file out of every *.json and *.md listing.
    tmp = p.with_name(f".{p.name}.tmp"); tmp.write_text(text); tmp.replace(p)

def locked(path):
    """The lock beside the charters, held across every read, change and write of one by write(), tick_goal() and save_note(): a session's
    write, a tick and Ian's note can land in the same moment from different processes, and each would write over the others."""
    f = open(path.parent / ".lock", "a"); fcntl.flock(f, fcntl.LOCK_EX); return f   # closing it, at the end of its with, releases it

def put(lines, key, line, after=None):
    """Frontmatter `lines` with the `key:` line set to `line` where it stands, else added after the `after:` line or last; None removes it.
    True when there was one."""
    # ponytail: a value continued on indented lines keeps them; no charter has one
    at = next((i for i, l in enumerate(lines) if l.startswith(f"{key}:")), None)
    if at is not None: lines[at:at + 1] = [line] if line else []
    elif line: lines.insert(next((i + 1 for i, l in enumerate(lines) if after and l.startswith(f"{after}:")), len(lines)), line)
    return at is not None

def task_dir(sid):
    # ponytail: a team name, when a session has one, names its task list ahead of both
    return Path.home() / ".claude" / "tasks" / re.sub(r"[^a-zA-Z0-9_-]", "-", os.environ.get("CLAUDE_CODE_TASK_LIST_ID") or sid)

def mirror_goals(rec, fm):
    """Reconcile the session's task list against the charter's goals. A goal task carries metadata.workstream_goal
    and metadata.workstream; every other task is left as it is. Ids follow Claude Code's rule, so none is ever reused."""
    # ponytail: Claude Code's own task writes take no lock; the mod queues this at a start, a prompt and a turn's end, when it makes none
    # ponytail: a /rename leaves the old workstream's goal tasks on the list
    key, d, want = rec["workstream"], task_dir(rec["session_id"]), {}
    for done, text, _ in goals(fm): want.setdefault(text, done)
    ids = sorted(int(p.stem) for p in d.glob("*.json") if p.stem.isdigit())
    try: hw = int((d / ".highwatermark").read_text())
    except (OSError, ValueError): hw = 0
    kept = set()
    for i in ids:
        try: t = json.loads((d / f"{i}.json").read_text())
        except (OSError, ValueError): continue
        meta = t.get("metadata") if isinstance(t, dict) else None
        if not (isinstance(meta, dict) and meta.get("workstream") == key and "workstream_goal" in meta): continue
        goal = meta["workstream_goal"]
        if goal not in want or goal in kept:   # dropped from the charter, or a second task for one goal
            (d / f"{i}.json").unlink(missing_ok=True)
            if i > hw: hw = i; atomic_write(d / ".highwatermark", str(i))
            continue
        kept.add(goal)
        status = "completed" if want[goal] else "pending" if t.get("status") == "completed" else t.get("status")
        if status != t.get("status"): atomic_write(d / f"{i}.json", json.dumps(t | {"status": status}, indent=2))
    # Claude Code clears a list once every task on it is completed: recreating them would loop, ids climbing each turn.
    if all(want.values()): return
    new = max(ids + [hw])
    for goal in (g for g in want if g not in kept):
        new += 1; d.mkdir(parents=True, exist_ok=True)
        atomic_write(d / f"{new}.json", json.dumps({"id": str(new), "subject": goal, "description": f"Mirrors a goal in the {key} charter; completing this task ticks it there.",
                                                    "status": "completed" if want[goal] else "pending", "blocks": [], "blockedBy": [],
                                                    "metadata": {"workstream": key, "workstream_goal": goal}}, indent=2))

def tick_goal(rec, path, tid):
    """A goal task's status, as its file reads now, onto its goal's checkbox. A deleted task changes nothing: the next reconcile recreates it."""
    try: t = json.loads((task_dir(rec["session_id"]) / f"{tid}.json").read_text())
    except (OSError, ValueError): return
    meta = t.get("metadata") or {}
    if meta.get("workstream") != rec["workstream"]: return
    with locked(path):
        text = path.read_text(); m = FRONT.match(text)
        for done, goal, at in goals(m.group(1)):
            if goal != meta.get("workstream_goal"): continue
            if done != (t.get("status") == "completed"):
                at += m.start(1); atomic_write(path, text[:at] + (" " if done else "x") + text[at + 1:])
            return

def bound(sid):
    """(record, (path, frontmatter, body)) for an active session bound to a charter, found by session id as charter() finds it, or None."""
    hit = registered(sid); rec = new_record({"session_id": sid}, hit and hit[1])
    return (rec, found) if hit and rec["kind"] == "active" and (found := find_charter(rec["workstream"])) else None

def sync(sid, tid=None):
    """`board.py mirror <session-id>` and `board.py tick <session-id> <task-id>`, from the mod's queue: the task list from the charter's
    goals, or a goal task's status onto its goal. Nothing for a session bound to no charter."""
    if not (b := bound(sid)): return
    rec, (path, fm, _) = b
    if tid is None: mirror_goals(rec, fm)
    else: tick_goal(rec, path, tid)

# What the charter tool's fields must be: the mod passes them as the model gave them.
WRITES = {"focus": "text", "record": "text", "add_goals": "a list of goal texts", "block": "text", "clear_block": "true"}

DATED = re.compile(r"- (?:\*\*)?(?:\d{4}-)?\d\d-\d\d")   # a dated row's bullet: **10-04**, 2026-10-04: and 2026-10-04 (Ian) all start so

def roll(key, body):
    """(body, N): `body` with its oldest N dated rows moved to <KEY>/record-archive.md, in order, until it fits two thirds of RECORD_BUDGET
    or no dated row is left, and the one line pointing at the archive at its top. A row runs from its bullet to the next bullet or heading."""
    # ponytail: a non-bullet paragraph straight after a row, before the next bullet or heading, is carried with that row
    segs = []
    for l in body.strip("\n").split("\n"):
        if l.startswith(("- ", "#")) or not segs: segs.append([l])
        else: segs[-1].append(l)
    size, gone = len(body.strip()), set()
    for i, s in enumerate(segs):
        if size <= RECORD_BUDGET * 2 // 3: break
        if DATED.match(s[0]): gone.add(i); size -= sum(len(l) + 1 for l in s)
    if not gone: return body, 0
    a = ws_dir() / key / "record-archive.md"; a.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(a, (a.read_text() if a.exists() else "") + "".join(l + "\n" for i in sorted(gone) for l in segs[i]))   # before the charter: a crash between repeats rows, never loses them
    kept, ptr = [l for i, s in enumerate(segs) if i not in gone for l in s], f"Earlier rows: {key}/record-archive.md"
    return "\n" + "\n".join(kept if ptr in kept else [ptr, ""] + kept).strip("\n") + "\n", len(gone)

def write(sid, req):
    """`board.py write <session-id>`, the mod's charter tool: the fields on stdin onto the session's charter, in WRITES' order, under the
    lock, as {ok, key, summary}; or {error} for a call refused. No field reaches note: or a goal's checkbox."""
    req = {k: req[k] for k in WRITES if req.get(k) not in (None, False)}
    valid = lambda k, v: (v is True if k == "clear_block" else isinstance(v, str) and v.strip() if k != "add_goals"
                          else isinstance(v, list) and v and all(isinstance(g, str) and g.strip() for g in v))
    if not req: return {"error": "nothing to write: give focus, record, add_goals, block or clear_block"}
    if bad := next((k for k, v in req.items() if not valid(k, v)), None): return {"error": f"{bad} must be {WRITES[bad]}"}
    if "block" in req and "clear_block" in req: return {"error": "block and clear_block cannot go in one call"}
    if not (b := bound(sid)): return {"error": "this session is not bound to a workstream"}
    rec, (path, _, _) = b; one = lambda s: " ".join(s.split()); done, added = [], False   # a frontmatter value is one line
    with locked(path):
        text = path.read_text(); m = FRONT.match(text); lines, body = m.group(1).split("\n"), m.group(2)
        if "focus" in req:
            put(lines, "focus", f'focus: "{esc(one(req["focus"]))}"', "purpose"); done.append("focus set")
        if "record" in req:
            first, _, rest = re.sub(r"^[-*] +", "", req["record"].strip()).partition("\n")   # the bullet is the tool's to add
            if not re.match(r"\*\*\d\d-\d\d", first): first = f"**{time.strftime('%m-%d')}** {first}"
            body = (body.rstrip() + "\n" if body.strip() else "\n") + f"- {first}" + (f"\n{rest}" if rest else "") + "\n"
            done.append("record entry added")
            if len(body.strip()) > RECORD_BUDGET:
                body, n = roll(rec["workstream"], body)
                if n: done.append(f"{n} rows rolled to the archive")
        if "add_goals" in req:
            # ponytail: a goals: list written inline, as goals: [], is not one goals() reads; no charter has one
            have, at = {g for _, g, _ in goals("\n".join(lines))}, next((i for i, l in enumerate(lines) if l.startswith("goals:")), None)
            if at is None: lines.append("goals:"); at = len(lines) - 1
            at = next((i for i in range(at + 1, len(lines)) if not re.match(r"[ \t]+-", lines[i])), len(lines))
            for g in map(one, req["add_goals"]):
                if g in have: done.append(f"goal already listed: {g}"); continue
                lines.insert(at, f'  - "[ ] {esc(g)}"'); at += 1; have.add(g); added = True; done.append(f"goal added: {g}")
        if "block" in req:
            put(lines, "blocked", f'blocked: "{esc(one(req["block"]))}"', "focus"); put(lines, "blocked_since", f"blocked_since: {NOW()}", "blocked")
            done.append(f"block set: {one(req['block'])}")
        if "clear_block" in req:
            had = put(lines, "blocked", None); had = put(lines, "blocked_since", None) or had
            done.append("block cleared" if had else "no block to clear")
        fm = "\n".join(lines); new = text[:m.start(1)] + fm + text[m.end(1):m.start(2)] + body
        if new != text: atomic_write(path, new)
    if added: mirror_goals(rec, fm)   # its task shows at once
    return {"ok": True, "key": rec["workstream"], "summary": "\n".join(done + [left_of(goals(fm)) or "no goals"])}

def launch(name, args, timeout, **kw):
    """`claude --bg -n <name> <args>`: (the backgrounded id, "") or (None, what it printed, on one line)."""
    import subprocess
    p = subprocess.run(["claude", "--bg", "-n", name, *args], capture_output=True, text=True, timeout=timeout, **kw)
    o = ANSI.sub("", p.stdout + p.stderr); m = re.search(r"backgrounded · (\S+)", o)   # the id is coloured even into a pipe
    return (m.group(1), "") if m else (None, oneline(o))

def worker(sid, req):
    """`board.py worker <session-id>`, the mod's worker tool: `claude --bg` for a worker named "<KEY> work: <task>" from the project root,
    not the session's cwd, which may be a worktree; as {ok, summary}, or {error}. The worker enters its own worktree.
    With {close: <id or unambiguous prefix>} instead: `claude stop` then `claude rm` for one of the caller's own workers. rm keeps a worktree whose commits are on no remote, which a merged PR
    (squashed, its branch deleted) leaves behind, and one a hook manages. Either is removed only when the worker's HEAD is on the default branch or its branch has a merged PR;
    a hook-managed one also only when clean."""
    import subprocess
    if req.get("close") is not None:
        close = req["close"]
        if req.get("task") is not None or req.get("prompt") is not None: return {"error": "give task and prompt to start a worker, or close to close one, not both"}
        if not (b := bound(sid)): return {"error": "this session is not bound to a workstream"}
        mine = [r for p in board_dir().glob(f"{close}*.json") if isinstance(close, str) and re.fullmatch(r"[\w-]+", close)
                and (r := load_record(p.stem)) and r.get("kind") == "worker" and r.get("workstream") == b[0]["workstream"]]
        if len(mine) != 1: return {"error": f"{close} is not the id of exactly one worker of {b[0]['workstream']}"}
        rec = mine[0]; job = job_of(rec["session_id"], reg_of(rec.get("pid")))
        def run(*argv, **kw):   # a step takes under 1 s; nine at 5 s are under the mod's 50 s run limit
            p = subprocess.run(argv, capture_output=True, text=True, timeout=5, **kw); return p.returncode, ANSI.sub("", p.stdout + p.stderr)
        wt, br, name = rec.get("cwd"), rec.get("branch"), rec["name"]
        def landed():   # no fetch: a stale origin ref still contains the commit the worker was cut from
            if any(run("git", "merge-base", "--is-ancestor", "HEAD", f"origin/{d}", cwd=wt)[0] == 0 for d in ("master", "main")): return True
            n = run("gh", "pr", "list", "--head", br, "--state", "merged", "--json", "number", "--jq", "length", cwd=wt) if br else (1, "")
            return not n[0] and n[1].strip().isdigit() and int(n[1]) > 0
        try:
            c = run("git", "rev-parse", "--path-format=absolute", "--git-common-dir", "--absolute-git-dir", "--abbrev-ref", "HEAD", cwd=wt) if wt and os.path.isdir(wt) else (1, "")
            got = c[1].split() if not c[0] else []
            # the main repo's git dir, found while the worktree exists; none for a main checkout, whose tree and branch close never touches
            common = got[0] if len(got) == 3 and got[0] != got[1] else None
            if common and got[2] != "HEAD": br = got[2]   # the record names a branch only for a <repo>/<branch> worktree path
            run("claude", "stop", job)   # fails on an exited session, which rm still removes
            code, out = run("claude", "rm", job)
            m = re.search(r"--discard-unpushed (\S+)", out)
            if m or (code and common and "worktree is still at" in out):   # else a hook manages the worktree, which rm will not remove
                if not landed(): return {"error": f'"{name}" is stopped but kept: its commits are not on the default branch and have no merged PR'}
                if m: code, out = run("claude", "rm", job, "--discard-unpushed", m.group(1))
                elif run("git", "-C", common, "worktree", "remove", wt)[0]: return {"error": f'"{name}" is stopped but kept: its worktree has uncommitted changes'}
                else: code, out = run("claude", "rm", job)
            if not code and common and br: run("git", "-C", common, "branch", "-D", br)
        except (OSError, subprocess.SubprocessError) as err: return {"error": f"closing failed: {err}"}
        return {"error": f"claude rm failed: {oneline(out)}"} if code else {"ok": True, "summary": f'closed "{name}"'}
    if not all(isinstance(req.get(k), str) and req[k].strip() for k in ("task", "prompt")): return {"error": "task and prompt must be text"}
    if not (b := bound(sid)): return {"error": "this session is not bound to a workstream"}
    name = f"{b[0]['workstream']} work: {' '.join(req['task'].split())}"
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}   # the worker is not a session of the cwd this ran in
    # ponytail: the 6 s bound sits under the mod's run limit, so a hung claude reaches the model as its why
    try: wid, why = launch(name, ["--permission-mode", "auto", req["prompt"]], 6, cwd=project_root(), env=env)
    except (OSError, subprocess.TimeoutExpired) as err: return {"error": f"claude --bg failed: {err}"}
    return {"ok": True, "summary": f'started "{name}", id {wid}'} if wid else {"error": f"claude --bg started nothing: {why or 'it printed nothing'}"}

BOUND_RULES = ("You are the active session for this workstream. Work through sub-agents and workers; hold coordination only. "
               "For work in several repos or worktrees at once, launch one worker per repo or worktree with the mcp__workstreams__worker tool "
               "(load it with ToolSearch first), never `claude --bg` yourself; each worker enters its own worktree and messages you when done. "
               "When a worker reports, confirm you have what you needed from it, then close it with the same tool; until then, reply to it. "
               "Every substantive turn, add a record entry and set the focus (plan progress, in flight, next) with the mcp__workstreams__charter tool: "
               "load it with ToolSearch first, and never edit the charter file. "
               "A record entry is one or two lines: what changed and what is next. Cite Ian's decisions by date and a few words, never quote him at length, "
               "and do not restate what a commit or PR already holds. Old rows roll to the charter's archive on their own. "
               "Each goal is mirrored on your task list: when you start on one, set its task to in_progress with TaskUpdate, which opens Ian's task panel, "
               "and completing its task ticks the goal. When new work appears, add it as a goal with the tool. Ask before any push. "
               "The workstream is private to this machine: never mention it, its charter, goals, record or block in anything that leaves the machine, "
               "such as a commit message, a PR title or description, a ticket, a comment or a document, because no reader there can see them. "
               "State what the work is for in its own terms. A key that is also a ticket key may still be cited as that ticket. "
               "When a sub-agent writes any of these for you, tell it the same. "
               "When a request falls outside the charter's scope, say so and offer three exits: extend the scope, do it unattached, or start or attach to another workstream. "
               "When the work waits on something outside your control and not on Ian, such as a review, someone's feedback or a merge, set a block with the tool "
               "and clear it once it clears. When you are picked up or reopened with a block set, first check whether it still holds. "
               "The charter's note: line is Ian's own: read it. The tool cannot change it.")

WORKER_BRIEF = ("# Workstream charter (worker of {key})\n\n"
                "You are a worker of workstream {key}. You start at the project root: enter your own worktree before you edit anything. "
                "When you finish, or are stuck on something only the active session can resolve, send it a message with SendMessage addressed to {key}. The active session closes you once it has what it needs from you. "
                "The workstream is private to this machine: never mention it, its charter, goals, record or block in anything that leaves the machine, "
                "such as a commit message, a PR title or description, a ticket, a comment or a document. State what the work is for in its own terms.")

RECORD_BUDGET = 30_000   # characters of the record a session is handed: its newest, where the handoff is

def charter_text(path, fm, body, refresh=False):
    """What a bound session reads of its charter: the rules ahead of everything a budget could cut, the frontmatter whole, then the
    record's newest RECORD_BUDGET characters from a line start. A refresh leaves the record out: the session already holds it.
    Done goals stay in the file, which the board and the task list read, and leave the text, with a count in their place."""
    gone, pos, keep = {fm.rfind("\n", 0, at) + 1 for d, _, at in goals(fm) if d}, 0, []
    if gone:
        for l in fm.split("\n"):
            if pos not in gone: keep.append(l)
            if l.startswith("goals:"): keep.append(f"# {len(gone)} done goals omitted")
            pos += len(l) + 1
        fm = "\n".join(keep)
    text = f"# Workstream charter ({path})\n\n# Operating rules\n{BOUND_RULES}\n\n---\n{fm}\n---"
    if refresh: return text
    body = body.strip()
    if len(body) > RECORD_BUDGET:
        tail = body[-RECORD_BUDGET:]
        if body[-RECORD_BUDGET - 1] != "\n": tail = tail[tail.find("\n") + 1:]
        body = f"(earlier record omitted: read {path})\n{tail}"
    return f"{text}\n\n# Record\n{body}"

def charter(sid, refresh=False):
    """`board.py charter <session-id> [--refresh]`, the mod's read: {registry, name} from the registry file naming the session,
    and with {key, path, text} for an active bound one, {key, worker, text} for a worker of a charter's key; {} with no registry file."""
    if not (hit := registered(sid)): return {}
    out = {"registry": str(hit[0]), "name": hit[1].get("name")}
    rec = new_record({"session_id": sid}, hit[1])
    if rec["kind"] not in ("active", "worker") or not (found := find_charter(rec["workstream"])): return out
    if rec["kind"] == "worker": return out | {"key": rec["workstream"], "worker": True, "text": WORKER_BRIEF.format(key=rec["workstream"])}
    return out | {"key": rec["workstream"], "path": str(found[0]), "text": charter_text(*found, refresh)}

def alive(pid):
    # Both checks. The registry file outlives a killed session by seconds to tens of seconds, so
    # the file alone reads a dead session as live; the file is what scopes this to Claude Code.
    if not pid or not (Path.home() / ".claude" / "sessions" / f"{pid}.json").exists():
        return False
    try:
        os.kill(pid, 0)          # sends nothing, asks only whether the pid exists
    except ProcessLookupError:
        return False
    except PermissionError:
        pass                     # another user's process, so it exists
    return True

def seconds_since(ts):
    import calendar
    try: return time.time() - calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))
    except Exception: return None

def hours_since(ts):
    s = seconds_since(ts); return None if s is None else round(s / 3600, 1)

def ago(s):
    s = max(s, 0); return f"{int(s // 60)}m" if s < 3600 else f"{int(s // 3600)}h" if s < 48 * 3600 else f"{int(s // 86400)}d"

def reg_of(pid):
    """The session registry file for `pid`, or None when it is missing or unreadable."""
    try: reg = json.loads((Path.home() / ".claude" / "sessions" / f"{pid}.json").read_text())
    except (OSError, ValueError): return None
    return reg if isinstance(reg, dict) else None

def job_of(sid, reg):
    """What `claude attach` and `claude stop` take: the short job id; the full session id is "no job matching"."""
    return reg.get("jobId") if reg and reg.get("sessionId") == sid and reg.get("jobId") else sid[:8]

def running(r):
    return any(c.get("state") == "running" for c in r.get("children") or [])

def working(rec):
    """Whether a live session's turn or a sub-agent of it runs: what draws a row Working, and what a reset never stops.
    The mod records every turn's start and its end, an interrupted one's too, so the record says."""
    return rec.get("state") == "busy" or running(rec)

def days_since(date_str):
    import calendar
    try: return (time.time() - calendar.timegm(time.strptime(date_str, "%Y-%m-%d"))) / 86400
    except Exception: return None

REQUIRED = ("session_id", "name", "state", "waiting_for")   # what render() indexes; the rest it reads with .get

def load_records():
    """Every board record, or the one line naming the first record render() cannot use."""
    records = []
    for rp in sorted(board_dir().glob("*.json")):
        if rp.name in ("tree-rows.json", "open.json"): continue   # the sidebar's click map and what Ian has seen, not session records
        try:
            rec = json.loads(rp.read_text())
        except Exception:
            return f"workstreams: board record {rp.name} unreadable"   # name it, do not skip it
        missing = [k for k in REQUIRED if k not in rec]
        if missing:
            return f"workstreams: board record {rp.name} missing {', '.join(missing)}"
        if not isinstance(rec.get("pid"), (int, type(None))):
            return f"workstreams: board record {rp.name} pid is not an integer"   # absent and None are fine; alive() hands anything else to os.kill
        records.append(rec)
    return records

def seen():
    """board/open.json: {"open": what is in the right pane, "title": its pane title when that is not "open", "seen": {what: when Ian last had it open}}.
    What a row opens is its stop(): a workstream's key, or a worker's session id. Only sidebar clicks write it."""
    try: return json.loads((board_dir() / "open.json").read_text())
    except (OSError, ValueError): return {}

stop = lambda e: e["session_id"] if "worker" in e else e["key"]   # what a tree-rows.json entry opens: a worker is its own stop, as its record is its own

def ws_of(token):
    """The workstream key of what is open: a worker's own, else the token itself."""
    rec = load_record(token) if token else None
    return rec["workstream"] if rec and rec.get("kind") == "worker" else token

def open_key(pane=None):
    """What is open in the right pane: open.json's "open", only while that pane titles itself with its title (@ws_name). The right pane is
    `pane` when the caller knows it (a key binding runs outside the sidebar), else any other pane in the sidebar's window.
    open.json outlives the pane: cc rebuilds it on the agent view after a tmux restart, and it closes when its attach exits."""
    import subprocess
    s = seen(); key, me = s.get("open"), os.environ.get("TMUX_PANE")
    if not (key and (pane or me)): return None
    try: p = subprocess.run(["tmux", "list-panes", "-t", pane or me, "-F", "#{pane_id}\t#{@ws_name}"], capture_output=True, text=True, timeout=1)
    except Exception: return None   # no tmux, or a hung one: no mark, never a missing board
    return key if p.returncode == 0 and (s.get("title") or key) in {n for i, _, n in (l.partition("\t") for l in p.stdout.splitlines()) if (i == pane if pane else i != me)} else None

def mark_open(token, title=None):
    # Leaving a session counts as having seen it up to now, as does opening one.
    s = seen(); at = s.get("seen") or {}; now = NOW()
    if s.get("open"): at[s["open"]] = now
    at[token] = now
    p = board_dir() / "open.json"; tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"open": token, "seen": at} | ({"title": title} if title and title != token else {}))); tmp.replace(p)

def charters(skip=("done", "archived")):
    """{key: (frontmatter, body)} for every charter whose status is not in `skip`."""
    out = {}
    for wp in sorted(ws_dir().glob("*.md")):
        m = FRONT.match(wp.read_text())
        if not m: continue
        ws = re.search(r"^workstream:\s*(\S+)", m.group(1), re.M); st = re.search(r"^status:\s*(\S+)", m.group(1), re.M)
        if ws and not (st and st.group(1) in skip): out[ws.group(1)] = (m.group(1), m.group(2))
    return out

def scalar(fm, name):
    """A charter's one-line `name:` value, or None. A double-quoted value is unescaped, as save_note() and write() write theirs with \\ and "
    escaped; any other value reads as it stands."""
    m = re.search(rf"^{name}:[ \t]*(.*?)[ \t]*$", fm, re.M)
    if not m: return None
    q = re.fullmatch(r'"((?:[^"\\]|\\.)*)"', m.group(1))
    return unescape(q.group(1)) if q else m.group(1) or None

def snoozed(fm):
    """Seconds left of a charter's snooze, or None with no snoozed_until:, an unparseable one or one that has passed."""
    s = seconds_since(scalar(fm, "snoozed_until"))
    return -s if s is not None and s < 0 else None

def remaining(s):
    """ago() for time to come, rounded up: a fresh 3h snooze reads 3h."""
    return ago(s + (59 if s < 3600 else 3599 if s < 48 * 3600 else 86399))

def notes():
    """{key: note} for every charter carrying one, whatever its status, as find_charter() finds a charter."""
    return {k: n for k, (fm, _) in charters(skip=()).items() if (n := scalar(fm, "note"))}

def listing():
    """sessionId -> `claude agents --json --all` row. A state source only; any failure is no extra state."""
    import subprocess
    try:
        rows = json.loads(subprocess.run(["claude", "agents", "--json", "--all"], capture_output=True, text=True, timeout=5).stdout)
        return {r.get("sessionId"): r for r in rows if isinstance(r, dict)}
    except Exception:
        return {}

# Grouped by assignment, a charter's block and, with nothing live, its goals: a status change recolours a row, and moves it only between Active and Blocked.
# A pinned workstream's rows go to Pinned, drawn as they would be in the group they would otherwise land in. A snoozed one's go to Snoozed,
# drawn as one dim line of its time left.
GROUPS = ("Pinned", "Active", "Blocked", "Idle", "Unassigned", "Done", "Snoozed")
SNOOZE = "☾"

def block_of(fm):
    """What the work waits on outside its session, with how long once blocked_since: says, or a falsy value; only the session sets and clears it."""
    bl, bs = re.search(r"^blocked:[ \t]*([\"']?)(.*?)\1[ \t]*$", fm, re.M), re.search(r"^blocked_since:[ \t]*[\"']?([^\s\"']*)", fm, re.M)
    s = seconds_since(bs.group(1)) if bs else None
    return bl and (what := (unescape(bl.group(2)) if bl.group(1) == '"' else bl.group(2)).strip()) and what + ("" if s is None else f" · {ago(s)}")

def left_of(gl):
    """What is left of a charter's goals: "N goals left", "all done", or None for a charter with none."""
    n = sum(not d for d, *_ in gl)
    return (f"{n} goal{'s' * (n != 1)} left" if n else "all done") if gl else None

def hint(rec, chart):
    """The charter whose refs.worktrees holds this session's cwd, or None. Worktrees only: repo refs match every root session."""
    cwd = (rec.get("cwd") or "").rstrip("/") + "/"
    for key, (fm, _) in sorted(chart.items()):
        wt = re.search(r"^\s*worktrees:\s*\[(.*)\]", fm, re.M)
        if wt and any(f"/{w.strip('/')}/" in cwd for w in re.findall(r"[\"']([^\"']+)[\"']", wt.group(1))): return key

def oneline(text, n=100):
    t = " ".join(str(text).split())
    return t if len(t) <= n else t[:n - 1] + "…"

def place(records):
    """What is on the board, deterministically: (live control records, group -> rows, stale keys). Both layouts format this."""
    rows, chart, sn = listing(), charters(), seen()
    pins = {k for k, (fm, _) in charters(skip=()).items() if scalar(fm, "pinned") == "true"}   # whatever the status, as notes() reads
    naps = {k: remaining(s) for k, (fm, _) in charters(skip=()).items() if (s := snoozed(fm))}   # time left, carried in a Snoozed row's note
    def land(home, key, note, lines):
        nap = key in naps and not any(l[2] == "31" for l in lines)   # a snooze never hides an ask, even one older than the snooze
        placed["Snoozed" if nap else "Pinned" if key in pins else home].append((home, key, naps[key] if nap else note, lines))
    done = set(charters(skip=("archived",))) - set(chart)   # finished: shown only while a session of it is still live
    # A reply is unread until Ian opens its session from the sidebar, a worker's by its own id and any other by its workstream's key
    # (the one open in the right pane is always read). ISO times compare as strings.
    at = sn.get("seen") or {}
    def unread(key, r):
        tok = r["session_id"] if r.get("kind") == "worker" else key
        return tok != sn.get("open") and (tok not in at or (r.get("last_turn_at") or "") > at[tok])
    # The pid check decides live; the listing is read only for a record already live by it.
    live = [r for r in records if r.get("kind") != "control" and r["state"] != "ended" and alive(r.get("pid"))]
    def reason(r):
        # A reply is Ian's turn only once no sub-agent of it runs: until then the session waits on that.
        if r["state"] == "waiting" and not (r.get("waiting_for") == "replied" and running(r)): return r.get("waiting_for") or "input"
        # What the listing says a session waits for: a dialog the mod sees no tool.check for, such as an MCP elicitation.
        row = rows.get(r["session_id"], {})
        if row.get("state") == "blocked" and row.get("waitingFor"): return row["waitingFor"]
    def status(key, r, turn="unread"):
        """(wide state, colour, tree label, reset) for one live session; control's row reads the same, and its own click restarts it instead.
        reset: the turn is Ian's and older than COLD_HOURS, so reopening re-sends the whole conversation uncached."""
        # Only a charter's active session resets: a fresh one rebuilds from that charter, which a worker or an unassigned session does not have.
        h = hours_since(r.get("last_turn_at")); cold = (h or 0) > COLD_HOURS; reset = cold and r.get("kind") == "active" and key in chart
        if why := reason(r):
            # yellow: the turn is yours, dim once read; red: a permission, a question or what a listing blocked session waits for
            if why == "replied":
                if r.get("kind") == "worker": turn = "replied"   # its workstream's goals would read as the workstream's own
                new = unread(key, r); code, why = ("33" if new else "2;33"), turn if new else turn.replace("unread", "read", 1)
            else: code, reset = "31", False
            ws_h = hours_since(r.get("waiting_since"))
            return f"waiting · {why}" + (f" · {ws_h}h" if ws_h is not None else ""), code, re.sub(r"^(unread|read) · ", "", why), reset   # the tree drops the prefix to fit its state cap
        if working(r): return "working", "32", "working", False
        if h is None: return "idle · no turn yet", "0", "no turn", False
        return f"idle · {h}h" + (" · cold" if cold else ""), ("34" if cold else "0"), f"{h}h" + (" cold" if cold and not reset else ""), reset   # on the tree the ↻ says cold
    by_ws = {}
    for r in live: by_ws.setdefault(r.get("workstream") or r["name"], []).append(r)
    placed, stale = {g: [] for g in GROUPS}, []   # group -> [(home group, workstream key, note, lines)]
    for key in sorted(set(chart) | set(by_ws)):
        fm, body = chart.get(key, ("", ""))
        gl = goals(fm); left = left_of(gl)   # live or dormant
        # Display only: the record keeps "replied"; the board shows it as unread, or read once opened.
        turn = f"unread · {left}" if left else "unread"
        note = (scalar(fm, "focus") or "(no focus)") + ("" if gl else "  [no goals]") if key in chart else "(unassigned)"
        block = block_of(fm)
        # A worker nests under its workstream's head row: the active session's, else the charter's dormant row. With neither, it stands alone.
        nest = key in chart or any(r.get("kind") != "worker" for r in by_ws.get(key, []))
        under = lambda r: nest and r.get("kind") == "worker"
        mine = sorted(by_ws.get(key, []), key=lambda r: (under(r), reason(r) is None, r["session_id"]))
        turns = [r["last_turn_at"] for r in records if r.get("workstream") == key and r.get("last_turn_at")]
        since = hours_since(max(turns)) if turns else None
        def dormant():
            """The charter's row with no live active session: what is left, or the block, and the retired session a click revives."""
            handoff = [l for l in body.splitlines() if l.strip()]
            extra = [f"last: {oneline(handoff[-1])}"] if turns and handoff else []
            # A dormant row is Ian's move, so it says what is left; every goal ticked outranks a charter no session has touched.
            label = "not started" if not turns and left != "all done" else left or "no goals"
            # Claude Code's supervisor retires a settled background session and keeps it listed: a click revives that session, never a new one.
            last = max((r for r in records if r.get("workstream") == key and r.get("kind") == "active"), key=lambda r: r.get("last_turn_at") or r.get("started_at") or "", default=None)
            sid = last["session_id"] if last and rows.get(last["session_id"], {}).get("kind") == "background" else None
            reset = bool(sid) and (hours_since(last.get("last_turn_at")) or 0) > COLD_HOURS
            return ("–", block or label + (f" · {since}h" if since is not None else ""), "2" if block else "34", extra, block or label, [], None, sid, reset, None, None)
        if mine:
            if sum(r.get("kind") == "active" for r in mine) > 1: note = "[duplicate] " + note
            if key in done: note = "done"
            looks = [status(key, r, turn) for r in mine]
            # A block moves a workstream out of Active only while none of its sessions runs or asks Ian: an ask is his to answer.
            held = block and not any(code in ("31", "32") for _, code, _, _ in looks)
            # A finished workstream's session sits in Done unless it runs or asks Ian: those stay in Active, where he sees or answers them.
            fin = key in done and not any(code in ("31", "32") for _, code, _, _ in looks)
            group = "Blocked" if held else "Done" if fin else "Active" if key in chart or key in done else "Unassigned"
            lines = [] if any(not under(r) for r in mine) else [dormant()]
            for r, (state, code, short, reset) in zip(mine, looks):
                if held and not under(r): state = short = block; code = "2"
                if key in done and not under(r): short = "done"   # the tree's label; its glyph and colour still carry the session's status
                guess = hint(r, chart) if key not in chart and key not in done else None
                extra = ([f"likely: {guess}"] if guess else []) + ([f"last: {oneline(r['last_message'])}"] if r.get("last_message") else []) + \
                        [f"· {c.get('name')} {str(c.get('agent_id'))[:8]} {c.get('state')}" for c in r.get("children") or []]
                wk = r.get("kind") == "worker" and {"name": r["name"], "task": r["name"].partition(" work: ")[2], "nested": under(r),
                                                    "where": f"{r['repo']}/{r['branch']}" if r.get("branch") else None}
                lines.append((r["session_id"][:8], state, code, extra, short, r.get("children") or [], guess, r["session_id"], reset,
                              r.get("waiting_since") or r.get("last_turn_at"), wk or None))   # the last seven feed only tree()
            land(group, key, note, lines)
            continue
        # Nothing live. A block, a turn inside STALE_DAYS or a charter created inside it keeps the row; absent or unparseable reads old.
        cr = re.search(r"^created:\s*(\S+)", fm, re.M)
        created_days = days_since(cr.group(1)) if cr else None
        if key not in pins and not block and (since is None or since > STALE_DAYS * 24) and (created_days is None or created_days > STALE_DAYS):
            stale.append(key); continue
        land("Blocked" if block else "Done" if left == "all done" else "Idle", key, note, [dormant()])
    ctl = [(r, *status("control", r)[:3]) for r in sorted((r for r in records if r.get("kind") == "control" and r["state"] != "ended" and alive(r.get("pid"))), key=lambda r: r["session_id"])]
    return ctl, placed, stale   # ctl: [(record, wide state, colour, tree label)]

nested = lambda wk: wk and wk["nested"]   # the last field of a place() line: a worker drawn under its workstream's row

def render(records, color=False):
    """The board, wide: one row per session with its focus, last message and children."""
    C = (lambda code, s: f"\033[{code}m{s}\033[0m") if color else (lambda code, s: s)
    ctl, placed, stale = place(records)
    nt = notes(); noted = lambda key, first: [f"note: {oneline(nt[key])}"] if first and nt.get(key) else []   # once a key, under its first row
    kw = max([len(k) for g in placed.values() for _, k, _, _ in g] + [len("control")] * bool(ctl) or [0])
    sw = max([len(s) for g in placed.values() for *_, ls in g for _, s, *_ in ls] or [0])
    out = ["# Workstream board (local inputs only; run /workstreams:board --deep for GitHub PRs and Jira)"]
    if ctl: out.append(C("1", "Control"))
    for r, state, code, _ in ctl:   # listed so every live session is accounted for; it belongs to no workstream
        out.append(f"  {C('36', 'control'.ljust(kw))}  {r['session_id'][:8]}  {C(code, state)}")
    for g in GROUPS:
        if not placed[g]: continue
        out.append(C("1", g))
        for _, key, note, lines in sorted(placed[g], key=lambda t: t[1]):
            if g == "Snoozed": out.append(C("2", f"  {key} · {note}")); continue
            for i, (sid, state, code, extra, *rest) in enumerate(lines):
                if nested(wk := rest[-1]):   # a worker, under its workstream's row and labelled with its name
                    out.append(f"    {C('36', wk['name'])}  {sid.ljust(8)}  {C(code, state.ljust(sw))}  {wk['where'] or ''}".rstrip())
                else: out.append(f"  {C('36', key.ljust(kw))}  {sid.ljust(8)}  {C(code, state.ljust(sw))}  {note}".rstrip())
                out += [C("2", f"      {e}") for e in noted(key, i == 0) + extra]
    if stale:
        out.append(C("2", f"stale ({len(stale)}, no session in {STALE_DAYS}d): " + ", ".join(sorted(stale))))
    if len(out) == 1: out.append("(nothing on the board)")
    return "\n".join(out)

ANSI = re.compile(r"\033\[[0-9;]*m")

def pane_width(width=None):
    # ponytail: 24-col floor; a narrower pane wraps lines rather than lose the state column
    return max(width or shutil.get_terminal_size().columns, 24)

def tree(records, color=False, width=None, hits=None, current=None, height=None):
    """The same board as render(), narrow: a file-tree sidebar for a tmux pane. Width is read per call, so a resize lands on the next redraw.
    `hits` gathers line number -> {key, session_id, glyph} for every clickable row: control, sessions, and idle, blocked and done workstreams,
    and a worker's own row under its workstream's, which adds worker, its name,
    and line number -> {key, goal} for each row of a goal in the open workstream's panel and {key, pin, snooze_x} for its key row; nothing else.
    A row whose glyph is ↻ adds reset_x, the glyph's column. `current` is open_key(), resolved by the caller: its rows carry the bar,
    and its charter, or its workstream's when it is a worker, fills the rows left under the footer. Height, like width, is read per call."""
    hits = {} if hits is None else hits
    C = lambda code, s: f"\033[{code}m{s}\033[0m" if color and code else s
    W, (ctl, placed, stale) = pane_width(width), place(records)
    def row(lead, glyph, name, state, code, name_code=None, dup=False, cap=None, on=False):
        # Name truncates with …; state right-aligns to W, capped at a third of it unless `cap` says otherwise. `!` after a key marks [duplicate].
        # `on`: the bar takes column 0 of the lead, so the open row is as wide as any other.
        lead, lead_code, name_code = ("▌" + lead[1:], "1", f"1;{name_code}" if name_code else "1") if on else (lead, "2", name_code)
        state = oneline(state, cap or W // 3); left = len(lead) + 2
        name = oneline(name, max(W - left - len(state) - 1 - dup, 1)) + "!" * dup
        return f"{C(lead_code, lead)}{C(code, glyph)} {C(name_code, name)}{' ' * (W - left - len(name) - len(state))}{C(code, state)}"
    tone = lambda code: code if code in ("31", "32", "33", "2;33") else "2"   # red asks, yellow unread, dim yellow read, green working, dim the rest
    # For Option+n: red asks Ian for an answer, yellow is an unread reply to him; a read one, dim yellow, needs nothing.
    need = lambda code, since: {"need": {"31": "asks", "33": "unread"}[code], "since": since} if code in ("31", "33") else {}
    nt = notes()
    def noted(key, trunk):   # Ian's note, dim, on the row's trunk; display only, so never in `hits`
        lead = f"  {trunk} ✎ "; return [C("2", lead + oneline(nt[key], W - len(lead)))] if nt.get(key) else []
    out = [C("1", "WORKSTREAMS"), C("1", "▾ Control")]
    for r, _, code, st in ctl or [(None, None, "2", "click to open")]:   # always a row: with no live control, a click dispatches one
        hits[len(out)] = {"key": "control", "session_id": r and r["session_id"], "glyph": "◆"} | need(code, r and (r.get("waiting_since") or r.get("last_turn_at")))
        out.append(row("  ", "◆", "control", st, tone(code), "36", cap=W - 12, on=current == "control"))   # 12: lead, glyph, "control", gap
    for g in GROUPS:
        if not placed[g]: continue
        out.append(C("1", f"▾ {g}"))
        if g == "Snoozed":   # one dim line a workstream; a click opens its first head row's session, and with no need Option+n passes over it
            for _, key, left, ls in sorted(placed[g], key=lambda t: t[1]):
                hits[len(out)] = {"key": key, "session_id": next(l[7] for l in ls if not nested(l[-1])), "glyph": SNOOZE}
                out.append(row("  ", SNOOZE, f"{key} · {left}", "", "2", "2", on=key == ws_of(current)))
            continue
        # A worker nests under its workstream's first head row (a [duplicate] key's first session), after that row's sub-agents.
        sessions = []
        for home, key, note, ls in sorted(placed[g], key=lambda t: t[1]):
            heads = [l for l in ls if not nested(l[-1])]
            sessions += [(key, note.startswith("[duplicate]"), l, home, [w for w in ls if nested(w[-1])] if n == 0 else []) for n, l in enumerate(heads)]
        for i, (key, dup, (tag, _, code, _, short, kids, guess, sid, reset, since, wk), home, workers) in enumerate(sessions):
            # A stale row's status is that it is stale, so its glyph is the reset button. A dormant row (tag "–", nothing live, a retired session or none) is ◌; a live one keeps ○.
            glyph = "↻" if reset else "⏸" if home == "Blocked" else {"31": "●", "33": "●", "32": "▶"}.get(code, "◌" if tag == "–" else "○")
            hits[len(out)] = {"key": key, "session_id": sid, "glyph": glyph} | ({"worker": wk["name"]} if wk else {}) | ({"reset_x": 2} if reset else {}) | need(code, since)   # reset_x: the column after the two-column lead
            if home == "Blocked":   # a block's label may take half the row, and what it waits on gives way before its age
                what, tail = re.fullmatch(r"(.*?)((?: · \d+[mhd])?)", short).groups(); short = oneline(what, W // 2 - len(tail)) + tail
            # ponytail: the title names a key, not a session, so every row of a [duplicate] key carries the bar
            out.append(row("  ", glyph, key, short, tone(code), "36", dup, cap=W // 2 if home == "Blocked" else None, on=(sid if wk else key) == current))
            trunk = "│" if i < len(sessions) - 1 else " "   # the group's trunk runs on while sessions follow
            if i == 0 or sessions[i - 1][0] != key: out += noted(key, trunk)   # a [duplicate] key's rows run together: its note shows once
            if guess: out.append(row(f"  {trunk} ", "→", f"{guess}?", "", "2"))
            for j, c in enumerate(kids):
                out.append(row(f"  {trunk} ", "└" if j == len(kids) - 1 and not workers else "├", str(c.get("name")), str(c.get("state")),
                               "32" if c.get("state") == "running" else "2"))
            for j, (_, _, wcode, _, wshort, _, _, wsid, _, wsince, w) in enumerate(workers):
                wglyph = {"31": "●", "33": "●", "32": "▶"}.get(wcode, "○")   # the branch is the lead's last column; the glyph is the session's own state
                hits[len(out)] = {"key": key, "session_id": wsid, "glyph": wglyph, "worker": w["name"]} | need(wcode, wsince)
                out.append(row(f"  {trunk} " + ("└" if j == len(workers) - 1 else "├"), wglyph, w["task"] + (f" · {w['where']}" if w["where"] else ""), wshort,
                               tone(wcode), on=wsid == current))
    count = f"stale ({len(stale)})"; full = f"{count}: " + ", ".join(sorted(stale))
    out += [C("2", "─" * W), C("2", full if stale and len(full) <= W else count)]
    if q := quota(W): out += [C("2", "─" * W), C(*q)]
    # The watch prints a newline after the board, so a board as tall as the pane would scroll its first row away.
    for row, goal in details(ws_of(current), W, (height or shutil.get_terminal_size().lines) - 1 - len(out), C):
        if goal: hits[len(out)] = {"key": ws_of(current)} | (goal if isinstance(goal, dict) else {"goal": goal})
        out.append(row)
    return "\n".join(out)

def details(key, W, room, C):
    """The charter of `key`, the workstream open in the right pane, in `room` rows: a double rule, its key and what is left of its goals,
    then what it is blocked on and Ian's note, its purpose, and its open goals at two rows apiece, a blank row around the key and between
    the rest, the last row cut with … when they run past. Nothing for a key with no charter, such as control or an unassigned session, or
    with no room for a row under the key. Returns [(row, goal)]: goal is the text of the goal a row shows, which a click on it sends, or for the key row
    {pin, snooze_x}: a click on its ☾, columns snooze_x, snoozes or unsnoozes, and anywhere else pins or unpins."""
    found = key and charters(skip=()).get(key)
    if not found or room < 5: return []
    fm = found[0]; gl = goals(fm); left = left_of(gl) or "no goals"
    wrap = lambda text, w: textwrap.wrap(" ".join(text.split()), w)
    hang = lambda glyph, lines, code=None, goal=None: [(code, (glyph + " " if i == 0 else "  ") + l, goal) for i, l in enumerate(lines)]   # a glyph, then an indent
    status = [l for glyph, text in (("⏸", block_of(fm)), ("✎", scalar(fm, "note"))) if text for l in hang(glyph, wrap(text, W - 2), "2")]
    purpose = [(None, l, None) for l in wrap(scalar(fm, "purpose") or "", W)]
    todo = []
    for _, text, _ in (g for g in gl if not g[0]):
        ls = wrap(text, W - 2); todo += hang("☐", ls[:1] + [oneline(" ".join(ls[1:]), W - 2)] if len(ls) > 1 else ls, goal=text)
    body = []
    for sec in filter(None, (status, purpose, todo)): body += [(None, "", None)] * bool(body) + sec
    if len(body) > room - 4:
        body = body[:room - 4]
        while not body[-1][1]: body.pop()
        code, last, goal = body[-1]; body[-1] = (code, last + " …" if len(last) <= W - 2 else last[:W - 1] + "…", goal)   # oneline() would drop the indent
    mark = ("★ " if scalar(fm, "pinned") == "true" else "☆ ") + SNOOZE + (f" {remaining(s)} " if (s := snoozed(fm)) else " ")
    name = mark + oneline(key, W - len(left) - 1 - len(mark))
    head = [(C("2", "═" * W), None), ("", None), (C("1;36", name) + " " * (W - len(name) - len(left)) + C("2", left), {"pin": True, "snooze_x": [2, len(mark) - 1]}), ("", None)]
    return head + [(C(code, l), goal) for code, l, goal in body]

def quota(W):
    """The sidebar's footer: (colour, line) for the 5h quota ~/.claude/quota-tap.sh saved from a status line, or None without one.
    Dim once the window it describes has reset; colours match the status line's (green, yellow from 70%, red from 90%)."""
    try:
        five = json.loads((Path.home() / ".claude" / "rate-limits.json").read_text())["five_hour"]
        pct, resets = round(float(five["used_percentage"])), int(five["resets_at"])
    except (OSError, ValueError, KeyError, TypeError): return None
    bar = "█" * (min(pct, 100) // 10) + "░" * (10 - min(pct, 100) // 10)
    left, at = f"5h {bar} {pct}%", time.strftime("%-I:%M%p", time.localtime(resets)).lower()
    right = f"resets {at}" if len(left) + len(at) + 8 <= W else at
    code = "2" if resets <= time.time() else "31" if pct >= 90 else "33" if pct >= 70 else "32"
    return code, left + " " * max(W - len(left) - len(right), 1) + right

COLD_HOURS = 1
STALE_DAYS = 3   # a workstream with no session turn this recent collapses to the stale summary line
DISPATCH_HOLD = 15   # seconds a key stays claimed after its dispatch: past a double-click and the redraw that shows the new session

RESUME = ("Start by restating this workstream from your injected charter: its purpose, what is in and out of scope, "
          "the goals still open, and any block it is waiting on and whether it still holds. "
          "Then read the handoff and tell me where things stand and what needs deciding.")

CONTROL = ["--model", "opus", "--effort", "high", "--permission-mode", "auto", "/workstreams:board"]   # control's own flags, never the user's defaults

def decide(entry, reg, rec=None, x=None, rows=dict):
    """What a click on a tree row does, given its tree-rows.json entry, its live session's registry (None if not live), its board record,
    the click's column (None opens) and a reader of the agents listing, called only for a session that is not live.
    ("attach", job) | ("restart", job) | ("dispatch", key) | ("message", text) | None for an unmapped line."""
    if not entry: return None
    control, sid = entry["key"] == "control", entry.get("session_id")
    live = bool(sid and reg and reg.get("sessionId") == sid)   # a reused pid names another session
    reset = x is not None and "reset_x" in entry and entry["reset_x"] <= x <= entry["reset_x"] + 1   # the ↻ and the space after it
    if not sid or (control or reset) and not live: return ("dispatch", entry["key"])   # control holds no state: an ended one is simply replaced
    # attach revives a retired background session under the same session id, transcript and all
    if not live: return ("attach", sid[:8]) if rows().get(sid, {}).get("kind") == "background" else ("message", f"{entry['key']}: session has ended")
    if reg.get("kind") != "bg": return ("message", f"{entry['key']}: interactive in another terminal, can't attach here")
    job = job_of(sid, reg)
    rec = (rec or {}) | {"session_id": sid}
    busy = working(rec)   # mid-turn, or a sub-agent running: never killed
    if control and not busy and (hours_since(rec.get("last_turn_at")) or 0) > COLD_HOURS: return ("restart", job)
    if reset and not busy and rec.get("waiting_for") in (None, "replied"): return ("restart", job)   # a permission or a question waits for Ian's answer
    return ("attach", job)   # a live session is opened as it stands: a click on its row never types into it

def tree_rows(goals=False):
    """board/tree-rows.json: {line: entry} as the watch last drew the sidebar, or {} without one. Only a click acts on a goal's or a pin's entry,
    so the rest, the steps and the menu, see the rows alone unless `goals`."""
    try: hits = json.loads((board_dir() / "tree-rows.json").read_text())
    except (OSError, ValueError): return {}
    return hits if goals else {n: e for n, e in hits.items() if "goal" not in e and "pin" not in e}

def judge(entry, x=None, rows=listing):
    """decide() for a tree-rows.json entry, with its board record and live registry read here: what a click on its row would do."""
    rec = load_record(entry["session_id"]) if entry and entry.get("session_id") else None
    pid = (rec or {}).get("pid")
    return decide(entry, reg_of(pid) if alive(pid) else None, rec, x, rows)

def open_row(line, target, x=None, client=None):
    """`board.py open <line> <pane> [<x> [<client>]]`: the sidebar's click, run by tmux, x its column and client where a menu it opens shows."""
    entry = tree_rows(goals=True).get(str(line))
    if entry and "goal" in entry: return focus(entry, target)
    if entry and "pin" in entry:
        lo, hi = entry.get("snooze_x") or (0, -1)
        if x is None or not lo <= x <= hi: return pin(entry["key"])
        found = find_charter(entry["key"])   # read at the click: the snooze may have passed since the draw
        return snooze(entry["key"]) if found and snoozed(found[1]) else snooze_menu(entry["key"], client)
    perform(entry, judge(entry, x), target)

def focus(entry, target):
    """A click on a goal in the panel: "Focus on this goal: <goal>" into the session open in `target`, and Enter, as Ian would type it.
    A bracketed paste, so a vim-mode prompt takes it as text in NORMAL mode too; a turn that is running queues it.
    Never while that session asks Ian for anything: Enter would answer the prompt."""
    import subprocess
    tmux = lambda *a: subprocess.run(["tmux", *a], capture_output=True, text=True)
    key = entry["key"]
    if open_key(target) != key: return tmux("display-message", "-l", f"workstreams: {key} is no longer open")
    if asks(key): return tmux("display-message", "-l", f"workstreams: {key} is asking you something; answer it first")
    tmux("set-buffer", "-b", "ws-goal", f"Focus on this goal: {entry['goal']}")
    tmux("paste-buffer", "-p", "-d", "-b", "ws-goal", "-t", target)
    tmux("send-keys", "-t", target, "Enter")

def asks(key):
    """Whether a live session of `key` asks Ian for anything but a reply, which its row draws red; unreadable records count as asking."""
    records = load_records()
    if isinstance(records, str): return True
    return any(code == "31" for g in place(records)[1].values() for _, k, _, ls in g if k == key for _, _, code, *_ in ls)

def perform(entry, act, target):
    """Does judge()'s `act` for `entry` in pane `target`. Every failure ends in a tmux message or nothing."""
    import subprocess
    tmux = lambda *a: subprocess.run(["tmux", *a], capture_output=True, text=True)
    if act and act[0] in ("restart", "dispatch"):
        # A double-click is two clicks about a second apart, and `claude --bg` can return inside that second: the key stays
        # claimed for DISPATCH_HOLD after a dispatch, so the second click cannot start a second session.
        lock = board_dir() / f".dispatch-{re.sub(r'[^A-Za-z0-9_-]', '-', entry['key'])}"
        try: os.close(os.open(lock, os.O_CREAT | os.O_EXCL))
        except FileExistsError:
            if time.time() - lock.stat().st_mtime < DISPATCH_HOLD: return tmux("display-message", f"{entry['key']}: already starting")
        if act[0] == "restart":
            subprocess.run(["claude", "stop", act[1]], capture_output=True, timeout=60); act = ("dispatch", entry["key"])
        args = CONTROL if act[1] == "control" else ["--permission-mode", "auto", RESUME]
        wid, why = launch(act[1], args, 120)
        act = ("attach", wid) if wid else ("message", f"{act[1]}: {why or 'dispatch printed nothing'}")
        lock.touch() if wid else lock.unlink(missing_ok=True)   # the hold runs from the dispatch's end; a failed one frees the key for a retry
    if act and act[0] == "attach":
        tmux("respawn-pane", "-k", "-t", target, f"claude attach {act[1]}")
        title = entry.get("worker", entry["key"])
        tmux("set", "-p", "-t", target, "@ws_name", title)   # the pane's title line (cc's pane-border-format) names what is open
        mark_open(stop(entry), title)
    elif act: tmux("display-message", act[1])

def step(way, target):
    """`board.py go next|prev|need <pane>`: from the stop open in `target` (a workstream's key, or a worker's session id), open the next (or previous) row in the sidebar's order, wrapping,
    whose click would attach: a live background session, or a retired one to revive. A row whose click would dispatch, restart or
    only explain is passed over, so a step never starts a session. With nothing open, next starts at the top and prev at the bottom.
    need steps the same way through only the rows waiting on Ian: the ones asking him for an answer, then his unread replies, each oldest first."""
    import functools, subprocess
    hits = tree_rows(); order = [hits[n] for n in sorted(hits, key=int)]
    if way == "prev": order.reverse()
    # ponytail: an unread row, once opened, leaves the list at the next redraw, so the press after it starts again from the top
    if way == "need": order = sorted((e for e in order if e.get("need")), key=lambda e: (e["need"] != "asks", e.get("since") or ""))
    here = open_key(target); at = next((i for i, e in enumerate(order) if stop(e) == here), -1)
    rows = functools.cache(listing)   # read once a step, and only for a row whose session is not live
    for e in order[at + 1:] + order[:at + 1]:
        # ponytail: a [duplicate] key is one stop, as the title names a key, not a session
        if stop(e) != here and (act := judge(e, rows=rows)) and act[0] == "attach": return perform(e, act, target)
    subprocess.run(["tmux", "display-message", "workstreams: nothing needs you" if way == "need" else "workstreams: no other session to open"], capture_output=True)

MENU_KEYS = "123456789abcdefghijklmnoprstuvwxyz"   # no q: it closes a tmux menu

def menu(client, target):
    """`board.py menu <client> <pane>`: every clickable row in the sidebar's order, as a tmux menu on `client` starting on the open row.
    An item runs its row's plain click, `board.py open <line> <pane>`: it opens, and never resets."""
    import shlex, subprocess
    hits = tree_rows(); lines = sorted(hits, key=int); here = open_key(target)
    if not lines: return
    click = lambda n: f"run-shell -b -c {shlex.quote(os.getcwd())} {shlex.quote(shlex.join([sys.executable, os.path.abspath(__file__), 'open', n, target]))}"
    # A name is a tmux format, so a # in a key is doubled. A map from a sidebar older than the glyph field names the key alone.
    name = lambda e: " ".join(filter(None, (e.get("glyph"), e.get("worker") or e["key"]))).replace("#", "##")
    items = [a for i, n in enumerate(lines) for a in (name(hits[n]), MENU_KEYS[i:i + 1], click(n))]
    at = next((i for i, n in enumerate(lines) if stop(hits[n]) == here), None)
    # -M: a menu opened from a key ignores the mouse without it.
    # ponytail: tmux draws no menu taller than the terminal, and nothing pages this one
    subprocess.run(["tmux", "display-menu", "-M", "-c", client, *(["-C", str(at)] if at is not None else []), *items], capture_output=True)

def note(client, target):
    """`board.py note <client> <pane>`: Option+e. A prompt on `client` for Ian's note on the workstream open in `target`, holding the current one.
    Enter runs save_note() for it, and an empty line clears it."""
    import shlex, subprocess
    tmux = lambda *a: subprocess.run(["tmux", *a], capture_output=True, text=True)
    if (key := ws_of(open_key(target))) is None: return tmux("display-message", "-c", client, "workstreams: open a session first")
    if not (found := find_charter(key)): return tmux("display-message", "-l", "-c", client, f"workstreams: {key} has no charter to note")   # -l: a key is no format
    fmt = lambda s: s.replace("%", "%%").replace("#", "##")   # the label and the pre-fill are formats with strftime; -l keeps their commas
    # The reply reaches save_note() through an option: %%% escapes what the parser reads, and set reads nothing more, where run-shell's
    # formats and the shell would. Every other % in the template is octal, since a %1, as in a pane id, takes the reply too.
    q = lambda s: '"' + re.sub(r'[\\"$]', r"\\\g<0>", s).replace("%", r"\045") + '"'
    save = shlex.join([sys.executable, os.path.abspath(__file__), "save-note", key, target]).replace("#", "##")   # run-shell expands formats
    # -b: without it this call would wait for Enter
    tmux("command-prompt", "-b", "-l", "-t", client, "-p", fmt(f"note for {key}: "), "-I", fmt(scalar(found[1], "note") or ""),
         f'set -p -t {q(target)} @ws_note "%%%" ; run-shell -b -c {q(os.getcwd())} {q(save)}')

def save_note(key, target):
    """`board.py save-note <key> <pane>`: what the prompt runs. The reply, waiting in the pane's @ws_note, stripped, becomes the note: line of
    key's charter, after focus: when it is new; an empty one removes it. Only that line changes, read and written under the lock, and the
    session writes its charter only through the tool, which never touches note:."""
    import subprocess
    p = subprocess.run(["tmux", "-u", "show", "-pv", "-t", target, "@ws_note"], capture_output=True, encoding="utf-8")   # -u: a C locale reads é as _
    if p.returncode or not (found := find_charter(key)): return
    reply = p.stdout.strip()
    with locked(found[0]):
        if not (m := FRONT.match(text := found[0].read_text())): return
        lines = m.group(1).split("\n"); put(lines, "note", f'note: "{esc(reply)}"' if reply else None, "focus")
        new = text[:m.start(1)] + "\n".join(lines) + text[m.end(1):]
        if new != text: atomic_write(found[0], new)   # unchanged, it keeps its mtime

def pin(key, on=None):
    """`board.py pin|unpin <key>`, and a click on the panel's ☆/★ (on=None toggles): the pinned: line of key's charter, after focus: when it is
    new, removed when off. Only that line changes, read and written under the lock as save_note() does note:. The report, or None without a charter."""
    if not (found := find_charter(key)): return None
    with locked(found[0]):
        if not (m := FRONT.match(text := found[0].read_text())): return None
        lines = m.group(1).split("\n"); on = scalar(m.group(1), "pinned") != "true" if on is None else on
        put(lines, "pinned", "pinned: true" if on else None, "focus")
        if on: put(lines, "snoozed_until", None)   # a pin and a snooze are never both on
        new = text[:m.start(1)] + "\n".join(lines) + text[m.end(1):]
        if new != text: atomic_write(found[0], new)   # unchanged, it keeps its mtime
    return f"{key} {'pinned' if on else 'unpinned'}"

def snooze(key, until=None):
    """`board.py snooze <key> <length>|unsnooze <key>`, an item of snooze_menu(), and an ask, which unsnoozes (until=None): the snoozed_until: line
    of key's charter, a UTC ISO time, after focus: when it is new, removed when off. A snooze also removes pinned:. Written under the lock as pin()
    writes. The report, or None without a charter."""
    if not (found := find_charter(key)): return None
    with locked(found[0]):
        if not (m := FRONT.match(text := found[0].read_text())): return None
        lines = m.group(1).split("\n"); put(lines, "snoozed_until", until and f"snoozed_until: {until}", "focus")
        if until: put(lines, "pinned", None)
        new = text[:m.start(1)] + "\n".join(lines) + text[m.end(1):]
        if new != text: atomic_write(found[0], new)   # unchanged, it keeps its mtime
    return f"{key} snoozed until {time.strftime('%a %H:%M', time.localtime(round(time.time() - seconds_since(until))))}" if until else f"{key} unsnoozed"

def until_of(length):
    """The UTC ISO time a snooze of `length` ends: Nm, Nh or Nd from now, or an ISO time as it stands; None for anything else."""
    if m := re.fullmatch(r"(\d+)([mhd])", length or ""):
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + int(m[1]) * {"m": 60, "h": 3600, "d": 86400}[m[2]]))
    return length if seconds_since(length) is not None else None

def snooze_menu(key, client=None):
    """A click on the panel's ☾: a tmux menu on `client` of 1h, 3h and 1d, each item `board.py snooze <key> <length>`. Its 1d is 08:00 tomorrow,
    local time, passed as an ISO time; the command's own 1d is 24 hours."""
    import shlex, subprocess
    t = time.localtime(); morning = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.mktime((t.tm_year, t.tm_mon, t.tm_mday + 1, 8, 0, 0, 0, 0, -1))))
    # run-shell expands formats, so a # in a key is doubled, as note() doubles it
    run = lambda arg: f"run-shell -b -c {shlex.quote(os.getcwd())} {shlex.quote(shlex.join([sys.executable, os.path.abspath(__file__), 'snooze', key, arg]))}".replace("#", "##")
    items = [a for i, (name, arg) in enumerate((("1h", "1h"), ("3h", "3h"), ("1d", morning))) for a in (name, str(i + 1), run(arg))]
    subprocess.run(["tmux", "display-menu", "-M", *(["-c", client] if client else []), "-T", f"snooze {key}".replace("#", "##"), *items], capture_output=True)

def main():
    if sys.argv[1:2] == ["charter"]: print(json.dumps(charter(sys.argv[2], "--refresh" in sys.argv[3:]))); return
    if sys.argv[1:2] == ["event"]: event(sys.argv[2], json.load(sys.stdin)); return
    if sys.argv[1:2] == ["write"]: print(json.dumps(write(sys.argv[2], json.load(sys.stdin)))); return
    if sys.argv[1:2] == ["worker"]: print(json.dumps(worker(sys.argv[2], json.load(sys.stdin)))); return
    if sys.argv[1:2] == ["mirror"]: sync(sys.argv[2]); return
    if sys.argv[1:2] == ["tick"]: sync(sys.argv[2], sys.argv[3]); return
    if sys.argv[1:2] == ["open"]:
        open_row(int(sys.argv[2]), sys.argv[3], int(sys.argv[4]) if sys.argv[4:] else None, sys.argv[5] if sys.argv[5:] else None); return
    if sys.argv[1:2] == ["go"]: step(*sys.argv[2:4]); return
    if sys.argv[1:2] == ["menu"]: menu(*sys.argv[2:4]); return
    if sys.argv[1:2] == ["note"]: note(*sys.argv[2:4]); return
    if sys.argv[1:2] == ["save-note"]: save_note(*sys.argv[2:4]); return
    if sys.argv[1:2] in (["pin"], ["unpin"]):
        said = pin(sys.argv[2], sys.argv[1] == "pin"); print(said or f"no charter for {sys.argv[2]}"); sys.exit(not said)
    if sys.argv[1:2] in (["snooze"], ["unsnooze"]):
        until = sys.argv[1] == "snooze" and until_of(sys.argv[3] if sys.argv[3:] else None)
        if until is None: print("give a length such as 90m, 3h or 1d"); sys.exit(1)
        said = snooze(sys.argv[2], until or None); print(said or f"no charter for {sys.argv[2]}"); sys.exit(not said)
    if sys.argv[1:2] == ["render"]:   # the /workstreams:board read; errors surface rather than print an empty board
        color = "--color" in sys.argv or ("--no-color" not in sys.argv and sys.stdout.isatty())
        narrow, width = "--tree" in sys.argv, (int(sys.argv[sys.argv.index("--width") + 1]) if "--width" in sys.argv else None)
        def once():
            records = load_records()
            if isinstance(records, str): return records
            if not narrow: return render(records, color)
            hits = {}; text = tree(records, color, width, hits, open_key())
            if "--watch" not in sys.argv: return text
            # ponytail: line numbers assume the tree fits the pane; a taller tree scrolls and clicks land a row off
            p = board_dir() / "tree-rows.json"; tmp = p.with_suffix(".tmp"); tmp.write_text(json.dumps(hits)); tmp.replace(p)
            head, _, rest = text.partition("\n")   # the tree's header carries the watch clock, right-aligned
            clock = time.strftime("%H:%M:%S")
            return head + " " * max(pane_width(width) - len(ANSI.sub("", head)) - len(clock), 1) + clock + "\n" + rest
        if "--watch" not in sys.argv:
            print(once()); return
        # Live view for a spare terminal tab: read-only, redraws until Ctrl-C. The clock lives here, never in render().
        born, opened = os.stat(__file__).st_mtime, board_dir() / "open.json"
        stamp = lambda: [p.exists() and p.stat().st_mtime_ns for p in (opened, ws_dir())]   # the charter directory moves when a pin or a snooze lands
        try:
            while True:
                last = stamp()   # taken before the draw, so a click landing mid-draw still wakes the next one
                print("\033[H\033[2J" + once() + ("" if narrow else f"\n\nupdated {time.strftime('%H:%M:%S')} · Ctrl-C to quit"), flush=True)
                # Opening a session rewrites open.json, a pin a charter: redraw at once, so the mark follows the click rather than the 3s tick.
                for _ in range(30):
                    if stamp() != last: break
                    time.sleep(0.1)
                # A long-lived watcher must not outlive its code: the click handler runs the new board.py, so an old loop misreads its files.
                if os.stat(__file__).st_mtime != born: os.execv(sys.executable, [sys.executable, __file__, *sys.argv[1:]])
        except KeyboardInterrupt:
            return
    # No subcommand: a settings hook of a session started before the mod took goal sync over, until it reloads. It reads nothing and
    # prints nothing.

if __name__ == "__main__":
    main()
