#!/usr/bin/env python3
"""Workstream board hook. One script, every event. Stdlib only."""
import fcntl, json, os, re, shutil, sys, textwrap, time
from pathlib import Path

def project_slug():
    root = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    root = re.sub(r"/\.worktrees/.*$", "", root)
    return re.sub(r"[^A-Za-z0-9-]", "-", root)

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

BOUND_RULES = ("You are the active session for this workstream. Work through sub-agents and workers; hold coordination only. "
               "Every substantive turn, add a record entry and set the focus (plan progress, in flight, next) with the mcp__workstreams__charter tool: "
               "load it with ToolSearch first, and never edit the charter file. "
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

RECORD_BUDGET = 30_000   # characters of the record a session is handed: its newest, where the handoff is

def charter_text(path, fm, body, refresh=False):
    """What a bound session reads of its charter: the rules ahead of everything a budget could cut, the frontmatter whole, then the
    record's newest RECORD_BUDGET characters from a line start. A refresh leaves the record out: the session already holds it."""
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
    and with {key, path, text} for an active bound one; {} with no registry file."""
    if not (hit := registered(sid)): return {}
    out = {"registry": str(hit[0]), "name": hit[1].get("name")}
    rec = new_record({"session_id": sid}, hit[1])
    if rec["kind"] != "active" or not (found := find_charter(rec["workstream"])): return out
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
    """board/open.json: {"open": key in the right pane, "seen": {key: when Ian last had it open}}. Only sidebar clicks write it."""
    try: return json.loads((board_dir() / "open.json").read_text())
    except (OSError, ValueError): return {}

def open_key(pane=None):
    """The key open in the right pane: open.json's "open", only while that pane titles itself with it (@ws_name). The right pane is
    `pane` when the caller knows it (a key binding runs outside the sidebar), else any other pane in the sidebar's window.
    open.json outlives the pane: cc rebuilds it on the agent view after a tmux restart, and it closes when its attach exits."""
    import subprocess
    key, me = seen().get("open"), os.environ.get("TMUX_PANE")
    if not (key and (pane or me)): return None
    try: p = subprocess.run(["tmux", "list-panes", "-t", pane or me, "-F", "#{pane_id}\t#{@ws_name}"], capture_output=True, text=True, timeout=1)
    except Exception: return None   # no tmux, or a hung one: no mark, never a missing board
    return key if p.returncode == 0 and key in {n for i, _, n in (l.partition("\t") for l in p.stdout.splitlines()) if (i == pane if pane else i != me)} else None

def mark_open(key):
    # Leaving a session counts as having seen it up to now, as does opening one.
    s = seen(); at = s.get("seen") or {}; now = NOW()
    if s.get("open"): at[s["open"]] = now
    at[key] = now
    p = board_dir() / "open.json"; tmp = p.with_suffix(".tmp"); tmp.write_text(json.dumps({"open": key, "seen": at})); tmp.replace(p)

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
GROUPS = ("Active", "Blocked", "Idle", "Unassigned", "Done")

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
    done = set(charters(skip=("archived",))) - set(chart)   # finished: shown only while a session of it is still live
    # A reply is unread until Ian opens its workstream from the sidebar; the one open in the right pane is always read. ISO times compare as strings.
    at = sn.get("seen") or {}
    unread = lambda key, r: key != sn.get("open") and (key not in at or (r.get("last_turn_at") or "") > at[key])
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
            if why == "replied": new = unread(key, r); code, why = ("33" if new else "2;33"), turn if new else turn.replace("unread", "read", 1)
            else: code, reset = "31", False
            ws_h = hours_since(r.get("waiting_since"))
            return f"waiting · {why}" + (f" · {ws_h}h" if ws_h is not None else ""), code, re.sub(r"^(unread|read) · ", "", why), reset   # the tree drops the prefix to fit its state cap
        if working(r): return "working", "32", "working", False
        if h is None: return "idle · no turn yet", "0", "no turn", False
        return f"idle · {h}h" + (" · cold" if cold else ""), ("34" if cold else "0"), f"{h}h" + (" cold" if cold and not reset else ""), reset   # on the tree the ↻ says cold
    by_ws = {}
    for r in live: by_ws.setdefault(r.get("workstream") or r["name"], []).append(r)
    placed, stale = {g: [] for g in GROUPS}, []   # group -> [(sort key, workstream key, lines)]
    for key in sorted(set(chart) | set(by_ws)):
        fm, body = chart.get(key, ("", ""))
        gl = goals(fm); left = left_of(gl)   # live or dormant
        # Display only: the record keeps "replied"; the board shows it as unread, or read once opened.
        turn = f"unread · {left}" if left else "unread"
        note = (scalar(fm, "focus") or "(no focus)") + ("" if gl else "  [no goals]") if key in chart else "(unassigned)"
        block = block_of(fm)
        mine = sorted(by_ws.get(key, []), key=lambda r: (reason(r) is None, r["session_id"]))
        if mine:
            if sum(r.get("kind") == "active" for r in mine) > 1: note = "[duplicate] " + note
            if key in done: note = "done"
            looks = [status(key, r, turn) for r in mine]
            # A block moves a workstream out of Active only while none of its sessions runs or asks Ian: an ask is his to answer.
            held = block and not any(code in ("31", "32") for _, code, _, _ in looks)
            # A finished workstream's session sits in Done unless it runs or asks Ian: those stay in Active, where he sees or answers them.
            fin = key in done and not any(code in ("31", "32") for _, code, _, _ in looks)
            group = "Blocked" if held else "Done" if fin else "Active" if key in chart or key in done else "Unassigned"
            lines = []
            for r, (state, code, short, reset) in zip(mine, looks):
                if held: state = short = block; code = "2"
                if key in done: short = "done"   # the tree's label; its glyph and colour still carry the session's status
                guess = hint(r, chart) if key not in chart and key not in done else None
                extra = ([f"likely: {guess}"] if guess else []) + ([f"last: {oneline(r['last_message'])}"] if r.get("last_message") else []) + \
                        [f"· {c.get('name')} {str(c.get('agent_id'))[:8]} {c.get('state')}" for c in r.get("children") or []]
                lines.append((r["session_id"][:8], state, code, extra, short, r.get("children") or [], guess, r["session_id"], reset,
                              r.get("waiting_since") or r.get("last_turn_at")))   # the last six feed only tree()
            placed[group].append(("", key, note, lines))
            continue
        # Nothing live. A block, a turn inside STALE_DAYS or a charter created inside it keeps the row; absent or unparseable reads old.
        turns = [r["last_turn_at"] for r in records if r.get("workstream") == key and r.get("last_turn_at")]
        since = hours_since(max(turns)) if turns else None
        cr = re.search(r"^created:\s*(\S+)", fm, re.M)
        created_days = days_since(cr.group(1)) if cr else None
        if not block and (since is None or since > STALE_DAYS * 24) and (created_days is None or created_days > STALE_DAYS):
            stale.append(key); continue
        handoff = [l for l in body.splitlines() if l.strip()]
        extra = [f"last: {oneline(handoff[-1])}"] if turns and handoff else []
        # A dormant row is Ian's move, so it says what is left; every goal ticked outranks a charter no session has touched.
        label = "not started" if not turns and left != "all done" else left or "no goals"
        # Claude Code's supervisor retires a settled background session and keeps it listed: a click revives that session, never a new one.
        last = max((r for r in records if r.get("workstream") == key and r.get("kind") == "active"), key=lambda r: r.get("last_turn_at") or r.get("started_at") or "", default=None)
        sid = last["session_id"] if last and rows.get(last["session_id"], {}).get("kind") == "background" else None
        reset = bool(sid) and (hours_since(last.get("last_turn_at")) or 0) > COLD_HOURS
        placed["Blocked" if block else "Done" if left == "all done" else "Idle"].append(("", key, note, [("–", block or label + (f" · {since}h" if since is not None else ""), "2" if block else "34", extra, block or label, [], None, sid, reset, None)]))
    ctl = [(r, *status("control", r)[:3]) for r in sorted((r for r in records if r.get("kind") == "control" and r["state"] != "ended" and alive(r.get("pid"))), key=lambda r: r["session_id"])]
    return ctl, placed, stale   # ctl: [(record, wide state, colour, tree label)]

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
        for _, key, note, lines in sorted(placed[g], key=lambda t: (t[0], t[1])):
            for i, (sid, state, code, extra, *_) in enumerate(lines):
                out.append(f"  {C('36', key.ljust(kw))}  {sid.ljust(8)}  {C(code, state.ljust(sw))}  {note}".rstrip())
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
    and line number -> {key, goal} for each row of a goal in the open workstream's panel; nothing else.
    A row whose glyph is ↻ adds reset_x, the glyph's column. `current` is open_key(), resolved by the caller: its rows carry the bar,
    and its charter fills the rows left under the footer. Height, like width, is read per call."""
    hits = {} if hits is None else hits
    C = lambda code, s: f"\033[{code}m{s}\033[0m" if color and code else s
    W, (ctl, placed, stale) = pane_width(width), place(records)
    def row(lead, glyph, name, state, code, name_code=None, dup=False, cap=None, on=False):
        # Name truncates with …; state right-aligns to W, capped at a third of it unless `cap` says otherwise. `!` after a key marks [duplicate].
        # `on`: the bar takes column 0 of the lead, so the open row is as wide as any other.
        lead, lead_code, name_code = ("▌" + lead[1:], "1", f"1;{name_code}") if on else (lead, "2", name_code)
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
        sessions = [(key, note.startswith("[duplicate]"), l) for _, key, note, ls in sorted(placed[g], key=lambda t: (t[0], t[1])) for l in ls]
        for i, (key, dup, (tag, _, code, _, short, kids, guess, sid, reset, since)) in enumerate(sessions):
            # A stale row's status is that it is stale, so its glyph is the reset button. A dormant row (tag "–", nothing live, a retired session or none) is ◌; a live one keeps ○.
            glyph = "↻" if reset else "⏸" if g == "Blocked" else {"31": "●", "33": "●", "32": "▶"}.get(code, "◌" if tag == "–" else "○")
            hits[len(out)] = {"key": key, "session_id": sid, "glyph": glyph} | ({"reset_x": 2} if reset else {}) | need(code, since)   # reset_x: the column after the two-column lead
            if g == "Blocked":   # a block's label may take half the row, and what it waits on gives way before its age
                what, tail = re.fullmatch(r"(.*?)((?: · \d+[mhd])?)", short).groups(); short = oneline(what, W // 2 - len(tail)) + tail
            # ponytail: the title names a key, not a session, so every row of a [duplicate] key carries the bar
            out.append(row("  ", glyph, key, short, tone(code), "36", dup, cap=W // 2 if g == "Blocked" else None, on=key == current))
            trunk = "│" if i < len(sessions) - 1 else " "   # the group's trunk runs on while sessions follow
            if i == 0 or sessions[i - 1][0] != key: out += noted(key, trunk)   # a [duplicate] key's rows run together: its note shows once
            if guess: out.append(row(f"  {trunk} ", "→", f"{guess}?", "", "2"))
            for j, c in enumerate(kids):
                out.append(row(f"  {trunk} ", "└" if j == len(kids) - 1 else "├", str(c.get("name")), str(c.get("state")),
                               "32" if c.get("state") == "running" else "2"))
    count = f"stale ({len(stale)})"; full = f"{count}: " + ", ".join(sorted(stale))
    out += [C("2", "─" * W), C("2", full if stale and len(full) <= W else count)]
    if q := quota(W): out += [C("2", "─" * W), C(*q)]
    # The watch prints a newline after the board, so a board as tall as the pane would scroll its first row away.
    for row, goal in details(current, W, (height or shutil.get_terminal_size().lines) - 1 - len(out), C):
        if goal: hits[len(out)] = {"key": current, "goal": goal}
        out.append(row)
    return "\n".join(out)

def details(key, W, room, C):
    """The charter of `key`, the workstream open in the right pane, in `room` rows: a double rule, its key and what is left of its goals,
    then what it is blocked on and Ian's note, its purpose, and its open goals at two rows apiece, a blank row around the key and between
    the rest, the last row cut with … when they run past. Nothing for a key with no charter, such as control or an unassigned session, or
    with no room for a row under the key. Returns [(row, goal)]: goal is the text of the goal a row shows, which a click on it sends."""
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
    name = oneline(key, W - len(left) - 1)
    head = [C("2", "═" * W), "", C("1;36", name) + " " * (W - len(name) - len(left)) + C("2", left), ""]
    return [(row, None) for row in head] + [(C(code, l), goal) for code, l, goal in body]

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
    job = reg.get("jobId") or sid[:8]   # attach takes the short job id; the full session id is "no job matching"
    rec = (rec or {}) | {"session_id": sid}
    busy = working(rec)   # mid-turn, or a sub-agent running: never killed
    if control and not busy and (hours_since(rec.get("last_turn_at")) or 0) > COLD_HOURS: return ("restart", job)
    if reset and not busy and rec.get("waiting_for") in (None, "replied"): return ("restart", job)   # a permission or a question waits for Ian's answer
    return ("attach", job)   # a live session is opened as it stands: a click on its row never types into it

def tree_rows(goals=False):
    """board/tree-rows.json: {line: entry} as the watch last drew the sidebar, or {} without one. Only a click acts on a goal's entry,
    so the rest, the steps and the menu, see the rows alone unless `goals`."""
    try: hits = json.loads((board_dir() / "tree-rows.json").read_text())
    except (OSError, ValueError): return {}
    return hits if goals else {n: e for n, e in hits.items() if "goal" not in e}

def judge(entry, x=None, rows=listing):
    """decide() for a tree-rows.json entry, with its board record and live registry read here: what a click on its row would do."""
    rec = load_record(entry["session_id"]) if entry and entry.get("session_id") else None
    pid = (rec or {}).get("pid")
    return decide(entry, reg_of(pid) if alive(pid) else None, rec, x, rows)

def open_row(line, target, x=None):
    """`board.py open <line> <pane> [<x>]`: the sidebar's click, run by tmux, x its column."""
    entry = tree_rows(goals=True).get(str(line))
    if entry and "goal" in entry: return focus(entry, target)
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
        p = subprocess.run(["claude", "--bg", "-n", act[1], *args], capture_output=True, text=True, timeout=120)
        o = ANSI.sub("", p.stdout + p.stderr); m = re.search(r"backgrounded · (\S+)", o)   # the id is coloured even into a pipe
        act = ("attach", m.group(1)) if m else ("message", f"{act[1]}: {oneline(o) or 'dispatch printed nothing'}")
        lock.touch() if m else lock.unlink(missing_ok=True)   # the hold runs from the dispatch's end; a failed one frees the key for a retry
    if act and act[0] == "attach":
        tmux("respawn-pane", "-k", "-t", target, f"claude attach {act[1]}")
        tmux("set", "-p", "-t", target, "@ws_name", entry["key"])   # the pane's title line (cc's pane-border-format) names what is open
        mark_open(entry["key"])
    elif act: tmux("display-message", act[1])

def step(way, target):
    """`board.py go next|prev|need <pane>`: from the key open in `target`, open the next (or previous) row in the sidebar's order, wrapping,
    whose click would attach: a live background session, or a retired one to revive. A row whose click would dispatch, restart or
    only explain is passed over, so a step never starts a session. With nothing open, next starts at the top and prev at the bottom.
    need steps the same way through only the rows waiting on Ian: the ones asking him for an answer, then his unread replies, each oldest first."""
    import functools, subprocess
    hits = tree_rows(); order = [hits[n] for n in sorted(hits, key=int)]
    if way == "prev": order.reverse()
    # ponytail: an unread row, once opened, leaves the list at the next redraw, so the press after it starts again from the top
    if way == "need": order = sorted((e for e in order if e.get("need")), key=lambda e: (e["need"] != "asks", e.get("since") or ""))
    here = open_key(target); at = next((i for i, e in enumerate(order) if e["key"] == here), -1)
    rows = functools.cache(listing)   # read once a step, and only for a row whose session is not live
    for e in order[at + 1:] + order[:at + 1]:
        # ponytail: a [duplicate] key is one stop, as the title names a key, not a session
        if e["key"] != here and (act := judge(e, rows=rows)) and act[0] == "attach": return perform(e, act, target)
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
    name = lambda e: " ".join(filter(None, (e.get("glyph"), e["key"]))).replace("#", "##")
    items = [a for i, n in enumerate(lines) for a in (name(hits[n]), MENU_KEYS[i:i + 1], click(n))]
    at = next((i for i, n in enumerate(lines) if hits[n]["key"] == here), None)
    # -M: a menu opened from a key ignores the mouse without it.
    # ponytail: tmux draws no menu taller than the terminal, and nothing pages this one
    subprocess.run(["tmux", "display-menu", "-M", "-c", client, *(["-C", str(at)] if at is not None else []), *items], capture_output=True)

def note(client, target):
    """`board.py note <client> <pane>`: Option+e. A prompt on `client` for Ian's note on the workstream open in `target`, holding the current one.
    Enter runs save_note() for it, and an empty line clears it."""
    import shlex, subprocess
    tmux = lambda *a: subprocess.run(["tmux", *a], capture_output=True, text=True)
    if (key := open_key(target)) is None: return tmux("display-message", "-c", client, "workstreams: open a session first")
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

def main():
    if sys.argv[1:2] == ["charter"]: print(json.dumps(charter(sys.argv[2], "--refresh" in sys.argv[3:]))); return
    if sys.argv[1:2] == ["event"]: event(sys.argv[2], json.load(sys.stdin)); return
    if sys.argv[1:2] == ["write"]: print(json.dumps(write(sys.argv[2], json.load(sys.stdin)))); return
    if sys.argv[1:2] == ["mirror"]: sync(sys.argv[2]); return
    if sys.argv[1:2] == ["tick"]: sync(sys.argv[2], sys.argv[3]); return
    if sys.argv[1:2] == ["open"]:
        open_row(int(sys.argv[2]), sys.argv[3], int(sys.argv[4]) if sys.argv[4:] else None); return
    if sys.argv[1:2] == ["go"]: step(*sys.argv[2:4]); return
    if sys.argv[1:2] == ["menu"]: menu(*sys.argv[2:4]); return
    if sys.argv[1:2] == ["note"]: note(*sys.argv[2:4]); return
    if sys.argv[1:2] == ["save-note"]: save_note(*sys.argv[2:4]); return
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
        stamp = lambda: opened.exists() and opened.stat().st_mtime_ns
        try:
            while True:
                last = stamp()   # taken before the draw, so a click landing mid-draw still wakes the next one
                print("\033[H\033[2J" + once() + ("" if narrow else f"\n\nupdated {time.strftime('%H:%M:%S')} · Ctrl-C to quit"), flush=True)
                # Opening a session rewrites open.json: redraw at once, so the mark follows the click rather than the 3s tick.
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
