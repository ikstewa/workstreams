import json, os, re, shlex, shutil, subprocess, sys, tempfile, time, unittest
from pathlib import Path

HOOK = Path(__file__).resolve().parents[1] / "hooks" / "board.py"

class BoardTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.proj = "/Users/x/dev/myproject"
        # A fake `claude` first on PATH: render()'s agents listing is hermetic, and each test can set its own.
        self.bin = Path(self.home) / "bin"; self.bin.mkdir(); self.listing("[]")
        self.env = {**os.environ, "HOME": self.home, "CLAUDE_PROJECT_DIR": self.proj, "CLAUDE_PID": "4242", "PATH": f"{self.bin}:{os.environ['PATH']}"}
        self.env.pop("CLAUDE_CODE_TASK_LIST_ID", None)   # the session running the suite may have one; each test names its own list
        for k in ("TMUX", "TMUX_PANE"): self.env.pop(k, None)   # the suite may run inside Ian's tmux: no test reaches its server
        sessions = Path(self.home) / ".claude" / "sessions"; sessions.mkdir(parents=True)
        (sessions / "4242.json").write_text(json.dumps({"pid": 4242, "sessionId": "s1", "name": "PAYMENTS_API", "cwd": self.proj, "kind": "interactive"}))
        self.board = Path(self.home) / ".claude" / "projects" / "-Users-x-dev-myproject" / "board"

    def fire(self, event):
        p = subprocess.run([sys.executable, str(HOOK)], input=json.dumps({"session_id": "s1", "cwd": self.proj, **event}), capture_output=True, text=True, env=self.env)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout

    def listing(self, stdout, code=0):
        f = self.bin / "claude"; f.write_text(f"#!/bin/sh\ncat <<'EOF'\n{stdout}\nEOF\nexit {code}\n"); f.chmod(0o755)

    def group_of(self, text, key):
        """The group heading a workstream's row sits under, or None when it has no row."""
        group = None
        for line in text.splitlines():
            if line and not line.startswith(" "): group = line
            elif line.startswith(f"  {key} "): return group
        return None

    def rec(self):
        return json.loads((self.board / "s1.json").read_text())

    def put_rec(self, rec, sid="s1"):
        (self.board / f"{sid}.json").write_text(json.dumps(rec))

    # --- the record: `board.py event`, one transition the mod forwards ---

    def event(self, ev, sid="s1", env=None, where=None, **fields):
        """`board.py event <sid>` as the mod runs it: the event on stdin with the session's cwd, `where` when given, which is also the process's."""
        p = subprocess.run([sys.executable, str(HOOK), "event", sid], input=json.dumps({"event": ev, "cwd": where or self.proj, **fields}),
                           capture_output=True, text=True, env=env or self.env, cwd=where)
        self.assertEqual((p.returncode, p.stdout), (0, ""), p.stderr)

    def test_a_start_writes_the_record_from_the_registry(self):
        self.event("start")
        r = self.rec()
        self.assertEqual((r["name"], r["pid"], r["kind"], r["workstream"]), ("PAYMENTS_API", 4242, "active", "PAYMENTS_API"))
        self.assertEqual((r["state"], r["repo"], r["cwd"]), ("idle", "myproject", self.proj))

    def test_a_turns_start_sets_busy_and_clears_waiting(self):
        self.event("start"); self.event("ask", kind="permission")
        self.assertEqual(self.rec()["waiting_for"], "permission")
        self.event("turn.start")
        r = self.rec(); self.assertEqual((r["state"], r["waiting_for"], r["waiting_since"]), ("busy", None, None))
        self.assertIsNotNone(r["last_turn_at"])

    def test_an_ask_waits_on_a_permission_or_a_question_and_keeps_an_earlier_since(self):
        self.event("turn.start"); self.event("ask", kind="permission")
        r = self.rec(); self.assertEqual((r["state"], r["waiting_for"]), ("waiting", "permission")); self.assertIsNotNone(r["waiting_since"])
        self.put_rec(r | {"waiting_since": "2026-01-01T00:00:00Z"})
        self.event("ask", kind="question")   # a second call asks while the first waits
        r = self.rec(); self.assertEqual((r["state"], r["waiting_for"], r["waiting_since"]), ("waiting", "question", "2026-01-01T00:00:00Z"))
        self.event("turn.start"); self.event("ask")   # anything but a question is a permission
        self.assertEqual(self.rec()["waiting_for"], "permission")

    def test_answered_clears_only_a_wait_on_ian_and_puts_back_what_it_covered(self):
        self.event("turn.start")
        for kinds in (["permission"], ["question"], ["permission", "question"]):
            for kind in kinds: self.event("ask", kind=kind)
            self.event("answered")
            r = self.rec(); self.assertEqual((r["state"], r["waiting_for"], r["waiting_since"]), ("busy", None, None), kinds)
        self.event("turn.complete", reason="answer", answer="Done."); replied = self.rec()
        self.event("answered")   # a reply waits on Ian, but no call's answer ends it
        r = self.rec(); self.assertEqual((r["state"], r["waiting_for"], r["waiting_since"]), ("waiting", "replied", replied["waiting_since"]))
        # A sub-agent's call that asks after the turn has ended: its answer puts the reply back, not busy.
        self.event("ask", kind="permission"); self.assertEqual(self.rec()["waiting_for"], "permission")
        self.event("answered")
        r = self.rec(); self.assertEqual((r["state"], r["waiting_for"], r["waiting_since"]), ("waiting", "replied", replied["waiting_since"]))
        self.assertNotIn("before_ask", r)

    def test_a_turns_end_waits_on_ian_with_its_answer_and_an_interrupt_settles(self):
        self.event("turn.start"); self.event("turn.complete", reason="answer", answer="x" * 500)
        r = self.rec(); self.assertEqual((r["state"], r["waiting_for"], r["last_message"]), ("waiting", "replied", "x" * 280))
        self.assertEqual(r["waiting_since"], r["last_turn_at"]); self.assertIsNotNone(r["last_turn_at"])
        for why in ("refusal", "error"):   # an empty answer keeps the last one
            self.event("turn.start"); self.event("turn.complete", reason=why, answer="")
            r = self.rec(); self.assertEqual((r["state"], r["waiting_for"], r["last_message"]), ("waiting", "replied", "x" * 280), why)
        self.event("turn.start"); self.event("ask", kind="permission")
        self.put_rec(self.rec() | {"last_turn_at": "2026-01-01T00:00:00Z"})
        self.event("turn.complete", reason="aborted", answer="")
        r = self.rec(); self.assertEqual((r["state"], r["waiting_for"], r["waiting_since"], r["last_message"]), ("idle", None, None, "x" * 280))
        self.assertGreater(r["last_turn_at"], "2026-01-01T00:00:00Z")

    def test_children_tracked(self):
        self.event("start")
        self.event("child.start", agent_id="a1", name="Explore")
        self.put_rec(self.rec() | {"children": [{"agent_id": "a1", "name": "Explore", "state": "paused", "last_message": None}]})
        self.event("child.start", agent_id="a1", name="Explore")   # twice for one id: its row, running again, never a second
        self.assertEqual(self.rec()["children"], [{"agent_id": "a1", "name": "Explore", "state": "running", "last_message": None}])
        self.event("child.stop", agent_id="a9"); self.assertEqual(len(self.rec()["children"]), 1)
        self.event("child.stop", agent_id="a1"); self.assertEqual(self.rec()["children"], [])
        self.event("child.start", agent_id="a1", name="Explore")   # resumed
        self.assertEqual([(c["agent_id"], c["state"]) for c in self.rec()["children"]], [("a1", "running")])

    def test_an_end_records_its_reason(self):
        self.event("start"); self.event("end", reason="prompt_input_exit")
        r = self.rec(); self.assertEqual((r["state"], r["ended_reason"]), ("ended", "prompt_input_exit"))

    def test_a_start_over_a_record_keeps_its_turn_times_and_starts_the_rest_over(self):
        self.event("start"); self.event("turn.start"); self.event("child.start", agent_id="a1", name="Explore")
        self.event("turn.complete", reason="answer", answer="Done."); self.event("end", reason="resume")
        before = self.rec()
        self.event("start")
        r = self.rec()
        self.assertEqual((r["last_turn_at"], r["started_at"]), (before["last_turn_at"], before["started_at"]))
        self.assertEqual((r["state"], r["waiting_for"], r["waiting_since"], r["children"], r["ended_reason"]), ("idle", None, None, [], None))

    def test_a_record_follows_its_registry_file_and_keeps_its_identity_without_one(self):
        # Made before any registry file names the session: unnamed, until an event finds the file. Without the file it keeps what it has.
        reg = Path(self.home) / ".claude" / "sessions" / "4242.json"; text = reg.read_text(); reg.unlink()
        self.event("start"); r = self.rec(); self.assertEqual((r["name"], r["pid"]), ("unnamed", None))
        reg.write_text(text); self.event("turn.start")
        r = self.rec(); self.assertEqual((r["name"], r["pid"], r["kind"], r["workstream"]), ("PAYMENTS_API", 4242, "active", "PAYMENTS_API"))
        reg.unlink(); self.event("start")
        r = self.rec(); self.assertEqual((r["name"], r["pid"], r["workstream"]), ("PAYMENTS_API", 4242, "PAYMENTS_API"))

    def test_an_unknown_event_fails_for_the_mod_to_log(self):
        self.event("start"); before = self.rec()
        p = subprocess.run([sys.executable, str(HOOK), "event", "s1"], input=json.dumps({"event": "nope"}), capture_output=True, text=True, env=self.env)
        self.assertEqual(p.returncode, 1); self.assertIn("unknown event: 'nope'", p.stderr.splitlines()[-1])
        self.assertEqual(self.rec(), before)

    def test_unreadable_own_record_is_rebuilt(self):
        self.event("start")
        (self.board / "s1.json").write_text("{ not json")
        self.event("turn.complete", reason="answer", answer="x")
        r = self.rec(); self.assertEqual((r["state"], r["last_message"], r["name"]), ("waiting", "x", "PAYMENTS_API"))

    def test_own_record_missing_a_key_is_rebuilt(self):
        self.event("start")
        r = self.rec(); del r["waiting_since"]; del r["children"]
        self.put_rec(r)
        self.event("ask", kind="permission")
        r = self.rec()
        self.assertEqual(r["waiting_for"], "permission"); self.assertIsNotNone(r["waiting_since"])
        self.assertEqual(r["children"], [])

    def test_worktree_cwd_maps_to_same_board(self):
        wt = self.proj + "/.worktrees/web-app/PROJ-1/x"
        p = subprocess.run([sys.executable, str(HOOK), "event", "s1"], input=json.dumps({"event": "start", "cwd": wt}),
                           capture_output=True, text=True, env={**self.env, "CLAUDE_PROJECT_DIR": wt}, check=True)
        r = self.rec(); self.assertEqual((r["repo"], r["branch"], p.stdout), ("web-app", "PROJ-1/x", ""))

    def write_charter(self, name="PAYMENTS_API", created=None, extra=""):
        # created=None omits the field entirely, matching every pre-existing call site's fixture.
        ws = Path(self.home) / ".claude" / "projects" / "-Users-x-dev-myproject" / "workstreams"; ws.mkdir(parents=True, exist_ok=True)
        created_line = f"created: {created}\n" if created is not None else ""
        (ws / f"{name}.md").write_text(f"---\nname: project-payments\nworkstream: {name}\npurpose: Rebuild the API.\nfocus: PROJ-1\nstatus: active\n{created_line}{extra}---\n\nHandoff: tests green, push pending.\n")

    def test_rename_binds_on_the_next_prompt(self):
        # Adopting a session deep in its work: /rename must bind its record, and its charter must follow, without /clear.
        self.event("start")   # starts as PAYMENTS_API, no charter yet
        self.write_charter("ADOPTED")
        (Path(self.home) / ".claude" / "sessions" / "4242.json").write_text(json.dumps({"pid": 4242, "sessionId": "s1", "name": "ADOPTED", "cwd": self.proj}))
        self.event("turn.start")   # the rename's start, or any event after it
        r = self.rec(); self.assertEqual((r["name"], r["workstream"], r["kind"]), ("ADOPTED", "ADOPTED", "active"))
        self.assertIn("\nworkstream: ADOPTED\n", self.charter_of()["text"])
        # Once that pid's registry file names another session, s1's events keep the name it had.
        (Path(self.home) / ".claude" / "sessions" / "4242.json").write_text(json.dumps({"pid": 4242, "sessionId": "s9", "name": "control", "cwd": self.proj}))
        self.event("end", reason="other")
        self.assertEqual(self.rec()["name"], "ADOPTED")

    # --- the charter a bound session reads: `board.py charter`, which the mod appends as a conversation row ---

    def charter_of(self, sid="s1", *flags, env=None, cwd=None):
        p = subprocess.run([sys.executable, str(HOOK), "charter", sid, *flags], capture_output=True, text=True, env=env or self.env, cwd=cwd)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    def test_memory_dir_is_never_read(self):
        mem = Path(self.home) / ".claude" / "projects" / "-Users-x-dev-myproject" / "memory"; mem.mkdir(parents=True, exist_ok=True)
        (mem / "project-payments.md").write_text("---\nworkstream: PAYMENTS_API\npurpose: stale copy\n---\n")
        self.assertNotIn("text", self.charter_of())

    def test_the_settings_hook_injects_nothing(self):
        # The charter arrives as a conversation row from the mod, and the mod writes the record.
        self.write_charter(); self.write_charter("ADOPTED")
        self.assertEqual(self.fire({"hook_event_name": "SessionStart", "source": "startup"}), "")
        (Path(self.home) / ".claude" / "sessions" / "4242.json").write_text(json.dumps({"pid": 4242, "sessionId": "s1", "name": "ADOPTED", "cwd": self.proj}))
        self.assertEqual(self.fire({"hook_event_name": "UserPromptSubmit", "prompt": "carry on"}), "")
        self.assertFalse((self.board / "s1.json").exists())

    def test_a_settings_hook_left_from_before_the_mod_reads_nothing_and_does_nothing(self):
        # A session started before the mod took goal sync over runs its settings hooks until it reloads: they must stay silent.
        self.write_charter(extra=self.GOALS); before = self.charter_path().read_text()
        self.task_file("2", subject="Run the schema migration", status="completed", metadata={"workstream": "PAYMENTS_API", "workstream_goal": "Run the schema migration"})
        for ev in ({"hook_event_name": "SessionStart", "source": "startup"}, {"hook_event_name": "UserPromptSubmit", "prompt": "go"},
                   {"hook_event_name": "PostToolUse", "tool_name": "TaskUpdate", "tool_input": {"taskId": "2", "status": "completed"}}, {"hook_event_name": "Stop"}):
            self.assertEqual(self.fire(ev), "", ev)   # fire asserts exit 0
        self.assertEqual((self.charter_path().read_text(), sorted(self.tasks())), (before, ["2"]))
        self.assertEqual(list(self.board.glob("*.json")) if self.board.exists() else [], [])
        p = subprocess.Popen([sys.executable, str(HOOK)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env)
        try: self.assertEqual(p.wait(timeout=10), 0)   # its stdin never closes: a read would hold it
        finally: p.kill(); p.stdin.close()
        self.assertEqual((p.stdout.read(), p.stderr.read()), (b"", b"")); p.stdout.close(); p.stderr.close()

    def test_the_charter_puts_the_rules_ahead_of_the_frontmatter(self):
        self.write_charter(extra=self.GOALS)
        out = self.charter_of(); text, path = out["text"], str(self.charter_path())
        self.assertEqual({k: out[k] for k in ("key", "path", "name", "registry")},
                         {"key": "PAYMENTS_API", "path": path, "name": "PAYMENTS_API", "registry": str(Path(self.home) / ".claude" / "sessions" / "4242.json")})
        fm = re.match(r"---\n(.*?)\n---\n", self.charter_path().read_text(), re.S).group(1)
        self.assertTrue(text.startswith(f"# Workstream charter ({path})\n\n# Operating rules\n"), text[:200])
        self.assertLess(text.index("# Operating rules"), text.index(f"\n---\n{fm}\n---\n"))   # the frontmatter whole, after the rules
        self.assertTrue(text.endswith("\n\n# Record\nHandoff: tests green, push pending."), text[-200:])
        self.assertIn("Every substantive turn, add a record entry and set the focus (plan progress, in flight, next) with the mcp__workstreams__charter tool: "
                      "load it with ToolSearch first, and never edit the charter file.", text)
        self.assertIn("When new work appears, add it as a goal with the tool. Ask before any push.", text)
        self.assertIn("The workstream is private to this machine: never mention it, its charter, goals, record or block in anything that leaves the machine", text)
        self.assertIn("When a sub-agent writes any of these for you, tell it the same.", text)
        self.assertIn("outside the charter's scope", text)
        self.assertIn("set a block with the tool and clear it once it clears", text); self.assertIn("with a block set, first check whether it still holds", text)
        self.assertIn("note: line is Ian's own: read it. The tool cannot change it.", text)
        self.assertIn("when you start on one, set its task to in_progress with TaskUpdate", text)

    def test_a_long_record_keeps_its_newest_part_from_a_line_start(self):
        self.write_charter(extra=self.GOALS); p = self.charter_path()
        lines = [f"line {i:05d} " + "x" * (i % 70) for i in range(1500)]
        p.write_text(p.read_text().replace("Handoff: tests green, push pending.", "\n".join(lines)))
        body = "\n".join(lines); text = self.charter_of()["text"]
        self.assertIn("\n---\n" + re.match(r"---\n(.*?)\n---\n", p.read_text(), re.S).group(1) + "\n---\n", text)
        head, kept = text.split("\n\n# Record\n")[1].split("\n", 1)
        self.assertEqual(head, f"(earlier record omitted: read {p})")
        self.assertTrue(body.endswith("\n" + kept)); self.assertTrue(kept.startswith("line "))
        self.assertLessEqual(len(kept), 30_000); self.assertGreater(len(kept), 30_000 - 100)
        p.write_text(p.read_text().replace("\n".join(lines), "\n".join(lines[-100:])))   # under budget: whole, no omission line
        self.assertTrue(self.charter_of()["text"].endswith("\n\n# Record\n" + "\n".join(lines[-100:])))

    def test_a_refresh_carries_no_record(self):
        self.write_charter(extra=self.GOALS)
        full, refresh = self.charter_of()["text"], self.charter_of("s1", "--refresh")["text"]
        self.assertEqual(full, refresh + "\n\n# Record\nHandoff: tests green, push pending.")

    def test_only_an_active_bound_session_gets_a_charter(self):
        # control, a worker, an unnamed session and a key with no charter get their registry file and no text; an unknown id gets {}.
        self.write_charter(); self.write_charter("OTHER")
        reg = str(Path(self.home) / ".claude" / "sessions" / "4242.json")
        for name in ("control", "PAYMENTS_API work: fix thing", None, "NOCHARTER"):
            (Path(self.home) / ".claude" / "sessions" / "4242.json").write_text(json.dumps({"pid": 4242, "sessionId": "s1", "name": name, "cwd": self.proj}))
            self.assertEqual(self.charter_of(), {"registry": reg, "name": name}, name)
        self.assertEqual(self.charter_of("s-unknown"), {})

    def test_the_charter_is_found_by_session_id_not_claude_pid(self):
        # A mod-started process has no CLAUDE_PID; a settings hook's CLAUDE_PID may be another session's.
        self.write_charter(); self.write_charter("OTHER")
        (Path(self.home) / ".claude" / "sessions" / "777.json").write_text(json.dumps({"pid": 777, "sessionId": "s2", "name": "OTHER", "cwd": self.proj}))
        bare = {k: v for k, v in self.env.items() if k != "CLAUDE_PID"}
        self.assertEqual((self.charter_of("s1", env=bare)["key"], self.charter_of("s2", env=bare)["key"]), ("PAYMENTS_API", "OTHER"))
        self.assertEqual(self.charter_of("s2")["key"], "OTHER")   # CLAUDE_PID=4242 names s1

    def test_the_charter_resolves_from_the_cwd_of_a_worktree(self):
        # The mod may start board.py with no CLAUDE_PROJECT_DIR: the cwd decides, a .worktrees path folded to its root as the settings hook folds it.
        root = Path(os.path.realpath(self.home)) / "proj"; wt = root / ".worktrees" / "web-app" / "PROJ-1" / "x"; wt.mkdir(parents=True)
        bare = {k: v for k, v in self.env.items() if k != "CLAUDE_PROJECT_DIR"}
        self.event("start", env={**self.env, "CLAUDE_PROJECT_DIR": str(wt)}, where=str(wt))   # as the mod starts it
        slug = re.sub(r"[^A-Za-z0-9-]", "-", str(root)); proj = Path(self.home) / ".claude" / "projects" / slug
        self.assertTrue((proj / "board" / "s1.json").exists())   # where the mod keeps this session
        (proj / "workstreams").mkdir(); (proj / "workstreams" / "PAYMENTS_API.md").write_text("---\nworkstream: PAYMENTS_API\n---\nHandoff.\n")
        self.assertEqual(self.charter_of(env=bare, cwd=wt)["path"], str(proj / "workstreams" / "PAYMENTS_API.md"))

    def test_a_prompt_long_after_the_last_turn_passes(self):
        # No prompt is held, however old the last turn: the sidebar's ↻ is where starting fresh is chosen.
        self.write_charter()
        self.event("start")
        r = self.rec(); r["last_turn_at"] = "2020-01-01T00:00:00Z"; (self.board / "s1.json").write_text(json.dumps(r))
        p = subprocess.run([sys.executable, str(HOOK)], input=json.dumps({"session_id": "s1", "cwd": self.proj, "hook_event_name": "UserPromptSubmit", "prompt": "carry on"}), capture_output=True, text=True, env=self.env)
        self.assertEqual((p.returncode, p.stdout, p.stderr), (0, "", ""))
        self.event("turn.start")
        r = self.rec(); self.assertEqual(r["state"], "busy"); self.assertGreater(r["last_turn_at"], "2020-01-01T00:00:00Z"); self.assertNotIn("hold_prompt", r)

    def become_worker(self, workstream="PAYMENTS_API"):
        (Path(self.home) / ".claude" / "sessions" / "4242.json").write_text(json.dumps({"pid": 4242, "sessionId": "s1", "name": f"{workstream} work: fix thing", "cwd": self.proj}))

    def test_worker_with_charter_records_as_a_worker(self):
        # A worker's kind is never "active", even when a charter exists to match its key against.
        self.write_charter(); self.become_worker()
        self.event("start")
        self.assertEqual(self.rec()["kind"], "worker")

    def become_control(self):
        (Path(self.home) / ".claude" / "sessions" / "4242.json").write_text(json.dumps({"pid": 4242, "sessionId": "s1", "name": "control", "cwd": self.proj}))

    def test_control_records_as_control(self):
        # control's first prompt is /workstreams:board; its start writes no control.json.
        self.write_charter(); self.become_control()
        self.event("start")
        self.assertEqual(self.rec()["kind"], "control"); self.assertFalse((self.board / "control.json").exists())

    def test_malformed_board_record_names_the_file(self):
        self.write_charter()
        self.board.mkdir(parents=True, exist_ok=True)
        (self.board / "zz-stale.json").write_text("{ not json")
        self.assertEqual(self.render().strip(), "workstreams: board record zz-stale.json unreadable")

    def test_board_record_missing_a_required_key_names_the_file(self):
        self.write_charter()
        self.board.mkdir(parents=True, exist_ok=True)
        (self.board / "zz-old-shape.json").write_text(json.dumps({"session_id": "zz", "name": "PAYMENTS_API", "workstream": "PAYMENTS_API"}))
        self.assertEqual(self.render().strip(), "workstreams: board record zz-old-shape.json missing state, waiting_for")

    def test_board_record_with_a_non_integer_pid_names_the_file(self):
        self.write_charter()
        self.board.mkdir(parents=True, exist_ok=True)
        (self.board / "zz-bad-pid.json").write_text(json.dumps({"session_id": "zz", "name": "PAYMENTS_API", "workstream": "PAYMENTS_API",
                                                                "pid": "4242", "state": "idle", "waiting_for": None}))
        self.assertEqual(self.render().strip(), "workstreams: board record zz-bad-pid.json pid is not an integer")

    def test_dead_pid_is_not_live_while_its_registry_file_remains(self):
        # Measured 2026-09-21 and 2026-09-22: a reaper removes a SIGKILLed session's registry
        # file on its own, seconds to tens of seconds later. The file alone would render this
        # record as a live session.
        self.write_charter()
        dead = subprocess.Popen([sys.executable, "-c", ""]); dead.wait()
        (Path(self.home) / ".claude" / "sessions" / f"{dead.pid}.json").write_text(json.dumps({"pid": dead.pid, "name": "PAYMENTS_API"}))
        self.board.mkdir(parents=True, exist_ok=True)
        hour_ago = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600))
        (self.board / "s9.json").write_text(json.dumps({"session_id": "s9", "pid": dead.pid, "name": "PAYMENTS_API",
                                                        "workstream": "PAYMENTS_API", "state": "idle", "waiting_for": None,
                                                        "last_turn_at": hour_ago, "last_message": None}))
        self.assertEqual(self.group_of(self.render("--no-color"), "PAYMENTS_API"), "Idle")

    def test_alive_true_path_renders_live_not_dormant(self):
        # Every other alive() test exercises the dead or absent-pid paths. Without this, an
        # alive() that returned False unconditionally would still pass the whole suite.
        self.write_charter()
        me = os.getpid()   # this test process, genuinely running for the duration of the call
        (Path(self.home) / ".claude" / "sessions" / f"{me}.json").write_text(json.dumps({"pid": me, "name": "PAYMENTS_API"}))
        self.board.mkdir(parents=True, exist_ok=True)
        (self.board / "s9.json").write_text(json.dumps({"session_id": "s9", "pid": me, "name": "PAYMENTS_API",
                                                        "workstream": "PAYMENTS_API", "state": "idle", "waiting_for": None,
                                                        "last_turn_at": None, "last_message": None}))
        self.assertRegex(self.render("--no-color"), r"\n  PAYMENTS_API +s9 +idle · no turn yet ")

    def test_registry_for_other_session_is_not_trusted(self):
        # CLAUDE_PID's registry names session "s1" as control (set up here), but this event is
        # about a session the board has never seen, and no registry file names it: identity is
        # found by session id, never by the pid of whoever ran the event. control is the
        # highest-consequence name a wrongly adopted identity could take — it would take the
        # sidebar's Control row.
        self.become_control()
        self.event("end", sid="s2", reason="other")
        r = json.loads((self.board / "s2.json").read_text())
        self.assertEqual(r["name"], "unnamed"); self.assertIsNone(r["pid"]); self.assertEqual(r["kind"], "active")

    def test_main_exits_zero_on_garbage_stdin(self):
        for garbage in ("", "not json"):
            p = subprocess.run([sys.executable, str(HOOK)], input=garbage, capture_output=True, text=True, env=self.env)
            self.assertEqual(p.returncode, 0, p.stderr)

    def test_recent_turn_renders_in_full_not_stale(self):
        self.write_charter()
        self.board.mkdir(parents=True, exist_ok=True)
        hour_ago = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600))
        (self.board / "s9.json").write_text(json.dumps({"session_id": "s9", "pid": None, "name": "PAYMENTS_API",
                                                        "workstream": "PAYMENTS_API", "state": "idle", "waiting_for": None,
                                                        "last_turn_at": hour_ago, "last_message": None}))
        ctx = self.render("--no-color")
        self.assertEqual(self.group_of(ctx, "PAYMENTS_API"), "Idle"); self.assertNotIn("stale (", ctx)

    def test_workstream_without_any_record_is_stale(self):
        self.write_charter()
        ctx = self.render("--no-color")
        self.assertIn("stale (1, no session in 3d): PAYMENTS_API", ctx)
        self.assertIsNone(self.group_of(ctx, "PAYMENTS_API"))

    def test_old_last_turn_is_stale(self):
        self.write_charter()
        self.board.mkdir(parents=True, exist_ok=True)
        (self.board / "s9.json").write_text(json.dumps({"session_id": "s9", "pid": None, "name": "PAYMENTS_API",
                                                        "workstream": "PAYMENTS_API", "state": "idle", "waiting_for": None,
                                                        "last_turn_at": "2020-01-01T00:00:00Z", "last_message": None}))
        ctx = self.render("--no-color")
        self.assertIn("stale (1, no session in 3d): PAYMENTS_API", ctx)
        self.assertIsNone(self.group_of(ctx, "PAYMENTS_API"))

    def test_nothing_stale_omits_the_stale_line(self):
        self.write_charter()
        self.board.mkdir(parents=True, exist_ok=True)
        hour_ago = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600))
        (self.board / "s9.json").write_text(json.dumps({"session_id": "s9", "pid": None, "name": "PAYMENTS_API",
                                                        "workstream": "PAYMENTS_API", "state": "idle", "waiting_for": None,
                                                        "last_turn_at": hour_ago, "last_message": None}))
        self.assertNotIn("stale", self.render("--no-color"))

    def test_live_session_wins_over_staleness(self):
        # place() only computes staleness for a workstream with nothing live — a live session must render in
        # full even when its last_turn_at would, on the staleness formula alone, read as stale.
        # First case is the one that makes this matter: a session between SessionStart and its
        # first prompt has last_turn_at: None, which reads stale by that formula alone.
        self.write_charter()
        me = os.getpid()   # this test process, genuinely running for the duration of the call
        (Path(self.home) / ".claude" / "sessions" / f"{me}.json").write_text(json.dumps({"pid": me, "name": "PAYMENTS_API"}))
        self.board.mkdir(parents=True, exist_ok=True)
        record = {"session_id": "s9", "pid": me, "name": "PAYMENTS_API", "workstream": "PAYMENTS_API",
                  "state": "idle", "waiting_for": None, "last_turn_at": None, "last_message": None}
        (self.board / "s9.json").write_text(json.dumps(record))
        ctx = self.render("--no-color")
        self.assertRegex(ctx, r"\n  PAYMENTS_API +s9 +idle · no turn yet "); self.assertNotIn("stale (", ctx)

        # Same gate, with a last_turn_at old enough to be stale on its own.
        record["last_turn_at"] = "2020-01-01T00:00:00Z"
        (self.board / "s9.json").write_text(json.dumps(record))
        ctx = self.render("--no-color")
        self.assertRegex(ctx, r"\n  PAYMENTS_API +s9 +idle · [\d.]+h · cold "); self.assertNotIn("stale (", ctx)

    def test_charter_created_today_with_no_session_renders_full_row(self):
        # The case the whole change exists for: a charter just written has no session record at
        # all, and must not read as stale from birth.
        today = time.strftime("%Y-%m-%d", time.gmtime())
        self.write_charter(created=today)
        ctx = self.render("--no-color")
        self.assertEqual(self.group_of(ctx, "PAYMENTS_API"), "Idle"); self.assertNotIn("stale (", ctx)

    def test_charter_created_long_ago_with_no_session_is_stale(self):
        old = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 30 * 86400))
        self.write_charter(created=old)
        ctx = self.render("--no-color")
        self.assertIn("stale (1, no session in 3d): PAYMENTS_API", ctx)
        self.assertIsNone(self.group_of(ctx, "PAYMENTS_API"))

    def test_charter_without_created_field_and_no_session_is_stale(self):
        self.write_charter()   # no created field at all
        ctx = self.render("--no-color")
        self.assertIn("stale (1, no session in 3d): PAYMENTS_API", ctx)
        self.assertIsNone(self.group_of(ctx, "PAYMENTS_API"))

    def test_charter_with_unparseable_created_and_no_session_is_stale(self):
        self.write_charter(created="not-a-date")
        ctx = self.render("--no-color")
        self.assertIn("stale (1, no session in 3d): PAYMENTS_API", ctx)
        self.assertIsNone(self.group_of(ctx, "PAYMENTS_API"))

    # --- render: the board /workstreams:board prints ---

    def render(self, *flags):
        p = subprocess.run([sys.executable, str(HOOK), "render", *flags], capture_output=True, text=True, env=self.env)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout

    def put(self, sid, ws, pid=None, **kw):
        self.board.mkdir(parents=True, exist_ok=True)
        rec = {"session_id": sid, "pid": pid, "name": ws, "workstream": ws, "kind": "active", "state": "idle",
               "waiting_for": None, "waiting_since": None, "last_turn_at": None, "last_message": None, "children": []} | kw
        (self.board / f"{sid}.json").write_text(json.dumps(rec))

    def me(self):
        # this test process: genuinely running for the call, with a registry file, so alive() says live
        me = os.getpid(); (Path(self.home) / ".claude" / "sessions" / f"{me}.json").write_text(json.dumps({"pid": me})); return me

    def job(self, sid, **in_flight):
        """sid's job file, as Claude Code keeps one for a background session: its inFlight counts, nothing unless `in_flight` says."""
        d = Path(self.home) / ".claude" / "jobs" / sid[:8]; d.mkdir(parents=True, exist_ok=True)
        (d / "state.json").write_text(json.dumps({"state": "blocked", "tempo": "idle", "inFlight": {"tasks": 0, "queued": 0, "kinds": [], "drainableMonitors": 0} | in_flight}))
        return d / "state.json"

    def test_render_places_each_group(self):
        me, hour_ago = self.me(), time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600))
        for k in ("WAIT", "BUSY", "IDLE", "DORM", "OLD"): self.write_charter(k)
        self.put("s-wait", "WAIT", me, state="waiting", waiting_for="permission", waiting_since=hour_ago)
        self.put("s-busy", "BUSY", me, state="busy", children=[{"agent_id": "a1", "name": "Explore", "state": "running"}])
        self.put("s-idle", "IDLE", me, last_turn_at=hour_ago)
        self.put("s-dorm", "DORM", None, last_turn_at=hour_ago)
        self.put("s-loose", "loose", me)
        self.put("s-ask", "asking", me, state="waiting", waiting_for="question")
        self.put("s-gone", "BUSY", None, state="waiting")   # dead: never listed live
        out = self.render("--no-color")
        self.assertEqual({k: self.group_of(out, k) for k in ("WAIT", "asking", "BUSY", "IDLE", "DORM", "loose", "OLD")},
                         {"WAIT": "Active", "asking": "Unassigned", "BUSY": "Active", "IDLE": "Active", "DORM": "Idle", "loose": "Unassigned", "OLD": None})
        heads = [l for l in out.splitlines() if l in ("Active", "Idle", "Unassigned")]
        self.assertEqual(heads, ["Active", "Idle", "Unassigned"])
        self.assertIn("\n      · Explore a1 running\n", out)
        self.assertIn("stale (1, no session in 3d): OLD", out)

    def test_render_lists_a_live_control_session(self):
        me = self.me(); self.write_charter()
        self.put("c-live", "control", me, kind="control", workstream=None)
        self.put("c-dead", "control", None, kind="control", workstream=None)
        out = self.render("--no-color")
        self.assertEqual(self.group_of(out, "control"), "Control")
        self.assertIn("c-live", out); self.assertNotIn("c-dead", out)
        self.put("c-two", "control", me, kind="control", workstream=None)
        rows = [l for l in self.render("--no-color").splitlines() if l.startswith("  control ")]
        self.assertEqual(len(rows), 2); self.assertFalse(any("[duplicate]" in l for l in rows))   # a second control is listed, never flagged

    def test_control_row_reads_like_a_session_row(self):
        # A control stuck on a permission is red, not a yellow "waiting"; its reply is unread, then read once opened.
        me = self.me(); now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        ctl_row = lambda *f: self.tree(40, *f).splitlines()[2]
        for kw, label in (({"state": "waiting", "waiting_for": "permission"}, "permission"), ({"state": "waiting", "waiting_for": "replied", "last_turn_at": now}, "unread"),
                          ({"state": "busy"}, "working"), ({"state": "idle"}, "no turn")):
            self.put("c-live", "control", me, kind="control", workstream=None, **kw)
            self.assertTrue(ctl_row().startswith("  ◆ control") and ctl_row().endswith(label), (kw, ctl_row()))
        self.put("c-live", "control", me, kind="control", workstream=None, state="waiting", waiting_for="permission")
        self.assertIn("\x1b[31mpermission", ctl_row("--color"))
        self.put("c-live", "control", me, kind="control", workstream=None, state="waiting", waiting_for="replied", last_turn_at=now)
        (self.board / "open.json").write_text(json.dumps({"open": "control", "seen": {}}))
        self.assertTrue(ctl_row().endswith("read"))

    def test_render_flags_two_live_active_records_as_duplicate(self):
        me = self.me(); self.write_charter("A"); self.write_charter("B"); self.write_charter("C")
        self.put("a1", "A", me); self.put("a2", "A", me)
        self.put("b1", "B", me); self.put("b2", "B", me, kind="worker", name="B work: x")
        self.put("c1", "C", me); self.put("c2", "C", None)   # the second one is dead
        rows = {l.split()[0] + "/" + l.split()[1]: l for l in self.render("--no-color").splitlines() if l.startswith("  ") and not l.startswith("   ")}
        self.assertIn("[duplicate]", rows["A/a1"]); self.assertIn("[duplicate]", rows["A/a2"])
        for k in ("B/b1", "B/b2", "C/c1"): self.assertNotIn("[duplicate]", rows[k])

    def test_render_survives_any_listing_failure(self):
        me = self.me(); self.write_charter(); self.put("s9", "PAYMENTS_API", me)
        for stdout, code in (("not json", 0), ("", 1), ('{"a": 1}', 0), ('[1, null, {"sessionId": 5}]', 0)):
            self.listing(stdout, code)
            self.assertEqual(self.group_of(self.render("--no-color"), "PAYMENTS_API"), "Active", stdout)
        (self.bin / "claude").unlink(); self.env["PATH"] = str(self.bin)   # no claude at all
        self.assertEqual(self.group_of(self.render("--no-color"), "PAYMENTS_API"), "Active")

    def test_listing_blocked_counts_only_for_a_live_record(self):
        me = self.me(); self.write_charter("LIVE"); self.write_charter("DEAD", created=time.strftime("%Y-%m-%d", time.gmtime()))
        self.put("live-1", "LIVE", me); self.put("dead-1", "DEAD", None)
        # A junk row costs only itself: the rows beside it still join.
        self.listing(json.dumps([1, None, {"sessionId": "live-1", "state": "blocked", "waitingFor": "approve push"},
                                 {"sessionId": "dead-1", "state": "blocked"}]))
        out = self.render("--no-color")
        self.assertEqual(self.group_of(out, "LIVE"), "Active"); self.assertIn("waiting · approve push", out)
        self.assertEqual(self.group_of(out, "DEAD"), "Idle")

    def test_done_charter_stays_while_its_session_is_live(self):
        # done + live session: its own group, labelled done, no focus and no likely: hint. done + nothing live: off the board.
        # archived + live session: Unassigned, as any session without a charter. A block left on any of them changes none of this.
        me = self.me(); ws = Path(self.home) / ".claude" / "projects" / "-Users-x-dev-myproject" / "workstreams"
        for k, st in (("FIN", "done"), ("GONE", "done"), ("ARCH", "archived")):
            self.write_charter(k, extra="blocked: PR #12\n")
            (ws / f"{k}.md").write_text((ws / f"{k}.md").read_text().replace("status: active", f"status: {st}"))
        self.put("s-fin", "FIN", me, state="waiting", waiting_for="replied")
        self.put("s-arch", "ARCH", me)
        out = self.render("--no-color")
        self.assertEqual(self.group_of(out, "FIN"), "Done"); self.assertEqual(self.group_of(out, "ARCH"), "Unassigned")
        self.assertIsNone(self.group_of(out, "GONE")); self.assertNotIn("GONE", self.tree(40))
        fin = next(l for l in out.splitlines() if l.startswith("  FIN "))
        self.assertTrue(fin.endswith("  done"), fin); self.assertNotIn("likely:", out)
        row = next(l for l in self.tree(40).splitlines() if " FIN " in l)
        self.assertTrue(row.startswith("  ● FIN") and row.endswith("done"), row)

    def finished(self, key, **kw):
        """A charter for `key` whose status is done, blocked on PR #12, with a live session built from `kw`."""
        self.write_charter(key, extra="blocked: PR #12\n"); ws = Path(self.home) / ".claude" / "projects" / "-Users-x-dev-myproject" / "workstreams"
        (ws / f"{key}.md").write_text((ws / f"{key}.md").read_text().replace("status: active", "status: done"))
        self.live(f"s-{key.lower()}-full", key, self.pid(), **kw)

    def test_a_done_charters_session_is_in_done_unless_it_works_or_asks(self):
        # Working or asking Ian keeps it in Active; a reply, unread or read, a settled or a cold session goes to Done. A block changes none of it.
        sub = {"agent_id": "a1", "name": "Explore", "state": "running"}
        cases = {"IDLE": ({}, "Done", "○", None), "UNREAD": (dict(state="waiting", waiting_for="replied", last_turn_at=self.ago(0.5)), "Done", "●", "unread"),
                 "READ": (dict(state="waiting", waiting_for="replied", last_turn_at=self.ago(0.5)), "Done", "○", None),
                 "COLD": (dict(last_turn_at=self.ago(40)), "Done", "○", None),
                 "WORK": (dict(status="busy", state="busy", last_turn_at=self.ago(0.1)), "Active", "▶", None),
                 "KID": (dict(state="waiting", waiting_for="replied", last_turn_at=self.ago(0.5), children=[sub]), "Active", "▶", None),
                 "PERM": (dict(state="waiting", waiting_for="permission", waiting_since=self.ago(0.2)), "Active", "●", "asks"),
                 "ASK": (dict(state="waiting", waiting_for="question", waiting_since=self.ago(0.1)), "Active", "●", "asks")}
        for key, (kw, *_) in cases.items(): self.finished(key, **kw)
        (self.board / "open.json").write_text(json.dumps({"seen": {"READ": self.ago(0)}}))
        wide, hits = self.render("--no-color"), self.hits()
        self.assertEqual({k: self.group_of(wide, k) for k in cases}, {k: v[1] for k, v in cases.items()})
        self.assertEqual({h["key"]: (h["glyph"], h.get("need")) for h in hits.values() if h["key"] != "control"}, {k: v[2:] for k, v in cases.items()})
        lines = self.tree(40).splitlines()
        self.assertEqual([l for l in lines if l.startswith("▾")], ["▾ Control", "▾ Active", "▾ Done"])
        for k in cases: self.assertRegex(next(l for l in lines if f" {k} " in l), r"done$")
        self.assertEqual(next(l for l in wide.splitlines() if l.startswith("  IDLE ")).split()[-1], "done")

    def test_listing_state_without_a_wait(self):
        # The record says a reply or a running turn: a listing blocked with no waitingFor, or a job file counting a background task,
        # over a settled record reads settled, never unread or working.
        me = self.me(); self.write_charter("DONE"); self.write_charter("BG")
        self.put("s-done", "DONE", me); self.put("s-bg", "BG", me, last_turn_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        self.job("s-bg", tasks=1, kinds=["local_bash"])
        self.listing(json.dumps([{"sessionId": "s-done", "state": "blocked", "waitingFor": None}, {"sessionId": "s-bg", "state": "working", "status": "idle"}]))
        out = self.render("--no-color")
        self.assertRegex(out, r"(?m)^  DONE +s-done +idle · no turn yet "); self.assertRegex(out, r"(?m)^  BG +s-bg +idle · 0\.0h ")
        self.assertNotIn("waiting", out); self.assertNotIn("working", out)

    def test_a_reply_is_unread_until_opened(self):
        # Unread: never opened, or a reply after Ian last opened the key. Read: opened since, or open in the right pane now.
        me, now, old = self.me(), time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "2026-01-01T00:00:00Z"
        for k in ("NEW", "OLD", "OPEN", "NEVER"): self.write_charter(k)
        for k, t in (("NEW", now), ("OLD", old), ("OPEN", now), ("NEVER", old)):
            self.put(f"s-{k}", k, me, state="waiting", waiting_for="replied", last_turn_at=t)
        (self.board / "open.json").write_text(json.dumps({"open": "OPEN", "seen": {"OLD": "2026-02-01T00:00:00Z", "NEW": old}}))
        rows = {l.split()[1]: l.split() for l in self.tree(40).splitlines() if l.startswith("  ") and len(l.split()) > 2}
        self.assertEqual({k: (rows[k][0], rows[k][-1]) for k in ("NEW", "OLD", "OPEN", "NEVER")},   # OLD and NEVER are stale: the label still says read or unread
                         {"NEW": ("●", "unread"), "OLD": ("↻", "read"), "OPEN": ("○", "read"), "NEVER": ("↻", "unread")})
        colour = self.render("--color")
        self.assertIn("\x1b[33mwaiting · unread", colour); self.assertIn("\x1b[2;33mwaiting · read", colour)

    def test_tree_footer_shows_the_saved_5h_quota(self):
        # The quota is the tree's last line, under a separator of its own; nothing without the file; dim once it has reset.
        f = Path(self.home) / ".claude" / "rate-limits.json"
        self.assertTrue(self.tree(40).endswith("stale (0)"))
        soon = int(time.time()) + 3600; at = time.strftime("%-I:%M%p", time.localtime(soon)).lower()
        f.write_text(json.dumps({"five_hour": {"used_percentage": 72.4, "resets_at": soon}}))
        lines = self.tree(40).splitlines()
        self.assertEqual(lines[-3:-1], ["stale (0)", "─" * 40])
        self.assertEqual(lines[-1], "5h ███████░░░ 72%" + " " * (40 - 17 - len(f"resets {at}")) + f"resets {at}")
        self.assertTrue(self.tree(40, "--color").endswith(f"\x1b[33m{lines[-1]}\x1b[0m"))   # yellow from 70%
        f.write_text(json.dumps({"five_hour": {"used_percentage": 95, "resets_at": int(time.time()) - 60}}))
        self.assertIn("\x1b[2m5h █████████░ 95%", self.tree(40, "--color"))   # reset already: dim, not red
        f.write_text("not json"); self.assertTrue(self.tree(40).endswith("stale (0)"))

    def test_render_order_is_fixed(self):
        me = self.me()
        for k in ("ZED", "ALF", "MID", "IDB", "IDA"): self.write_charter(k)
        self.put("s1", "ZED", me, state="waiting", waiting_for="question", waiting_since="2026-01-01T00:00:00Z")   # an older wait does not lead
        self.put("s2", "ALF", me, state="waiting", waiting_for="question", waiting_since="2026-01-02T00:00:00Z")
        self.put("s3", "MID", me, state="waiting", waiting_for="question", waiting_since="2026-01-02T00:00:00Z")
        self.put("s4", "IDB", me); self.put("s5", "IDA", me)
        out = self.render("--no-color")
        keys = [l.split()[0] for l in out.splitlines() if l.startswith("  ") and not l.startswith("   ")]
        self.assertEqual(keys, ["ALF", "IDA", "IDB", "MID", "ZED"])   # alphabetical: a wait never moves a row
        self.assertEqual(out, self.render("--no-color"))

    def test_waits_are_coloured_not_reordered_and_a_running_subagent_is_working(self):
        me = self.me()
        for k in ("TURN", "PERM", "SUB"): self.write_charter(k)
        self.put("s1", "TURN", me, state="waiting", waiting_for="replied", waiting_since="2026-01-01T00:00:00Z")
        self.put("s2", "PERM", me, state="waiting", waiting_for="permission", waiting_since="2026-01-02T00:00:00Z")
        self.put("s3", "SUB", me, state="waiting", waiting_for="replied", children=[{"agent_id": "a1", "name": "general-purpose", "state": "running"}])
        out = self.render("--no-color")
        self.assertEqual([l.split()[0] for l in out.splitlines() if l.startswith("  ") and not l.startswith("   ")], ["PERM", "SUB", "TURN"])
        self.assertEqual(self.group_of(out, "SUB"), "Active")
        colour = self.render("--color")
        self.assertIn("\x1b[31mwaiting · permission", colour); self.assertIn("\x1b[33mwaiting · unread", colour)

    def test_unassigned_session_in_a_charter_worktree_is_hinted(self):
        me = self.me(); self.write_charter("FEAT", extra='refs:\n  worktrees: [".worktrees/api/feat/base"]\n')
        self.put("s-in", "edb485cd", me, cwd="/Users/x/dev/myproject/.worktrees/api/feat/base/src")
        self.put("s-root", "f91305ce", me, cwd="/Users/x/dev/myproject")   # root session: no hint
        self.put("s-near", "near", me, cwd="/Users/x/dev/myproject/.worktrees/api/feat/base-2")   # prefix, not the path
        out = self.render("--no-color")
        self.assertEqual(out.count("likely: FEAT"), 1)
        self.assertIn("edb485cd", out.split("likely: FEAT")[0].splitlines()[-2])   # the hint sits under its own session's row
        lines = self.tree().splitlines()
        self.assertRegex(lines[lines.index(next(l for l in lines if "edb485cd" in l)) + 1].rstrip(), r"^  │ → FEAT\?$")
        self.assertEqual(sum("→" in l for l in lines), 1)

    def test_colour_only_when_asked(self):
        me = self.me(); self.write_charter(); self.put("s9", "PAYMENTS_API", me, state="waiting", waiting_for="question")
        self.assertNotIn("\x1b", self.render("--no-color"))
        self.assertNotIn("\x1b", self.render())   # stdout is a pipe here, so auto-detect says no
        self.assertIn("\x1b[", self.render("--color"))

    # --- tree: the same board, narrow, for a tmux sidebar ---

    def tree(self, width=36, *flags):
        return self.render("--tree", "--width", str(width), *(flags or ("--no-color",))).rstrip("\n")

    def board_of_every_kind(self):
        me, hour_ago = self.me(), time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600))
        for k in ("WAIT", "BUSY", "IDLE", "DORM", "OLD", "A_VERY_LONG_WORKSTREAM_KEY_INDEED"): self.write_charter(k)
        self.put("c-live", "control", me, kind="control", workstream=None)
        self.put("s-wait", "WAIT", me, state="waiting", waiting_for="permission", waiting_since=hour_ago)
        self.put("s-busy", "BUSY", me, state="busy", children=[{"agent_id": "a1", "name": "an-extremely-long-subagent-type", "state": "running"}])
        self.put("s-idle", "IDLE", me, last_turn_at=hour_ago)
        self.put("s-long", "A_VERY_LONG_WORKSTREAM_KEY_INDEED", me)
        self.put("s-dorm", "DORM", None, last_turn_at=hour_ago)
        self.put("s-loose", "loose", me)
        self.listing(json.dumps([{"sessionId": "s-long", "state": "blocked", "waitingFor": "approve the push to origin master please"}]))

    def test_tree_places_groups_and_rows_in_order(self):
        self.board_of_every_kind()
        lines = self.tree().splitlines()
        self.assertEqual(lines[0], "WORKSTREAMS")
        self.assertEqual([l for l in lines if l.startswith("▾")], ["▾ Control", "▾ Active", "▾ Idle", "▾ Unassigned"])
        rows = [l.split()[:2] for l in lines if l.startswith("  ") and l[2] in "◆●▶○◌"]
        self.assertEqual([r[1][:4] for r in rows], ["cont", "A_VE", "BUSY", "IDLE", "WAIT", "DORM", "loos"])
        self.assertEqual([r[0] for r in rows], ["◆", "●", "▶", "○", "●", "◌", "○"])
        self.assertRegex(lines[lines.index("▾ Active") + 1], r"^  ● A_VE\S* +approve[^\n]*$")

    def test_tree_children_connectors(self):
        me = self.me(); self.write_charter("A"); self.write_charter("B")
        sub = lambda n, st="done": {"agent_id": n, "name": n, "state": st}
        self.put("a1", "A", me, children=[sub("one"), sub("two")])
        self.put("b1", "B", me, children=[sub("three", "paused")])   # not running, so B reads idle
        self.assertEqual([l.rstrip() for l in self.tree().splitlines()[3:8]],
                         ["▾ Active", "  ○ A" + " " * 24 + "no turn", "  │ ├ one" + " " * 23 + "done",
                          "  │ └ two" + " " * 23 + "done", "  ○ B" + " " * 24 + "no turn"])
        self.assertRegex(self.tree().splitlines()[8], r"^    └ three +paused$")   # last session: no trunk

    def test_tree_lines_fit_the_width(self):
        self.board_of_every_kind()
        for width in (30, 36):
            for flag in ("--no-color", "--color"):
                out = self.tree(width, flag)
                if flag == "--color": self.assertIn("\x1b[", out)
                for line in out.splitlines():
                    self.assertLessEqual(len(re.sub(r"\x1b\[[0-9;]*m", "", line)), width, f"{width} {flag}: {line!r}")

    def test_tree_truncates_with_an_ellipsis(self):
        self.board_of_every_kind()
        out = self.tree(30)
        self.assertNotIn("A_VERY_LONG_WORKSTREAM_KEY_INDEED", out)
        self.assertRegex(out, r"\n  ● A_VERY_\w*… +approve[^\n]*…\n")
        self.assertRegex(out, r"\n  │ └ an-extremely-\S*… +running\n")

    def test_tree_and_wide_show_the_same_sessions(self):
        self.board_of_every_kind()
        wide = [l.split()[0] for l in self.render("--no-color").splitlines() if l.startswith("  ") and not l.startswith("   ")]
        narrow = [l.split()[1] for l in self.tree(80).splitlines() if l.startswith("  ") and l[2] in "◆●▶○◌"]
        self.assertEqual(sorted(wide), sorted(narrow))

    def test_tree_stale_line(self):
        for k in ("OLD1", "OLD2"): self.write_charter(k)
        self.assertTrue(self.tree().endswith("\nstale (2): OLD1, OLD2"))
        for k in ("OLD3", "OLD4", "OLD5", "OLD6"): self.write_charter(k)
        self.assertTrue(self.tree(30).endswith("\nstale (6)"))   # the keys would overflow, so the count stands alone
        self.assertEqual(self.tree(30).splitlines()[-2], "─" * 30)

    # --- click-to-open: the watch's line map and `board.py open` ---

    def test_watch_reloads_when_its_code_changes(self):
        # A sidebar started before a change must draw with the new code, not misread the new code's files.
        copy = Path(self.home) / "board.py"; copy.write_text(HOOK.read_text())
        out = Path(self.home) / "watch.out"
        with out.open("w") as f:
            p = subprocess.Popen([sys.executable, str(copy), "render", "--watch", "--tree", "--no-color"], stdout=f, env=self.env)
        try:
            time.sleep(1); copy.write_text(HOOK.read_text().replace('C("1", "WORKSTREAMS")', 'C("1", "RELOADED")', 1))
            for _ in range(80):
                if "RELOADED" in out.read_text(): break
                time.sleep(0.1)
        finally: p.kill(); p.wait()
        self.assertIn("RELOADED", out.read_text())

    def test_watch_tree_maps_session_and_dormant_lines(self):
        self.board_of_every_kind()
        self.put("s-guess-full-session-id", "8ab3", self.me(), cwd="/w/.worktrees/k/g", children=[{"agent_id": "a9", "name": "sub", "state": "running"}])
        self.write_charter("G", extra='refs:\n  worktrees: [".worktrees/k/g"]\n')
        rows = self.board / "tree-rows.json"
        p = subprocess.Popen([sys.executable, str(HOOK), "render", "--watch", "--tree", "--width", "36", "--no-color"], stdout=subprocess.DEVNULL, env=self.env)
        try:
            for _ in range(50):
                if rows.exists(): break
                time.sleep(0.1)
            hits = json.loads(rows.read_text())
        finally: p.kill(); p.wait()
        lines = self.tree().splitlines()   # the watch prints these same lines from the top of the pane, line 0 first
        self.assertEqual(sorted(map(int, hits)), [i for i, l in enumerate(lines) if l.startswith("  ") and l[2] in "◆●▶○◌"])
        by_key = {h["key"]: h["session_id"] for h in hits.values()}
        self.assertEqual(by_key, {"control": "c-live", "WAIT": "s-wait", "A_VERY_LONG_WORKSTREAM_KEY_INDEED": "s-long", "BUSY": "s-busy",
                                  "IDLE": "s-idle", "DORM": None, "8ab3": "s-guess-full-session-id", "loose": "s-loose"})
        self.assertEqual(self.group_of(self.render("--no-color"), "WAIT"), "Active")   # the map file is not read as a session record

    def module(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("board", HOOK); mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
        return mod

    def decide(self):
        """decide(), in-process for the rest of the test with the fake HOME, where it reads a session's job file."""
        from unittest import mock
        p = mock.patch.dict(os.environ, self.env); p.start(); self.addCleanup(p.stop)
        return self.module().decide

    def test_decide(self):
        decide = self.decide()
        bg = {"sessionId": "35fd6ab1-1e2e", "kind": "bg", "jobId": "35fd6ab1"}
        self.assertIsNone(decide(None, None))
        self.assertEqual(decide({"key": "DORM", "session_id": None}, None), ("dispatch", "DORM"))
        self.assertEqual(decide({"key": "K", "session_id": "35fd6ab1-1e2e"}, bg), ("attach", "35fd6ab1"))
        for reg in (None, bg | {"sessionId": "other"}):   # ended, or its pid now names another session
            self.assertEqual(decide({"key": "K", "session_id": "35fd6ab1-1e2e"}, reg)[0], "message")
        self.assertIn("can't attach", decide({"key": "K", "session_id": "35fd6ab1-1e2e"}, bg | {"kind": "interactive"})[1])

    def open_line(self, line, claude_out="", x=None, listing="[]"):
        """Runs `board.py open` against a fake claude and a tmux that logs its argv; returns the tmux calls. `listing` is what `claude agents` prints."""
        return self.board_py("open", str(line), "%9", *([str(x)] if x is not None else []), claude_out=claude_out, listing=listing)

    def board_py(self, *argv, claude_out="", listing="[]", panes=""):
        """open_line() for any `board.py <argv…>`; its tmux also prints `panes`, the answer list-panes gets."""
        log = Path(self.home) / "tmux.log"; log.unlink(missing_ok=True); (Path(self.home) / "claude.log").unlink(missing_ok=True)
        (self.bin / "tmux").write_text(f"#!/bin/sh\nprintf '%s|' \"$@\" >> {log}\necho >> {log}\ncat <<'EOF'\n{panes}\nEOF\nexit 0\n")
        (self.bin / "tmux").chmod(0o755)
        (Path(self.home) / "listing.json").write_text(listing)
        (self.bin / "claude").write_text(f"#!/bin/sh\necho \"$@\" > {self.home}/claude.args\necho \"$@\" >> {self.home}/claude.log\n"
                                         f"[ \"$1\" = agents ] && exec cat {self.home}/listing.json\nprintf '{claude_out}'\n"); (self.bin / "claude").chmod(0o755)
        p = subprocess.run([sys.executable, str(HOOK), *argv], capture_output=True, text=True, env=self.env)
        self.assertEqual(p.returncode, 0, p.stderr)
        return log.read_text().splitlines() if log.exists() else []

    def test_open_attaches_dispatches_or_says_why(self):
        me = self.me()
        (Path(self.home) / ".claude" / "sessions" / f"{me}.json").write_text(json.dumps({"pid": me, "sessionId": "s-bg-full", "kind": "bg", "jobId": "s-bg-fu"}))
        self.put("s-bg-full", "LIVE", me)
        self.board.mkdir(parents=True, exist_ok=True)
        (self.board / "tree-rows.json").write_text(json.dumps({"2": {"key": "LIVE", "session_id": "s-bg-full"}, "4": {"key": "DORM", "session_id": None}}))
        self.assertEqual(self.open_line(2), ["respawn-pane|-k|-t|%9|claude attach s-bg-fu|", "set|-p|-t|%9|@ws_name|LIVE|"])
        self.assertEqual(self.open_line(3), [])   # unmapped: nothing
        # The real dispatch output, colour codes and all.
        out = r"backgrounded · \033[36m35fd6ab1\033[39m · DORM\n\033[2m  claude attach 35fd6ab1    open in this terminal\033[22m\n"
        self.assertEqual(json.loads((self.board / "open.json").read_text())["open"], "LIVE")
        self.assertEqual(self.open_line(4, out), ["respawn-pane|-k|-t|%9|claude attach 35fd6ab1|", "set|-p|-t|%9|@ws_name|DORM|"])
        opened = json.loads((self.board / "open.json").read_text())   # switching marks the one left as seen too
        self.assertEqual((opened["open"], sorted(opened["seen"])), ("DORM", ["DORM", "LIVE"]))
        args = (Path(self.home) / "claude.args").read_text()
        self.assertTrue(args.startswith("--bg -n DORM --permission-mode auto Start by restating this workstream")); self.assertNotIn("--model", args); self.assertNotIn("--effort", args)
        self.assertIn("the goals still open, and any block it is waiting on and whether it still holds.", args)
        self.later(); self.assertEqual(self.open_line(4, "Error: not logged in"), ["display-message|DORM: Error: not logged in|"])   # never a guessed id

    # --- the open row: the sidebar marks what the right pane shows ---

    def panes(self, *lines, code=0):
        """The sidebar as pane %1, beside a fake tmux that answers list-panes with `lines`; returns the log of its argv."""
        log = Path(self.home) / "panes.log"; log.unlink(missing_ok=True); body = "\n".join(lines)
        (self.bin / "tmux").write_text(f"#!/bin/sh\nprintf '%s|' \"$@\" >> {log}\necho >> {log}\ncat <<'EOF'\n{body}\nEOF\nexit {code}\n"); (self.bin / "tmux").chmod(0o755)
        self.env["TMUX_PANE"] = "%1"
        return log

    def opened(self, key):
        self.board_of_every_kind(); (self.board / "open.json").write_text(json.dumps({"open": key, "seen": {}}))
        return self.tree(40)   # the board with nothing marked: no TMUX_PANE yet

    def test_tree_marks_the_row_open_in_the_right_pane(self):
        # Column 0 of the session row only: its hint and child lines, and every other line, stay as they were.
        self.write_charter("G", extra='refs:\n  worktrees: [".worktrees/k/g"]\n')
        self.put("s-guess", "8ab3", self.me(), cwd="/w/.worktrees/k/g", children=[{"agent_id": "a9", "name": "sub", "state": "running"}])
        plain = self.opened("8ab3"); log = self.panes("%1\t", "%2\t8ab3")
        self.assertIn("\n  ▶ 8ab3 ", plain); self.assertEqual(self.tree(40), plain.replace("\n  ▶ 8ab3 ", "\n▌ ▶ 8ab3 "))
        self.assertEqual(log.read_text(), "list-panes|-t|%1|-F|#{pane_id}\t#{@ws_name}|\n")   # one call, for the sidebar's own window
        self.put("s-guess2", "8ab3", self.me(), state="busy")   # a [duplicate] key: the title cannot say which, so both
        self.assertEqual([l[:9] for l in self.tree(40).splitlines() if "▌" in l], ["▌ ▶ 8ab3!", "▌ ▶ 8ab3!"])

    def test_tree_marks_nothing_unless_open_json_and_the_title_agree(self):
        plain = self.opened("BUSY")
        for lines in (("%1\t", "%2\tIDLE"),                 # the pane shows another key
                      ("%1\tBUSY", "%2\tagent view")):      # cc's rebuilt pane; the sidebar's own line never counts
            self.panes(*lines); out = self.tree(40); self.assertNotIn("▌", out, lines); self.assertEqual(out, plain, lines)
        self.panes("%1\t", "%2\tBUSY"); del self.env["TMUX_PANE"]   # outside tmux: no pane to ask about
        out = self.tree(40); self.assertNotIn("▌", out); self.assertEqual(out, plain)

    def test_a_failing_tmux_costs_only_the_mark(self):
        plain = self.opened("BUSY")
        self.panes("%1\t", "%2\tBUSY", code=1)   # a match printed, but a non-zero exit
        out = self.tree(40); self.assertNotIn("▌", out); self.assertEqual(out, plain)
        (self.bin / "tmux").unlink(); self.env["PATH"] = str(self.bin)   # no tmux at all
        out = self.tree(40); self.assertIn("\n  ▶ BUSY ", out); self.assertNotIn("▌", out)

    def test_tree_marks_an_open_control_row(self):
        self.opened("control"); self.panes("%1\t", "%2\tcontrol")
        lines = self.tree(40).splitlines()
        self.assertTrue(lines[2].startswith("▌ ◆ control "), lines[2]); self.assertEqual(sum("▌" in l for l in lines), 1)

    def test_a_marked_row_fits_the_width_in_colour(self):
        self.opened("A_VERY_LONG_WORKSTREAM_KEY_INDEED"); self.panes("%1\t", "%2\tA_VERY_LONG_WORKSTREAM_KEY_INDEED")
        for width in (30, 36):
            row = next(l for l in self.tree(width, "--color").splitlines() if "▌" in l)
            self.assertEqual(len(re.sub(r"\x1b\[[0-9;]*m", "", row)), width, row)
            self.assertTrue(row.startswith("\x1b[1m▌ \x1b[0m") and "\x1b[1;36mA_VERY_" in row, row)   # bold bar, bold cyan name

    def test_the_wide_board_ignores_the_open_row(self):
        self.opened("BUSY"); plain = [self.render(f) for f in ("--no-color", "--color")]
        log = self.panes("%1\t", "%2\tBUSY")
        self.assertEqual([self.render(f) for f in ("--no-color", "--color")], plain); self.assertFalse(log.exists())   # nor asks tmux

    def test_the_watch_redraws_as_soon_as_a_row_is_opened(self):
        # The 3s tick is too slow for the mark: a rewritten open.json wakes the watch within a poll.
        self.opened("IDLE"); self.panes("%1\t", "%2\tBUSY")
        out = Path(self.home) / "watch.out"
        with out.open("w") as f:
            p = subprocess.Popen([sys.executable, str(HOOK), "render", "--watch", "--tree", "--width", "40", "--no-color"], stdout=f, env=self.env)
        try:
            for _ in range(50):
                if "WORKSTREAMS" in out.read_text(): break
                time.sleep(0.1)
            seen, t0 = len(out.read_text()), time.time()
            (self.board / "open.json").write_text(json.dumps({"open": "BUSY", "seen": {}}))
            while time.time() - t0 < 2.5 and "▌ ▶ BUSY" not in out.read_text()[seen:]: time.sleep(0.05)
            took = time.time() - t0
        finally: p.kill(); p.wait()
        self.assertIn("▌ ▶ BUSY", out.read_text()[seen:]); self.assertLess(took, 1.5)

    # --- the Control row: always present, and what its click does ---

    def test_tree_always_has_a_mapped_control_row(self):
        self.write_charter("IDLE"); self.put("s-idle", "IDLE", self.me())
        lines, hits = self.tree().splitlines(), {}
        self.assertEqual(lines[1:3], ["▾ Control", "  ◆ control" + " " * 12 + "click to open"])
        for width in (24, 30):   # the full state fits while the pane has room for it
            self.assertLessEqual(len(self.tree(width).splitlines()[2]), width)
        self.assertTrue(self.tree(30).splitlines()[2].endswith("click to open"))
        from unittest import mock
        with mock.patch.dict(os.environ, self.env):   # in-process: the fake HOME and the fake claude
            mod = self.module(); mod.tree(mod.load_records(), width=36, hits=hits)
        self.assertEqual(hits, {2: {"key": "control", "session_id": None, "glyph": "◆"}, 4: {"key": "IDLE", "session_id": "s-idle", "glyph": "○"}})
        self.assertNotIn("control", self.render("--no-color"))   # the wide board lists only a live control

    def test_decide_control(self):
        decide, C = self.decide(), {"key": "control", "session_id": "c-full-id"}
        now, cold = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "2020-01-01T00:00:00Z"
        reg = {"sessionId": "c-full-id", "kind": "bg", "jobId": "c-full-i", "status": "idle"}; bare = {k: v for k, v in reg.items() if k != "status"}
        rec = lambda **kw: {"state": "idle", "waiting_for": None, "last_turn_at": now} | kw
        self.assertEqual(decide({"key": "control", "session_id": None}, None), ("dispatch", "control"))
        self.assertEqual(decide(C, None, rec()), ("dispatch", "control"))                       # ended since the redraw
        self.assertEqual(decide(C, reg | {"sessionId": "other"}, rec()), ("dispatch", "control"))
        self.assertEqual(decide(C, reg | {"kind": "interactive"}, rec())[0], "message")
        self.assertEqual(decide(C, reg, rec(last_turn_at=cold)), ("restart", "c-full-i"))
        self.assertEqual(decide(C, reg, rec(state="waiting", waiting_for="replied", last_turn_at=cold)), ("restart", "c-full-i"))
        # Any warm control, and one mid-turn or with a sub-agent running however old its turn, is attached as it stands.
        sub = {"agent_id": "a1", "name": "Explore", "state": "running"}
        for r, g in ((rec(), reg), (rec(last_turn_at=None), reg), (rec(state="waiting", waiting_for="replied"), reg),
                     (rec(state="busy"), reg), (rec(state="busy", last_turn_at=cold), reg), (rec(state="busy", last_turn_at=cold), bare), (rec(), bare),
                     (rec(last_turn_at=cold, children=[sub]), reg),
                     *((rec(state="waiting", waiting_for=w), reg) for w in ("permission", "question", "approve push", None))):
            self.assertEqual(decide(C, g, r), ("attach", "c-full-i"), (r, g))
        # The record says whether a turn runs: the registry's status and the job file hold nothing.
        for g in (reg | {"status": "busy"}, reg | {"status": "waiting"}): self.assertEqual(decide(C, g, rec(last_turn_at=cold)), ("restart", "c-full-i"), g)
        self.job("c-full-id", queued=1); self.assertEqual(decide(C, reg, rec(last_turn_at=cold)), ("restart", "c-full-i"))

    def control_live(self, **kw):
        """A live background control at tree line 2, in the given record state."""
        me = self.me()
        (Path(self.home) / ".claude" / "sessions" / f"{me}.json").write_text(json.dumps({"pid": me, "sessionId": "c-full-id", "kind": "bg", "jobId": "c-full-i", "status": "idle"}))
        self.put("c-full-id", "control", me, kind="control", workstream=None, **{"last_turn_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())} | kw)
        (self.board / "tree-rows.json").write_text(json.dumps({"2": {"key": "control", "session_id": "c-full-id"}}))

    BG = r"backgrounded · \033[36m9d9d9d9d\033[39m · control\n"
    ATTACH, TITLE = "respawn-pane|-k|-t|%9|claude attach ", "set|-p|-t|%9|@ws_name|control|"

    def test_open_control_dispatches_with_its_own_flags(self):
        self.board.mkdir(parents=True, exist_ok=True)
        (self.board / "tree-rows.json").write_text(json.dumps({"2": {"key": "control", "session_id": None}}))
        self.assertEqual(self.open_line(2, self.BG), [self.ATTACH + "9d9d9d9d|", self.TITLE])
        self.assertEqual((Path(self.home) / "claude.log").read_text(),
                         "--bg -n control --model opus --effort high --permission-mode auto /workstreams:board\n")

    def test_open_cold_control_is_stopped_then_replaced(self):
        self.control_live(last_turn_at="2020-01-01T00:00:00Z")
        self.assertEqual(self.open_line(2, self.BG), [self.ATTACH + "9d9d9d9d|", self.TITLE])
        self.assertEqual((Path(self.home) / "claude.log").read_text().splitlines(),
                         ["stop c-full-i", "--bg -n control --model opus --effort high --permission-mode auto /workstreams:board"])

    def test_open_live_control_only_attaches(self):
        # Whatever state a warm control is in, a click opens it: no keys typed, nothing dispatched or stopped.
        for kw in ({}, {"state": "waiting", "waiting_for": "replied"}, {"state": "busy"}, {"state": "waiting", "waiting_for": "permission"}):
            self.control_live(**kw)
            self.assertEqual(self.open_line(2), [self.ATTACH + "c-full-i|", self.TITLE], kw)
            self.assertFalse((Path(self.home) / "claude.log").exists())

    # --- reopen and reset: a click opens the row's own session, and a stale row's ↻ starts a fresh one ---

    def ago(self, hours):
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - hours * 3600))

    def hits(self, width=40):
        """tree()'s line map, in-process against the fake HOME and claude, written where a click reads it: {line: entry}."""
        from unittest import mock
        hits = {}
        with mock.patch.dict(os.environ, self.env):
            mod = self.module(); mod.tree(mod.load_records(), width=width, hits=hits)
        (self.board / "tree-rows.json").write_text(json.dumps(hits))
        return hits

    def row_of(self, key):
        (line, hit), = [(n, h) for n, h in self.hits().items() if h["key"] == key]
        return line, hit

    def stale_live(self, extra="", **kw):
        """A live background session for STALE, its last turn two hours old, under a charter with `extra` frontmatter; returns its row's line and line-map entry."""
        me = self.me(); self.write_charter("STALE", extra=extra)
        (Path(self.home) / ".claude" / "sessions" / f"{me}.json").write_text(json.dumps({"pid": me, "sessionId": "s-stale-full", "kind": "bg", "jobId": "s-stale-", "status": "idle"}))
        self.put("s-stale-full", "STALE", me, last_turn_at=self.ago(2), **kw)
        return self.row_of("STALE")

    def dispatched(self, key):
        return f"--bg -n {key} --permission-mode auto {self.module().RESUME}"

    def later(self):
        """As if DISPATCH_HOLD has passed: the next click on a key is a separate click, not the second half of a double-click."""
        for f in self.board.glob(".dispatch-*"): f.unlink()

    def test_an_idle_row_reopens_its_latest_retired_session(self):
        # The latest by last turn, else by start, of the workstream's active sessions: a worker's session is its own.
        self.write_charter("DORM")
        bg = lambda sid: {"sessionId": sid, "id": sid[:8], "kind": "background", "state": "done"}
        self.put("s-older-full", "DORM", None, last_turn_at=self.ago(3)); self.put("s-newer-full", "DORM", None, last_turn_at=self.ago(2))
        self.put("s-worker-full", "DORM", None, kind="worker", name="DORM work: x", last_turn_at=self.ago(0.5))
        L = json.dumps([bg("s-older-full"), bg("s-newer-full"), bg("s-worker-full")]); self.listing(L)
        line, hit = self.row_of("DORM")
        self.assertEqual(hit["session_id"], "s-newer-full")
        self.assertTrue(self.tree(40).splitlines()[line].startswith("  ↻ DORM "))   # not live and stale: its glyph is the reset button
        self.assertEqual(self.open_line(line, listing=L), ["respawn-pane|-k|-t|%9|claude attach s-newer-|", "set|-p|-t|%9|@ws_name|DORM|"])
        self.assertEqual((Path(self.home) / "claude.log").read_text(), "agents --json --all\n")   # read the listing, dispatched nothing
        self.assertEqual(json.loads((self.board / "open.json").read_text())["open"], "DORM")
        self.put("s-started-full", "DORM", None, started_at=self.ago(0.1))   # no turn yet, so its start places it
        self.listing(json.dumps([bg("s-newer-full"), bg("s-started-full")]))
        line, hit = self.row_of("DORM")
        self.assertEqual(hit, {"key": "DORM", "session_id": "s-started-full", "glyph": "◌"})   # and with no turn, nothing stale to reset
        self.assertTrue(self.tree(40).splitlines()[line].startswith("  ◌ DORM "))   # not live, not stale: still drawn dormant

    def test_an_idle_row_with_nothing_to_reopen_dispatches(self):
        # Its latest session unlisted, or listed but not as a background session: a fresh session, as for a key that never had one.
        self.write_charter("DORM")
        self.put("s-old-full", "DORM", None, last_turn_at=self.ago(3)); self.put("s-last-full", "DORM", None, last_turn_at=self.ago(2))
        for L in ("[]", json.dumps([{"sessionId": "s-old-full", "kind": "background"}, {"sessionId": "s-last-full", "kind": "interactive"}])):
            self.listing(L); line, hit = self.row_of("DORM"); self.later()
            self.assertEqual(hit, {"key": "DORM", "session_id": None, "glyph": "◌"}, L)
            self.assertEqual(self.open_line(line, self.BG.replace("control", "DORM"), listing=L), [self.ATTACH + "9d9d9d9d|", "set|-p|-t|%9|@ws_name|DORM|"], L)
            self.assertEqual((Path(self.home) / "claude.log").read_text(), self.dispatched("DORM") + "\n", L)

    def test_a_stale_row_opens_on_a_click_and_resets_on_its_arrow(self):
        line, hit = self.stale_live()
        self.assertEqual(hit, {"key": "STALE", "session_id": "s-stale-full", "glyph": "↻", "reset_x": 2})
        for x in (None, 1, 4, 39):   # a binding with no column; the lead, the name and the row's far end
            self.assertEqual(self.open_line(line, x=x), ["respawn-pane|-k|-t|%9|claude attach s-stale-|", "set|-p|-t|%9|@ws_name|STALE|"], x)
            self.assertFalse((Path(self.home) / "claude.log").exists(), x)
        for x in (2, 3):   # the ↻ and the space after it: stop, then dispatch, then attach the id the dispatch printed
            self.later(); self.assertEqual(self.open_line(line, self.BG.replace("control", "STALE"), x=x), [self.ATTACH + "9d9d9d9d|", "set|-p|-t|%9|@ws_name|STALE|"], x)
            self.assertEqual((Path(self.home) / "claude.log").read_text().splitlines(), ["stop s-stale-", self.dispatched("STALE")], x)
            self.assertEqual(json.loads((self.board / "open.json").read_text())["open"], "STALE")

    def test_the_arrow_on_a_retired_row_starts_fresh(self):
        self.write_charter("DORM"); self.put("s-dorm-full", "DORM", None, last_turn_at=self.ago(2))
        L = json.dumps([{"sessionId": "s-dorm-full", "id": "s-dorm-f", "kind": "background", "state": "blocked"}]); self.listing(L)
        line, hit = self.row_of("DORM")
        self.assertEqual(hit, {"key": "DORM", "session_id": "s-dorm-full", "glyph": "↻", "reset_x": 2})
        self.assertEqual(self.open_line(line, self.BG.replace("control", "DORM"), x=2, listing=L), [self.ATTACH + "9d9d9d9d|", "set|-p|-t|%9|@ws_name|DORM|"])
        self.assertEqual((Path(self.home) / "claude.log").read_text(), self.dispatched("DORM") + "\n")   # nothing to stop, and no listing read

    def test_a_double_click_starts_one_session(self):
        # The second click of a double-click lands after `claude --bg` has returned: it must not start a second session.
        self.write_charter("DORM"); self.put("s-dorm-full", "DORM", None, last_turn_at=self.ago(2))
        L = json.dumps([{"sessionId": "s-dorm-full", "id": "s-dorm-f", "kind": "background", "state": "blocked"}]); self.listing(L)
        line, _ = self.row_of("DORM"); bg, started = self.BG.replace("control", "DORM"), [self.ATTACH + "9d9d9d9d|", "set|-p|-t|%9|@ws_name|DORM|"]
        self.assertEqual(self.open_line(line, bg, x=2, listing=L), started)
        self.assertEqual(self.open_line(line, bg, x=2, listing=L), ["display-message|DORM: already starting|"])
        self.assertFalse((Path(self.home) / "claude.log").exists())   # nothing dispatched
        self.assertEqual(self.open_line(line, listing=L)[0], "respawn-pane|-k|-t|%9|claude attach s-dorm-f|")   # a plain click still opens
        # A failed dispatch frees the key at once; a hold older than DISPATCH_HOLD, from a dispatch that died holding it, is taken over.
        self.later(); self.assertEqual(self.open_line(line, "Error: not logged in", x=2, listing=L), ["display-message|DORM: Error: not logged in|"])
        self.assertEqual(self.open_line(line, bg, x=2, listing=L), started)
        old = time.time() - 60; os.utime(self.board / ".dispatch-DORM", (old, old))
        self.assertEqual(self.open_line(line, bg, x=2, listing=L), started)
        # A live stale row's reset stops its session once, not once per click.
        sline, _ = self.stale_live(); sbg = self.BG.replace("control", "STALE")
        self.open_line(sline, sbg, x=2); self.assertEqual((Path(self.home) / "claude.log").read_text().splitlines(), ["stop s-stale-", self.dispatched("STALE")])
        self.assertEqual(self.open_line(sline, sbg, x=2), ["display-message|STALE: already starting|"]); self.assertFalse((Path(self.home) / "claude.log").exists())

    def test_a_reset_never_stops_a_session_that_turned_busy_or_asks(self):
        # The line map still carries the ↻ drawn before the session changed: the click re-checks, and only opens it.
        line, _ = self.stale_live(); me = os.getpid(); reg = Path(self.home) / ".claude" / "sessions" / f"{me}.json"
        attach = ["respawn-pane|-k|-t|%9|claude attach s-stale-|", "set|-p|-t|%9|@ws_name|STALE|"]
        # A prompt just sent, or a dialog up.
        for kw in ({"state": "busy", "last_turn_at": self.ago(0)}, {"state": "waiting", "waiting_for": "permission"}, {"state": "waiting", "waiting_for": "question"}):
            self.put("s-stale-full", "STALE", me, **{"last_turn_at": self.ago(2)} | kw)
            self.assertEqual(self.open_line(line, x=2), attach, kw); self.assertFalse((Path(self.home) / "claude.log").exists(), kw)
        # A turn that has run for two hours, whatever the registry says, or a sub-agent running: drawn working, and the old arrow only opens it.
        for status, kw in (("idle", {"state": "busy"}), ("busy", {"state": "busy"}), ("idle", {"children": [{"agent_id": "a1", "name": "Explore", "state": "running"}]})):
            self.put("s-stale-full", "STALE", me, last_turn_at=self.ago(2), **kw)
            reg.write_text(json.dumps(json.loads(reg.read_text()) | {"status": status}))
            self.assertRegex(self.tree(40).splitlines()[line], r"^  ▶ STALE +working$", status); self.assertEqual(self.open_line(line, x=2), attach, status)
        self.assertFalse((Path(self.home) / "claude.log").exists())

    def test_an_interrupted_stale_turn_resets_on_its_arrow(self):
        # Interrupted: the mod's turn.complete, aborted, settles the record. Two hours on, its arrow stops it and starts fresh.
        self.stale_live(state="busy"); reg = Path(self.home) / ".claude" / "sessions" / f"{os.getpid()}.json"
        reg.write_text(json.dumps(json.loads(reg.read_text()) | {"name": "STALE"}))
        self.event("turn.complete", sid="s-stale-full", reason="aborted", answer="")
        r = json.loads((self.board / "s-stale-full.json").read_text()); self.put_rec(r | {"last_turn_at": self.ago(2)}, "s-stale-full")
        line, hit = self.row_of("STALE")
        self.assertEqual(hit, {"key": "STALE", "session_id": "s-stale-full", "glyph": "↻", "reset_x": 2})
        self.assertEqual(self.open_line(line, self.BG.replace("control", "STALE"), x=2), [self.ATTACH + "9d9d9d9d|", "set|-p|-t|%9|@ws_name|STALE|"])
        self.assertEqual((Path(self.home) / "claude.log").read_text().splitlines(), ["stop s-stale-", self.dispatched("STALE")])

    def test_decide_reset_and_reopen(self):
        decide, cold, now = self.decide(), "2020-01-01T00:00:00Z", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        reg, E = {"sessionId": "s-full-id", "kind": "bg", "jobId": "job-live", "status": "idle"}, {"key": "K", "session_id": "s-full-id", "reset_x": 2}
        rec = lambda **kw: {"state": "idle", "waiting_for": None, "last_turn_at": cold} | kw
        never = lambda: self.fail("a live session never reads the listing")
        for r in (rec(), rec(state="waiting", waiting_for="replied")):
            for x in (2, 3): self.assertEqual(decide(E, reg, r, x, never), ("restart", "job-live"), (r, x))
        # Mid-turn by the record, however old the turn and whatever the registry says, a sub-agent running, or a wait on Ian's answer.
        sub = {"agent_id": "a1", "name": "Explore", "state": "running"}
        for r, g in ((rec(state="busy"), {k: v for k, v in reg.items() if k != "status"}), (rec(state="busy"), reg), (rec(state="busy", last_turn_at=now), reg),
                     (rec(children=[sub]), reg), *((rec(state="waiting", waiting_for=w), reg) for w in ("permission", "question", "approve push"))):
            self.assertEqual(decide(E, g, r, 2, never), ("attach", "job-live"), (r, g))
        for g in (reg | {"status": "busy"}, reg | {"status": "waiting"}): self.assertEqual(decide(E, g, rec(), 2, never), ("restart", "job-live"), g)   # the registry holds nothing
        for x in (None, 0, 1, 4, 39): self.assertEqual(decide(E, reg, rec(), x, never), ("attach", "job-live"), x)
        self.assertEqual(decide({"key": "K", "session_id": "s-full-id"}, reg, rec(), 2, never), ("attach", "job-live"))   # no ↻ drawn on it
        self.assertEqual(decide(E, reg | {"kind": "interactive"}, rec(), 2, never)[0], "message")
        listed = lambda kind: (lambda: {"s-full-id": {"sessionId": "s-full-id", "kind": kind}})
        self.assertEqual(decide(E, None, rec(), 2, listed("background")), ("dispatch", "K"))
        self.assertEqual(decide(E, None, rec(), None, listed("background")), ("attach", "s-full-i"))
        self.assertEqual(decide(E, reg | {"sessionId": "other"}, rec(), None, listed("background")), ("attach", "s-full-i"))   # its pid reused
        for rows in (listed("interactive"), dict): self.assertEqual(decide(E, None, rec(), None, rows), ("message", "K: session has ended"))
        self.job("s-full-id", tasks=1); self.assertEqual(decide(E, reg, rec(), 2, never), ("restart", "job-live"))   # nor does the job file

    def test_only_a_stale_turn_that_is_yours_gets_the_arrow(self):
        me, two = self.me(), self.ago(2)
        for k in ("STALE", "REPLY", "RETIRED", "WORK", "KID", "PERM", "ASK", "LISTED", "FRESH", "NOTURN", "GONE"): self.write_charter(k)
        self.put("c-live", "control", me, kind="control", workstream=None, last_turn_at=two)
        self.put("s-stale", "STALE", me, last_turn_at=two); self.put("s-reply", "REPLY", me, state="waiting", waiting_for="replied", last_turn_at=two)
        self.put("s-retired", "RETIRED", None, last_turn_at=two); self.put("s-gone", "GONE", None, last_turn_at=two)
        self.put("s-work", "WORK", me, state="busy", last_turn_at=two)
        self.put("s-sub", "KID", me, state="waiting", waiting_for="replied", last_turn_at=two, children=[{"agent_id": "a1", "name": "Explore", "state": "running"}])
        self.put("s-perm", "PERM", me, state="waiting", waiting_for="permission", last_turn_at=two)
        self.put("s-ask", "ASK", me, state="waiting", waiting_for="question", last_turn_at=two)
        self.put("s-listed", "LISTED", me, last_turn_at=two); self.put("s-fresh", "FRESH", me, last_turn_at=self.ago(0.5)); self.put("s-noturn", "NOTURN", me)
        self.write_charter("WKR"); self.put("s-wkr", "WKR work: sweep", me, kind="worker", workstream="WKR", last_turn_at=two)   # no charter of its own to reset from
        self.put("s-loose", "loose", me, last_turn_at=two)                                                                    # unassigned: nothing to reset from
        self.listing(json.dumps([{"sessionId": "s-retired", "kind": "background", "state": "done"},
                                 {"sessionId": "s-listed", "kind": "background", "state": "blocked", "waitingFor": "approve push"}]))
        lines, hits = self.tree(40).splitlines(), self.hits()
        self.assertEqual({l.split()[1] for l in lines if l[2:3] == "↻"}, {"STALE", "REPLY", "RETIRED"})
        self.assertEqual(sum("↻" in l for l in lines), 3)   # the glyph only, never at the row's end as well
        for k in ("WKR", "loose"): self.assertTrue(next(l for l in lines if f" {k} " in l).endswith(" 2.0h cold"), k)
        self.assertEqual({h["key"]: h["reset_x"] for h in hits.values() if "reset_x" in h}, {"STALE": 2, "REPLY": 2, "RETIRED": 2})
        self.assertEqual({int(n) for n, h in hits.items() if "reset_x" in h}, {i for i, l in enumerate(lines) if l[2:3] == "↻"})
        self.assertTrue(next(l for l in lines if " STALE " in l).endswith(" 2.0h"))   # the ↻ says cold
        self.assertTrue(lines[2].startswith("  ◆ control") and lines[2].endswith(" 2.0h cold"), lines[2])   # control's own click restarts it
        self.assertIn("  STALE    s-stale   idle · 2.0h · cold ", self.render("--no-color"))   # the wide board is unchanged

    def test_an_arrow_row_is_exactly_the_width(self):
        me = self.me(); self.write_charter("STALE"); self.write_charter("A_VERY_LONG_WORKSTREAM_KEY_INDEED")
        self.put("s-stale", "STALE", me, last_turn_at=self.ago(2))
        self.put("s-long", "A_VERY_LONG_WORKSTREAM_KEY_INDEED", me, state="waiting", waiting_for="replied", last_turn_at=self.ago(2))
        for width in (30, 40):
            for flag in ("--no-color", "--color"):
                rows = [l for l in (re.sub(r"\x1b\[[0-9;]*m", "", l) for l in self.tree(width, flag).splitlines()) if l[2:3] == "↻"]
                self.assertEqual(len(rows), 2, (width, flag))
                for l in rows: self.assertTrue(len(l) == width and l[-1] != " ", (width, flag, l))   # the state runs to the row's end
        stale = lambda *f: next(l for l in self.tree(40, *f).splitlines() if "STALE" in l)
        self.assertEqual(stale(), "  ↻ STALE" + " " * 27 + "2.0h")
        self.assertEqual(stale("--color"), "\x1b[2m  \x1b[0m\x1b[2m↻\x1b[0m \x1b[36mSTALE\x1b[0m" + " " * 27 + "\x1b[2m2.0h\x1b[0m")
        reply = next(l for l in self.tree(40, "--color").splitlines() if "A_VERY" in l)
        self.assertTrue(reply.startswith("\x1b[2m  \x1b[0m\x1b[33m↻\x1b[0m "), reply)   # an unread stale reply keeps its yellow

    def test_a_marked_arrow_row_fits_the_width(self):
        self.write_charter("STALE"); self.put("s-stale", "STALE", self.me(), last_turn_at=self.ago(2))
        (self.board / "open.json").write_text(json.dumps({"open": "STALE", "seen": {}})); self.panes("%1\t", "%2\tSTALE")
        for width in (30, 40):
            for flag in ("--no-color", "--color"):
                row = re.sub(r"\x1b\[[0-9;]*m", "", next(l for l in self.tree(width, flag).splitlines() if "▌" in l))
                self.assertTrue(len(row) == width and row.startswith("▌ ↻ STALE") and row.endswith(" 2.0h"), (width, flag, row))

    # --- a running turn: the record's busy, which the mod sets at every turn's start and clears at its end, or a running sub-agent ---

    def test_a_turn_runs_by_the_record_or_a_child(self):
        # The registry's status, the job file and the listing say nothing about it.
        self.write_charter("INT"); sub = {"agent_id": "a1", "name": "Explore", "state": "running"}
        for status, kw, work, listing, want in (
                ("idle", {}, {}, [], ("▶", "working", "working")),
                ("idle", {}, {}, [{"sessionId": "s-int-full", "state": "blocked"}], ("▶", "working", "working")),
                (None, {}, {}, [], ("▶", "working", "working")),
                ("busy", {"state": "idle"}, {}, [], ("○", "0.5h", "idle · 0.5h")),
                ("waiting", {"state": "idle"}, {}, [], ("○", "0.5h", "idle · 0.5h")),
                ("idle", {"state": "idle"}, {"tasks": 1, "kinds": ["local_bash"]}, [{"sessionId": "s-int-full", "state": "working"}], ("○", "0.5h", "idle · 0.5h")),
                ("idle", {"state": "idle"}, {"queued": 1}, [], ("○", "0.5h", "idle · 0.5h")),
                ("idle", {"state": "idle", "children": [sub]}, {}, [], ("▶", "working", "working"))):
            self.live("s-int-full", "INT", os.getpid(), status=status, **{"state": "busy", "last_turn_at": self.ago(0.5)} | kw)
            self.job("s-int-full", **work); self.listing(json.dumps(listing))
            row = next(l for l in self.tree(40).splitlines() if " INT " in l).split()
            wide = re.search(r"(?m)^  INT +s-int-fu +(.+?)  ", self.render("--no-color")).group(1)
            self.assertEqual((row[0], row[-1], wide), want, (status, kw, work, listing))

    def test_a_reply_is_ians_once_no_sub_agent_runs(self):
        # A turn begun without a prompt (a task notification, a peer's message) sets busy through the mod's turn.start, so a reply
        # stays one whatever the registry or the job file say; only a sub-agent still running, or a wait the listing names, outranks it.
        self.write_charter("RUN"); sub = {"agent_id": "a1", "name": "Explore", "state": "running"}
        for status, kw, work, listing, want in (
                ("busy", {}, {}, [], ("●", "unread")),
                ("idle", {}, {"tasks": 1}, [], ("●", "unread")),
                ("idle", {"children": [sub]}, {}, [], ("▶", "working")),
                ("busy", {"state": "idle", "waiting_for": None}, {}, [{"sessionId": "s-run-full", "state": "blocked"}], ("○", "0.5h")),
                ("busy", {"state": "idle", "waiting_for": None}, {}, [{"sessionId": "s-run-full", "state": "blocked", "waitingFor": "approve push"}], ("●", "push")),   # a named wait stays an ask
                ("idle", {}, {}, [], ("●", "unread"))):
            self.live("s-run-full", "RUN", os.getpid(), status=status, **{"state": "waiting", "waiting_for": "replied", "last_turn_at": self.ago(0.5)} | kw)
            self.job("s-run-full", **work); self.listing(json.dumps(listing))
            row = next(l for l in self.tree(40).splitlines() if " RUN " in l).split()
            self.assertEqual((row[0], row[-1]), want, (status, kw, work, listing))
            self.assertEqual("need" in next(h for h in self.hits().values() if h["key"] == "RUN"), want[0] == "●", (status, kw, work))
        self.listing("[]"); self.event("turn.start", sid="s-run-full")   # the next turn, begun with no prompt
        self.assertTrue(next(l for l in self.tree(40).splitlines() if " RUN " in l).endswith(" working"))

    def test_an_unreadable_registry_file_leaves_the_record_to_say_and_a_click_to_explain(self):
        self.write_charter("INT"); me = os.getpid(); self.live("s-int-full", "INT", me, state="busy", last_turn_at=self.ago(0.5))
        row = lambda: next(l for l in self.tree(40).splitlines() if " INT " in l).split()[-1]
        for text in ("not json", "1"):   # the record's busy stands, and a click finds no session to attach
            (Path(self.home) / ".claude" / "sessions" / f"{me}.json").write_text(text); self.assertEqual(row(), "working", text)
            self.assertEqual(self.open_line(self.row_of("INT")[0]), ["display-message|INT: session has ended|"], text)

    # --- keyboard switching: Option+j/k step to the next row a click would attach, and Option+m lists every row ---

    def press(self, *argv, title="agent view", panes=None, listing="[]"):
        """A key binding's `board.py go|menu …`, the right pane %9 titled `title`; returns the tmux calls after the title lookup."""
        calls = self.board_py(*argv, listing=listing, panes="\n".join(panes or ("%1\t", f"%9\t{title}")))
        return [c for c in calls if not c.startswith("list-panes|")]

    def live(self, sid, key, pid, how="bg", status="idle", **kw):
        """A live session for `key`, under a pid that exists: `how` is its registry kind, bg or interactive, and `status` its status, None for none."""
        (Path(self.home) / ".claude" / "sessions" / f"{pid}.json").write_text(json.dumps({"pid": pid, "sessionId": sid, "name": kw.get("name", key), "kind": how, "jobId": sid[:8]} | ({"status": status} if status else {})))
        self.put(sid, key, pid, **kw)

    RETIRED = json.dumps([{"sessionId": "s-ret-full", "kind": "background", "state": "done"}])

    def switchable(self):
        """The sidebar, top to bottom: control with none live, ALPHA live in the background, BRAVO live in another terminal,
        CHARLIE live in the background, DORM with nothing to reopen, RET with a retired session to revive. Returns {key: line}."""
        for k in ("ALPHA", "BRAVO", "CHARLIE", "DORM", "RET"): self.write_charter(k)
        # Three live sessions need three pids that exist: this process, its parent, and pid 1, another user's, which alive() counts.
        self.live("s-alpha-full", "ALPHA", os.getpid()); self.live("s-bravo-full", "BRAVO", os.getppid(), "interactive"); self.live("s-charlie-full", "CHARLIE", 1)
        self.put("s-dorm-full", "DORM", None, last_turn_at=self.ago(2)); self.put("s-ret-full", "RET", None, last_turn_at=self.ago(2))
        self.listing(self.RETIRED)
        return {h["key"]: n for n, h in self.hits().items()}

    def attached(self, key, job):
        return [f"respawn-pane|-k|-t|%9|claude attach {job}|", f"set|-p|-t|%9|@ws_name|{key}|"]

    def open_now(self, key):
        (self.board / "open.json").unlink(missing_ok=True)
        if key: (self.board / "open.json").write_text(json.dumps({"open": key, "seen": {}}))

    def test_a_step_opens_the_next_row_a_click_would_attach(self):
        # Walked each way from ALPHA: BRAVO (another terminal's), DORM (a dispatch) and control (a dispatch) are passed over, and
        # both ends wrap. Each landing is a click's attach, RET's a revival; only RET, not live, costs a read of the listing.
        self.switchable(); self.open_now("ALPHA"); claude = Path(self.home) / "claude.log"
        for way, walk in (("next", [("CHARLIE", "s-charli"), ("RET", "s-ret-fu"), ("ALPHA", "s-alpha-")]),
                          ("prev", [("RET", "s-ret-fu"), ("CHARLIE", "s-charli"), ("ALPHA", "s-alpha-")])):
            for key, job in walk:
                here = json.loads((self.board / "open.json").read_text())["open"]
                self.assertEqual(self.press("go", way, "%9", title=here, listing=self.RETIRED), self.attached(key, job), (way, here))
                self.assertEqual(json.loads((self.board / "open.json").read_text())["open"], key)
                self.assertEqual(claude.read_text() if claude.exists() else "", "agents --json --all\n" * (key == "RET"), (way, here))

    def test_with_nothing_open_a_step_starts_from_an_end(self):
        # No open.json; one the title disagrees with (cc's rebuilt pane); one naming a key with no row.
        self.switchable()
        for opened, title in ((None, "agent view"), ("ALPHA", "agent view"), ("GONE", "GONE")):
            for way, key, job in (("next", "ALPHA", "s-alpha-"), ("prev", "RET", "s-ret-fu")):
                self.open_now(opened)
                self.assertEqual(self.press("go", way, "%9", title=title, listing=self.RETIRED), self.attached(key, job), (opened, way))

    def test_a_step_asks_the_pane_it_was_given_what_is_open(self):
        # The binding runs outside the sidebar, so only %9's title counts, whatever the other panes or $TMUX_PANE say.
        self.switchable(); self.open_now("CHARLIE"); self.env["TMUX_PANE"] = "%1"
        calls = self.press("go", "next", "%9", panes=("%1\tCHARLIE", "%2\tCHARLIE", "%9\tagent view"), listing=self.RETIRED)
        self.assertEqual(calls, self.attached("ALPHA", "s-alpha-"))   # nothing open, so the top: not RET, after CHARLIE
        self.assertEqual((Path(self.home) / "tmux.log").read_text().splitlines()[0], "list-panes|-t|%9|-F|#{pane_id}\t#{@ws_name}|")

    def test_a_step_never_starts_a_session(self):
        # Control is live but cold, so its click would stop and replace it; DORM's would dispatch. With ALPHA open there is nowhere to go.
        self.write_charter("ALPHA"); self.write_charter("DORM")
        self.live("c-full-id", "control", os.getppid(), kind="control", workstream=None, last_turn_at=self.ago(2))
        self.live("s-alpha-full", "ALPHA", os.getpid()); self.put("s-dorm-full", "DORM", None, last_turn_at=self.ago(2))
        self.assertEqual([h["key"] for _, h in sorted(self.hits().items())], ["control", "ALPHA", "DORM"])
        self.open_now("ALPHA")
        for way in ("next", "prev"):
            self.assertEqual(self.press("go", way, "%9", title="ALPHA"), ["display-message|workstreams: no other session to open|"], way)
            self.assertFalse((Path(self.home) / "claude.log").exists(), way)   # no stop, no --bg, and no listing: every row here was decided without one

    def test_a_step_reads_the_listing_at_most_once(self):
        # Rows drawn while their sessions were live, ended since: each click would read the listing, and the step reads it once for all.
        for k in ("GONE1", "GONE2", "RET"): self.put(f"s-{k.lower()}-full", k, None)
        (self.board / "tree-rows.json").write_text(json.dumps({"2": {"key": "control", "session_id": None, "glyph": "◆"},
            **{str(n): {"key": k, "session_id": f"s-{k.lower()}-full", "glyph": "○"} for n, k in ((4, "GONE1"), (5, "GONE2"), (6, "RET"))}}))
        self.assertEqual(self.press("go", "next", "%9", listing=self.RETIRED), self.attached("RET", "s-ret-fu"))
        self.assertEqual((Path(self.home) / "claude.log").read_text(), "agents --json --all\n")

    def menu_items(self, call, rows):
        """(flags, [[name, key, command], …]) from a logged display-menu call of `rows` items."""
        name, *args = call.split("|")[:-1]; at = len(args) - 3 * rows
        self.assertEqual(name, "display-menu"); return args[:at], [args[i:i + 3] for i in range(at, len(args), 3)]

    def test_the_menu_lists_every_row_and_an_item_is_its_click(self):
        lines = self.switchable(); self.open_now("CHARLIE")
        (call,) = self.press("menu", "/dev/ttys042", "%9", title="CHARLIE")   # one call, and nothing opened by it
        flags, items = self.menu_items(call, 6)
        self.assertEqual(flags, ["-M", "-c", "/dev/ttys042", "-C", "3"])   # CHARLIE's item is the starting choice
        self.assertEqual([(n, k) for n, k, _ in items], [("◆ control", "1"), ("○ ALPHA", "2"), ("○ BRAVO", "3"), ("○ CHARLIE", "4"), ("◌ DORM", "5"), ("↻ RET", "6")])
        for (_, _, cmd), line in zip(items, sorted(lines.values())):   # the sidebar's plain click on that line: no column, so never a reset
            *run, inner = shlex.split(cmd)
            self.assertEqual((run, shlex.split(inner)), (["run-shell", "-b", "-c", os.getcwd()], [sys.executable, str(HOOK), "open", str(line), "%9"]))
        self.open_now(None)
        self.assertEqual(self.menu_items(self.press("menu", "/dev/ttys042", "%9")[0], 6)[0], ["-M", "-c", "/dev/ttys042"])   # nothing open: no choice set

    def test_menu_shortcuts_skip_q_and_run_out(self):
        self.board.mkdir(parents=True, exist_ok=True)
        (self.board / "tree-rows.json").write_text(json.dumps({str(n): {"key": f"K{n}" if n < 42 else "A#B", "session_id": None, "glyph": "◌"} for n in range(2, 43)}))
        _, items = self.menu_items(self.press("menu", "/dev/ttys042", "%9")[0], 41)
        self.assertEqual([n for n, _, _ in items], [f"◌ K{n}" for n in range(2, 42)] + ["◌ A##B"])   # line order, and a # doubled for the format
        self.assertEqual([k for _, k, _ in items], list("123456789abcdefghijklmnoprstuvwxyz") + [""] * 7)

    def test_no_rows_is_no_menu_and_no_step(self):
        self.board.mkdir(parents=True, exist_ok=True)
        for rows in (None, "{}", "not json"):
            (self.board / "tree-rows.json").unlink(missing_ok=True)
            if rows: (self.board / "tree-rows.json").write_text(rows)
            self.assertEqual(self.press("menu", "/dev/ttys042", "%9"), [], rows)
            self.assertEqual(self.press("go", "next", "%9"), ["display-message|workstreams: no other session to open|"], rows)
        # A map a sidebar wrote before rows carried their glyph: the menu still opens, naming the keys alone.
        (self.board / "tree-rows.json").write_text(json.dumps({"2": {"key": "control", "session_id": None}, "4": {"key": "OLD", "session_id": None}}))
        calls = self.press("menu", "/dev/ttys042", "%9")
        self.assertEqual(len(calls), 1, calls); self.assertIn("|control|1|", calls[0]); self.assertIn("|OLD|2|", calls[0])
        self.assertEqual(self.press("go", "need", "%9"), ["display-message|workstreams: nothing needs you|"])   # rows, and none of them needs Ian

    # --- Option+n: the row that most needs Ian, asks before unread and the oldest first ---

    def pid(self):
        """A pid that exists until the test ends, for a live session of its own: a click attaches only the session its pid's registry names."""
        p = subprocess.Popen(["cat"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL)
        self.addCleanup(p.wait); self.addCleanup(p.stdin.close); return p.pid

    def needy(self):
        """A live background session per row: ZED, MID and ALF asking Ian, YAK, BOB and control with his unread replies, READ with a
        reply he has seen and IDLE waiting on nobody. Returns {key: since} for the rows that wait on him."""
        t = {h: self.ago(h) for h in (0.5, 1, 1.5, 2, 2.5, 4, 5, 6)}
        for k in ("ALF", "BOB", "IDLE", "MID", "READ", "YAK", "ZED"): self.write_charter(k)
        for key, kw in (("control", dict(kind="control", workstream=None, state="waiting", waiting_for="replied", waiting_since=t[0.5], last_turn_at=self.ago(0.6))),
                        ("ZED", dict(state="waiting", waiting_for="permission", waiting_since=t[2], last_turn_at=t[2.5])),
                        ("ALF", dict(state="waiting", waiting_for="question", waiting_since=t[1], last_turn_at=t[4])),   # the oldest turn, not the oldest wait
                        ("MID", dict(last_turn_at=t[1.5])),                                                             # listing blocked: its turn dates it
                        ("YAK", dict(state="waiting", waiting_for="replied", waiting_since=t[5], last_turn_at=t[5])),
                        ("BOB", dict(state="waiting", waiting_for="replied", last_turn_at=t[4])),                       # a reply with no since: its turn dates it
                        ("READ", dict(state="waiting", waiting_for="replied", waiting_since=t[6], last_turn_at=t[6])),
                        ("IDLE", dict(last_turn_at=t[0.5]))):
            self.live(f"s-{key.lower()}-full", key, self.pid(), **kw)
        (self.board / "open.json").write_text(json.dumps({"seen": {"READ": self.ago(0)}}))
        self.listing(json.dumps([{"sessionId": "s-mid-full", "state": "blocked", "waitingFor": "approve push"}]))
        return {"ZED": t[2], "MID": t[1.5], "ALF": t[1], "YAK": t[5], "BOB": t[4], "control": t[0.5]}

    def test_option_n_opens_asks_then_unread_ones_oldest_first(self):
        since = self.needy(); hits = self.hits()
        # The line map carries need and since on a red or a yellow row only, beside the keys every row has.
        self.assertEqual({h["key"]: (h.get("need"), h.get("since")) for h in hits.values()},
                         {"control": ("unread", since["control"]), "ALF": ("asks", since["ALF"]), "BOB": ("unread", since["BOB"]), "IDLE": (None, None),
                          "MID": ("asks", since["MID"]), "READ": (None, None), "YAK": ("unread", since["YAK"]), "ZED": ("asks", since["ZED"])})
        self.assertEqual(next(h for h in hits.values() if h["key"] == "ZED"), {"key": "ZED", "session_id": "s-zed-full", "glyph": "●", "need": "asks", "since": since["ZED"]})
        # From nothing open, each press opens the next in that order, passing over the row open now, and wraps; READ and IDLE never come up.
        here, claude = "agent view", Path(self.home) / "claude.log"
        for key in ("ZED", "MID", "ALF", "YAK", "BOB", "control", "ZED"):
            self.assertEqual(self.press("go", "need", "%9", title=here), self.attached(key, f"s-{key.lower()}-full"[:8]), here)
            self.assertFalse(claude.exists(), key); here = key

    def test_option_n_with_nothing_waiting_on_ian_starts_nothing(self):
        # Waiting on nobody: idle, working, a read reply. Waiting on Ian but passed over: the row open now, a session in another
        # terminal, whose click only explains, and a cold control, whose click would replace it.
        for k in ("IDLE", "WORK", "READ", "OPEN", "TERM"): self.write_charter(k)
        self.live("s-idle-full", "IDLE", self.pid(), last_turn_at=self.ago(0.5))
        self.live("s-work-full", "WORK", self.pid(), status="busy", state="busy", last_turn_at=self.ago(0.5))
        self.live("s-read-full", "READ", self.pid(), state="waiting", waiting_for="replied", last_turn_at=self.ago(3))
        self.live("s-open-full", "OPEN", self.pid(), state="waiting", waiting_for="permission", waiting_since=self.ago(1))
        self.live("s-term-full", "TERM", self.pid(), "interactive", state="waiting", waiting_for="question", waiting_since=self.ago(2))
        self.live("s-ctl-full", "control", self.pid(), kind="control", workstream=None, state="waiting", waiting_for="replied", last_turn_at=self.ago(2))
        (self.board / "open.json").write_text(json.dumps({"open": "OPEN", "seen": {"READ": self.ago(0)}}))
        self.assertEqual({h["key"]: h.get("need") for h in self.hits().values()}, {"control": "unread", "IDLE": None, "OPEN": "asks", "READ": None, "TERM": "asks", "WORK": None})
        self.assertEqual(self.press("go", "need", "%9", title="OPEN"), ["display-message|workstreams: nothing needs you|"])
        self.assertFalse((Path(self.home) / "claude.log").exists())   # nothing stopped, dispatched or listed

    # --- a block: the charter waits on something outside its session, so its row is not Ian's move ---

    def blocked(self, key="HELD", what="PR #12", since=None, **kw):
        """A charter for `key` blocked on `what` since `since`, an ISO time, or with no blocked_since: line when None."""
        self.write_charter(key, extra=f"blocked: {what}\n" + (f"blocked_since: {since}\n" if since else ""), **kw)

    def test_a_blocked_charter_has_its_own_group(self):
        # Between Active and Idle in both layouts, labelled with what it waits on and its age; with nothing live, a click starts a session.
        self.blocked(since=self.ago(50)); self.write_charter("DORM", created=time.strftime("%Y-%m-%d", time.gmtime()))
        self.write_charter("BUSY"); self.put("s-busy", "BUSY", self.me(), state="busy"); self.put("s-loose", "loose", self.me())
        lines, wide = self.tree(40).splitlines(), self.render("--no-color")
        self.assertEqual([l for l in lines if l.startswith("▾")], ["▾ Control", "▾ Active", "▾ Blocked", "▾ Idle", "▾ Unassigned"])
        self.assertEqual([l for l in wide.splitlines() if l in ("Active", "Blocked", "Idle", "Unassigned")], ["Active", "Blocked", "Idle", "Unassigned"])
        self.assertEqual(lines[lines.index("▾ Blocked") + 1], "  ⏸ HELD" + " " * 21 + "PR #12 · 2d")
        self.assertEqual(sum(" HELD" in l for l in lines), 1)   # nothing for it in Idle
        self.assertRegex(wide, r"(?m)^  HELD +– +PR #12 · 2d +PROJ-1  \[no goals\]$")
        self.assertIn("\x1b[2mPR #12 · 2d", self.render("--color"))   # dim, as the tree draws it
        line, hit = self.row_of("HELD")
        self.assertEqual(hit, {"key": "HELD", "session_id": None, "glyph": "⏸"})   # no need, and nothing to reset
        self.assertEqual(self.open_line(line, self.BG.replace("control", "HELD")), self.attached("HELD", "9d9d9d9d"))
        self.assertEqual((Path(self.home) / "claude.log").read_text(), self.dispatched("HELD") + "\n")

    def test_a_blocked_row_reopens_and_resets_its_session(self):
        # A click does what it does on that row's session; once the session is stale, the arrow replaces the pause and starts fresh.
        self.blocked(since=self.ago(5)); L = json.dumps([{"sessionId": "s-held-full", "id": "s-held-f", "kind": "background", "state": "done"}]); self.listing(L)
        self.put("s-held-full", "HELD", None, last_turn_at=self.ago(0.5))
        self.assertEqual(self.row_of("HELD")[1], {"key": "HELD", "session_id": "s-held-full", "glyph": "⏸"})   # retired and warm
        self.put("s-held-full", "HELD", None, last_turn_at=self.ago(2))
        line, hit = self.row_of("HELD")
        self.assertEqual(hit, {"key": "HELD", "session_id": "s-held-full", "glyph": "↻", "reset_x": 2})
        self.assertEqual(self.tree(40).splitlines()[line], "  ↻ HELD" + " " * 21 + "PR #12 · 5h")
        self.assertEqual(self.open_line(line, listing=L), self.attached("HELD", "s-held-f"))   # revived
        self.later(); self.assertEqual(self.open_line(line, self.BG.replace("control", "HELD"), x=2, listing=L), self.attached("HELD", "9d9d9d9d"))
        self.assertEqual((Path(self.home) / "claude.log").read_text(), self.dispatched("HELD") + "\n")
        # A live session, settled and cold under a block: the arrow stops it, then starts fresh.
        sline, shit = self.stale_live(extra="blocked: PR #12\n")
        self.assertEqual(shit, {"key": "STALE", "session_id": "s-stale-full", "glyph": "↻", "reset_x": 2})
        self.assertEqual(self.group_of(self.render("--no-color"), "STALE"), "Blocked")
        self.later(); self.assertEqual(self.open_line(sline, self.BG.replace("control", "STALE"), x=2), self.attached("STALE", "9d9d9d9d"))
        self.assertEqual((Path(self.home) / "claude.log").read_text().splitlines(), ["stop s-stale-", self.dispatched("STALE")])

    def test_only_what_ian_can_act_on_lifts_a_block_into_active(self):
        # Running, or asking Ian for a permission, a question or what the listing names: Active, as without a block.
        # A reply, read or unread, or a settled session: Blocked, with the block for its state and no need.
        sub, keys = {"agent_id": "a1", "name": "Explore", "state": "running"}, ("REPLY", "READ", "IDLE", "PERM", "ASK", "LISTED", "WORK", "KID")
        for key, kw in zip(keys, (dict(state="waiting", waiting_for="replied", last_turn_at=self.ago(0.5)),
                                  dict(state="waiting", waiting_for="replied", last_turn_at=self.ago(0.5)),
                                  dict(last_turn_at=self.ago(0.5)),
                                  dict(state="waiting", waiting_for="permission", waiting_since=self.ago(0.2)),
                                  dict(state="waiting", waiting_for="question", waiting_since=self.ago(0.1)),
                                  dict(last_turn_at=self.ago(0.3)),
                                  dict(status="busy", state="busy", last_turn_at=self.ago(0.1)),
                                  dict(state="waiting", waiting_for="replied", last_turn_at=self.ago(0.5), children=[sub]))):
            self.blocked(key); self.live(f"s-{key.lower()}-full", key, self.pid(), **kw)
        (self.board / "open.json").write_text(json.dumps({"seen": {"READ": self.ago(0)}}))
        self.listing(json.dumps([{"sessionId": "s-listed-full", "state": "blocked", "waitingFor": "approve push"}]))
        wide, hits = self.render("--no-color"), self.hits()
        self.assertEqual({k: self.group_of(wide, k) for k in keys}, {"REPLY": "Blocked", "READ": "Blocked", "IDLE": "Blocked", "PERM": "Active",
                                                                     "ASK": "Active", "LISTED": "Active", "WORK": "Active", "KID": "Active"})
        self.assertEqual({h["key"]: (h["glyph"], h.get("need")) for h in hits.values() if h["key"] != "control"},
                         {"REPLY": ("⏸", None), "READ": ("⏸", None), "IDLE": ("⏸", None), "PERM": ("●", "asks"), "ASK": ("●", "asks"),
                          "LISTED": ("●", "asks"), "WORK": ("▶", None), "KID": ("▶", None)})
        self.assertRegex(wide, r"(?m)^  REPLY +s-reply- +PR #12 +PROJ-1")   # its session's id, and the block for its state
        colour = self.tree(40, "--color")
        self.assertIn("\x1b[31m●\x1b[0m \x1b[36mPERM\x1b[0m", colour); self.assertIn("\x1b[2m⏸\x1b[0m \x1b[36mREPLY\x1b[0m", colour)

    def test_option_n_passes_over_a_blocked_row(self):
        # The ask under a block comes first, ahead of an older unread reply elsewhere; the blocked reply, older still, never comes up.
        self.blocked("PERM"); self.live("s-perm-full", "PERM", self.pid(), state="waiting", waiting_for="permission", waiting_since=self.ago(1))
        self.blocked("REPLY"); self.live("s-reply-full", "REPLY", self.pid(), state="waiting", waiting_for="replied", waiting_since=self.ago(5), last_turn_at=self.ago(5))
        self.write_charter("NEWS"); self.live("s-news-full", "NEWS", self.pid(), state="waiting", waiting_for="replied", waiting_since=self.ago(3), last_turn_at=self.ago(3))
        self.hits(); here = "agent view"
        for key in ("PERM", "NEWS", "PERM"):
            self.assertEqual(self.press("go", "need", "%9", title=here), self.attached(key, f"s-{key.lower()}-full"[:8]), here); here = key

    def test_option_n_with_only_blocked_rows_starts_nothing(self):
        self.blocked("REPLY"); self.live("s-reply-full", "REPLY", self.pid(), state="waiting", waiting_for="replied", last_turn_at=self.ago(0.5))
        self.blocked("DORM", created=time.strftime("%Y-%m-%d", time.gmtime()))   # nothing live
        self.assertEqual({h["key"]: h.get("need") for h in self.hits().values()}, {"control": None, "DORM": None, "REPLY": None})
        self.assertEqual(self.press("go", "need", "%9"), ["display-message|workstreams: nothing needs you|"])
        self.assertFalse((Path(self.home) / "claude.log").exists())

    def test_a_blocked_charter_never_goes_stale(self):
        # Created long ago and no turn in years: the block keeps its row, with no age when the charter carries no blocked_since.
        old = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 30 * 86400))
        self.blocked(what="Dana's review", created=old); self.write_charter("OLD", created=old)
        self.put("s-held-full", "HELD", None, last_turn_at="2020-01-01T00:00:00Z")
        out, lines = self.render("--no-color"), self.tree(40).splitlines()
        self.assertEqual(self.group_of(out, "HELD"), "Blocked"); self.assertIn("stale (1, no session in 3d): OLD", out)
        self.assertRegex(out, r"(?m)^  HELD +– +Dana's review +PROJ-1")
        self.assertEqual(lines[-1], "stale (1): OLD"); self.assertIn("  ⏸ HELD" + " " * 19 + "Dana's review", lines)

    def test_a_block_reads_its_value_quoted_or_not_and_its_age(self):
        # Minutes under an hour, hours under two days, days after; no age without a time that parses; an empty block is none.
        today = time.strftime("%Y-%m-%d", time.gmtime())
        for k, extra in (("MIN", f'blocked: "PR #12"\nblocked_since: {self.ago(5 / 60)}\n'), ("HRS", f"blocked: 'PR #12'\nblocked_since: \"{self.ago(47.5)}\"\n"),
                         ("DAYS", f"blocked: PR #12  \nblocked_since: {self.ago(48)}\n"), ("BAD", "blocked: PR #12\nblocked_since: yesterday\n"),
                         ("EMPTY", 'blocked: ""\n'), ("SPACE", "blocked: '  '\n"), ("BARE", f"blocked:\nblocked_since: {self.ago(1)}\n")):
            self.write_charter(k, created=today, extra=extra)
        out = self.render("--no-color"); labels = dict(re.findall(r"(?m)^  (\S+) +– +(.+?)  ", out))
        self.assertEqual({k: self.group_of(out, k) for k in ("MIN", "HRS", "DAYS", "BAD", "EMPTY", "SPACE", "BARE")},
                         {"MIN": "Blocked", "HRS": "Blocked", "DAYS": "Blocked", "BAD": "Blocked", "EMPTY": "Idle", "SPACE": "Idle", "BARE": "Idle"})
        self.assertEqual({k: labels[k] for k in ("MIN", "HRS", "DAYS", "BAD")}, {"MIN": "PR #12 · 5m", "HRS": "PR #12 · 47h", "DAYS": "PR #12 · 2d", "BAD": "PR #12"})

    def test_a_blocked_row_is_exactly_the_width(self):
        # The label takes up to half the row, the name truncates first, and a long label gives up what it waits on before its age.
        self.blocked(since=self.ago(50)); self.blocked("A_VERY_LONG_WORKSTREAM_KEY_INDEED", what="the review of PR #620 from Dana", since=self.ago(50))
        self.blocked("STALE", since=self.ago(50)); self.put("s-stale", "STALE", self.me(), last_turn_at=self.ago(2))
        for width in (30, 40):
            for flag in ("--no-color", "--color"):
                rows = [l for l in (re.sub(r"\x1b\[[0-9;]*m", "", l) for l in self.tree(width, flag).splitlines()) if l[2:3] in ("⏸", "↻")]
                self.assertEqual(len(rows), 3, (width, flag))
                for l in rows: self.assertTrue(len(l) == width and l.endswith(" · 2d"), (width, flag, l))
        self.assertIn("  ⏸ A_VERY_LO… the revie… · 2d", self.tree(30).splitlines())
        plain, colour = self.tree(40).splitlines(), self.tree(40, "--color").splitlines()
        self.assertIn("  ⏸ A_VERY_LONG_WO… the review of … · 2d", plain)
        self.assertIn("  ⏸ HELD" + " " * 21 + "PR #12 · 2d", plain); self.assertIn("  ↻ STALE" + " " * 20 + "PR #12 · 2d", plain)
        self.assertIn("\x1b[2m  \x1b[0m\x1b[2m⏸\x1b[0m \x1b[36mHELD\x1b[0m" + " " * 21 + "\x1b[2mPR #12 · 2d\x1b[0m", colour)
        self.assertIn("\x1b[2m  \x1b[0m\x1b[2m↻\x1b[0m \x1b[36mSTALE\x1b[0m" + " " * 20 + "\x1b[2mPR #12 · 2d\x1b[0m", colour)

    # --- Option+e: Ian's note on a workstream, its charter's note: line, a dim line under its row that nothing else reads ---

    NOTE = 'it\'s "50%" #1, ok; $HOME \\ done'
    NOTE_LINE = 'note: "it\'s \\"50%\\" #1, ok; $HOME \\\\ done"\n'   # NOTE as a YAML double-quoted string

    def note(self, key, text):
        """Gives `key`'s charter, as write_charter() wrote it, the note `text` after its focus: line."""
        p = self.charter_path(key); esc = text.replace("\\", "\\\\").replace('"', '\\"')
        p.write_text(p.read_text().replace("focus: PROJ-1\n", f'focus: PROJ-1\nnote: "{esc}"\n'))

    @staticmethod
    def tmux_words(line):
        """`line`'s words as tmux's parser reads them, in the shapes note() writes: bare words, and double quotes with \\ escapes and \\ooo octal."""
        words = []
        for w in re.findall(r'"(?:\\.|[^"\\])*"|[^\s"]+', line):
            if w.startswith('"'):
                assert "$" not in re.sub(r"\\.", "", w), w   # tmux expands a bare $ in double quotes
                w = re.sub(r"\\([0-3][0-7]{2}|.)", lambda m: chr(int(m[1], 8)) if len(m[1]) == 3 else m[1], w[1:-1])
            words.append(w)
        return words

    def test_option_e_prompts_for_the_open_workstream_holding_its_note(self):
        # One prompt, on the client that pressed it, labelled with the key and holding its note. Its Enter sets the pane's @ws_note from %%%,
        # then runs save-note for the key: any charter's key, through the parser, run-shell's formats and the shell.
        odd = 'x#2%"$HOME\\it\'s'; self.write_charter("NOTED", extra=self.NOTE_LINE); self.write_charter(odd); self.board.mkdir(parents=True)
        for key, label, held in (("NOTED", "note for NOTED: ", 'it\'s "50%%" ##1, ok; $HOME \\ done'),
                                 (odd, 'note for x##2%%"$HOME\\it\'s: ', "")):
            self.open_now(key)
            (call,) = self.press("note", "/dev/ttys042", "%9", title=key)
            *flags, template, _ = call.split("|")
            self.assertEqual(flags, ["command-prompt", "-b", "-l", "-t", "/dev/ttys042", "-p", label, "-I", held], key)
            self.assertEqual(template.count("%"), 3, template)   # %%% is its only %: a %1, as in a pane id, would take the reply too
            self.assertIn(' @ws_note "%%%" ; ', template)   # %%% escapes only inside double quotes
            words = self.tmux_words(template)
            self.assertEqual(words[:11], ["set", "-p", "-t", "%9", "@ws_note", "%%%", ";", "run-shell", "-b", "-c", os.getcwd()], key)
            self.assertNotIn("#", words[11].replace("##", ""), key)   # run-shell would expand a lone #
            self.assertEqual(shlex.split(words[11].replace("##", "#")), [sys.executable, str(HOOK), "save-note", key, "%9"], key)

    def test_option_e_says_why_it_opens_no_prompt(self):
        # Nothing open, or cc's rebuilt pane on the agent view; or a session with no charter to hold a note: unassigned, or control.
        self.write_charter("NOTED"); self.board.mkdir(parents=True, exist_ok=True)
        for opened, title, said in ((None, "agent view", ["-c", "workstreams: open a session first"]), ("NOTED", "agent view", ["-c", "workstreams: open a session first"]),
                                    ("loose", "loose", ["-l", "-c", "workstreams: loose has no charter to note"]),
                                    ("control", "control", ["-l", "-c", "workstreams: control has no charter to note"])):
            self.open_now(opened)
            (call,) = self.press("note", "/dev/ttys042", "%9", title=title)
            self.assertEqual(call.split("|")[:-1], ["display-message", *said[:-1], "/dev/ttys042", said[-1]], opened)

    def save(self, key, reply):
        """The prompt's Enter: save-note for `key`, its tmux answering the pane's @ws_note with `reply`."""
        self.assertEqual(self.board_py("save-note", key, "%9", panes=reply), ["-u|show|-pv|-t|%9|@ws_note|"])

    def test_saving_a_note_changes_only_its_charter_line(self):
        # Inserted after focus:, replaced where it stands, removed when empty; the rest of the frontmatter, the goals and the body byte for byte.
        self.write_charter("NOTED", extra=self.GOALS); p = self.charter_path("NOTED"); before = p.read_text()
        self.save("NOTED", self.NOTE); self.assertEqual(p.read_text(), before.replace("focus: PROJ-1\n", "focus: PROJ-1\n" + self.NOTE_LINE))
        self.save("NOTED", "  second  "); self.assertEqual(p.read_text(), before.replace("focus: PROJ-1\n", 'focus: PROJ-1\nnote: "second"\n'))   # stripped
        self.save("NOTED", "   "); self.assertEqual(p.read_text(), before)
        ino = p.stat().st_ino; self.save("NOTED", ""); self.assertEqual((p.read_text(), p.stat().st_ino), (before, ino))   # nothing to remove: not rewritten
        self.save("loose", "x"); self.assertEqual(sorted(f.name for f in p.parent.iterdir()), [".lock", "NOTED.md"])   # no charter: nothing written but the lock
        self.save("NOTED", "kept"); self.panes("lost", code=1)   # no @ws_note to read: nothing changes
        subprocess.run([sys.executable, str(HOOK), "save-note", "NOTED", "%9"], env=self.env, check=True)
        self.assertEqual(p.read_text(), before.replace("focus: PROJ-1\n", 'focus: PROJ-1\nnote: "kept"\n'))

    def test_a_charter_without_focus_takes_its_note_last(self):
        p = self.charter_path("BARE"); p.parent.mkdir(parents=True, exist_ok=True); p.write_text("---\nworkstream: BARE\nstatus: active\n---\n\nHandoff.\n")
        self.save("BARE", "hello"); self.assertEqual(p.read_text(), '---\nworkstream: BARE\nstatus: active\nnote: "hello"\n---\n\nHandoff.\n')
        p.write_text("---\nworkstream: BARE\nnote: by hand\nstatus: active\n---\n\nHandoff.\n")   # a note: line where a hand put it is replaced there
        self.save("BARE", "new"); self.assertEqual(p.read_text(), '---\nworkstream: BARE\nnote: "new"\nstatus: active\n---\n\nHandoff.\n')

    def test_an_odd_note_value_reads_as_it_stands(self):
        # Only save-note writes the quoted form; whatever else a hand leaves reads as it stands, and none of it raises.
        today = time.strftime("%Y-%m-%d", time.gmtime())
        odd = {"PLAIN": ("note: parked for now", "parked for now"), "SINGLE": ("note: 'quoted'", "'quoted'"), "OPEN": ('note: "unterminated', '"unterminated'),
               "TRAIL": ('note: "a" b', '"a" b'), "ESC": ('note: "a \\q b"', "a q b"), "EMPTY": ("note:", None), "BLANK": ('note: ""', None)}
        for k, (line, _) in odd.items(): self.write_charter(k, created=today, extra=line + "\n")
        shown, key = {}, None
        for l in self.tree(80).splitlines():   # render() and tree() assert a clean exit
            if l[2:3] == "◌": key = l.split()[1]
            elif "✎" in l: shown[key] = l.split("✎ ", 1)[1]
        self.assertEqual(shown, {k: s for k, (_, s) in odd.items() if s})
        self.assertEqual(sum("note:" in l for l in self.render("--no-color").splitlines()), 5)
        self.open_now("PLAIN"); self.assertEqual(self.press("note", "/dev/ttys042", "%9", title="PLAIN")[0].split("|")[8], "parked for now")

    def test_the_session_reads_its_note_in_its_charter(self):
        self.write_charter(); self.save("PAYMENTS_API", self.NOTE)
        self.assertIn("\n" + self.NOTE_LINE, self.charter_of()["text"])

    def test_a_note_sits_under_its_row_on_the_trunk(self):
        # Directly under the row, above its hint and children; on the group's trunk mid-group, a space on its last row; never on the line map.
        # A hint is drawn only on an Unassigned row, so the noted row with one is a live session of an archived charter.
        me = self.me(); self.write_charter("A"); self.write_charter("B"); self.write_charter("G", extra='refs:\n  worktrees: [".worktrees/k/g"]\n')
        self.write_charter("ARCH"); p = self.charter_path("ARCH"); p.write_text(p.read_text().replace("status: active", "status: archived"))
        sub = lambda n, st="done": {"agent_id": n, "name": n, "state": st}
        self.put("a1", "A", me, children=[sub("one"), sub("two")]); self.put("b1", "B", me, children=[sub("three", "paused")])
        self.put("s-arch", "ARCH", me, cwd="/w/.worktrees/k/g", children=[sub("four")]); self.put("s-loose", "loose", me)
        plain = self.tree(40).splitlines()
        self.note("A", "mid note"); self.note("B", "last note"); self.note("ARCH", "archived")
        row = {k: next(i for i, l in enumerate(plain) if l.startswith(f"  ○ {k} ")) for k in ("A", "B", "ARCH")}
        self.assertEqual([plain[row[k] + 1][:5] for k in ("A", "B", "ARCH")], ["  │ ├", "    └", "  │ →"])   # what the note goes above
        want = list(plain)
        for k, line in sorted({"A": "  │ ✎ mid note", "B": "    ✎ last note", "ARCH": "  │ ✎ archived"}.items(), key=lambda kv: -row[kv[0]]):
            want.insert(row[k] + 1, line)
        lines = self.tree(40).splitlines(); self.assertEqual(lines, want)
        hits = self.hits()   # the click map still names the row drawn on each of its lines
        self.assertEqual({n: lines[n].split()[1] for n in hits}, {n: h["key"] for n, h in hits.items()})
        self.assertEqual(len(hits), 5)

    def test_a_note_line_fits_the_width(self):
        me = self.me(); self.write_charter("LONG"); self.write_charter("SHORT"); self.put("s-long", "LONG", me); self.put("s-short", "SHORT", me)
        self.note("LONG", "parked until Dana's review of the stacked PRs lands"); self.note("SHORT", "ok")
        for width in (30, 40):
            for flag in ("--no-color", "--color"):
                out = [re.sub(r"\x1b\[[0-9;]*m", "", l) for l in self.tree(width, flag).splitlines() if "✎" in l]
                self.assertEqual(len(out), 2, (width, flag)); long_, short = out
                self.assertTrue(len(long_) == width and long_.startswith("  │ ✎ parked until") and long_.endswith("…"), (width, flag, long_))
                self.assertEqual(short, "    ✎ ok", (width, flag))
        self.assertIn("  │ ✎ parked until Dana's review of the…", self.tree(40).splitlines())
        self.assertIn("\x1b[2m  │ ✎ parked until Dana's review of the…\x1b[0m", self.tree(40, "--color").splitlines())   # dim, all of it

    def test_a_duplicate_key_shows_its_note_once(self):
        me = self.me(); self.write_charter("A"); self.put("a1", "A", me); self.put("a2", "A", me); self.note("A", "one note")
        lines = self.tree(40).splitlines(); rows = [i for i, l in enumerate(lines) if l.startswith("  ○ A! ")]
        self.assertEqual((len(rows), lines[rows[0] + 1], sum("✎" in l for l in lines)), (2, "  │ ✎ one note", 1))
        wide = self.render("--no-color").splitlines(); i = next(i for i, l in enumerate(wide) if " a1 " in l)
        self.assertEqual((wide[i + 1], sum("note:" in l for l in wide)), ("      note: one note", 1))

    def test_the_wide_board_puts_a_note_line_first_under_its_row(self):
        self.write_charter("A"); self.put("a1", "A", self.me(), last_message="Pushed the fix.")
        self.write_charter("D", created=time.strftime("%Y-%m-%d", time.gmtime())); self.note("A", "parked"); self.note("D", "later")
        wide = self.render("--no-color").splitlines()
        a, d = (next(i for i, l in enumerate(wide) if l.startswith(f"  {k} ")) for k in ("A", "D"))
        self.assertEqual(wide[a + 1:a + 3], ["      note: parked", "      last: Pushed the fix."])
        self.assertEqual(wide[d + 1], "      note: later")
        self.assertIn("\x1b[2m      note: parked\x1b[0m", self.render("--color"))

    def test_notes_change_nothing_but_their_own_lines(self):
        # A note moves, recolours, marks and maps nothing: grouping, colours, ↻, Option+n and clicks.
        self.board_of_every_kind(); self.put("s-cold", "OLD", self.me(), last_turn_at=self.ago(2))
        entries = lambda: [h for _, h in sorted(self.hits().items())]
        drop = lambda text: [l for l in text.splitlines() if "✎" not in l and "note:" not in l]
        plain = (drop(self.render("--no-color")), drop(self.tree(40, "--color")), entries())
        for k in ("WAIT", "BUSY", "IDLE", "DORM", "OLD", "A_VERY_LONG_WORKSTREAM_KEY_INDEED"): self.note(k, "noted")
        self.assertEqual(drop(self.render("--no-color")), plain[0])
        self.assertEqual(drop(self.tree(40, "--color")), plain[1])
        self.assertEqual(entries(), plain[2])
        self.assertTrue(any("↻" in l for l in plain[1]) and any("need" in h for h in plain[2]))   # the board compared carries both
        self.assertEqual(sum("✎" in l for l in self.tree(40).splitlines()), 6)

    # --- bin/ws: the tmux session cc builds, on a tmux server of the test's own ---

    def test_ws_builds_the_session_and_rebinds_on_every_run(self):
        if not shutil.which("tmux"): self.skipTest("no tmux")
        ws, proj = HOOK.parents[1] / "bin" / "ws", Path(self.home) / "proj"; proj.mkdir()
        env = {k: v for k, v in self.env.items() if k not in ("TMUX", "TMUX_PANE")} | {"TMUX_TMPDIR": tempfile.mkdtemp(dir="/tmp"), "TERM": "xterm-256color"}
        (self.bin / "claude").write_text('#!/bin/sh\n[ "$2" = --json ] && { echo "[]"; exit 0; }\nexec sleep 60\n'); (self.bin / "claude").chmod(0o755)
        (self.bin / "uv").write_text(f'#!/bin/sh\nshift 2\nexec {sys.executable} "$@"\n'); (self.bin / "uv").chmod(0o755)   # uv finds no Python under a fake HOME
        tmux = lambda *a: subprocess.run(["tmux", *a], env=env, capture_output=True, text=True).stdout
        run = lambda cwd=proj: subprocess.run([str(ws)], cwd=cwd, env=env, capture_output=True, text=True, timeout=30)   # its attach fails: no terminal here
        try:
            run()
            panes = {p: c for c, p in (l.rsplit(" ", 1) for l in tmux("list-panes", "-t", "ws", "-F", "#{pane_start_command} #{pane_id}").splitlines())}
            self.assertEqual(len(panes), 2, panes)
            side = next(p for p, c in panes.items() if "render --watch --tree" in c); right = next(p for p in panes if p != side)
            where = tmux("display", "-p", "-t", side, "#{pane_current_path}").strip(); self.assertEqual(where, os.path.realpath(proj))
            want = [f"MouseDown1Pane if-shell -F -t = \"#{{==:#{{pane_id}},{side}}}\" \"run-shell -b -c '{where}' 'uv run --no-project {HOOK} open #{{mouse_y}} {right} #{{mouse_x}}'\"",
                    *(f"M-{k} if-shell -F \"#{{==:#{{session_name}},ws}}\" \"run-shell -b -c '{where}' 'uv run --no-project {HOOK} {cmd} {right}'\" \"send-keys M-{k}\""
                      for k, cmd in (("j", "go next"), ("k", "go prev"), ("m", "menu #{client_name}"), ("n", "go need"), ("e", "note #{client_name}")))]
            keys = " ".join(tmux("list-keys", "-T", "root").split())
            for w in want: self.assertIn(" ".join(w.split()), keys)
            self.assertEqual(tmux("show", "-p", "-t", right, "-v", "@ws_name").strip(), "agent view")
            # A running ws gets the current bindings on the next run, from any directory, and nothing is rebuilt.
            tmux("unbind", "-n", "M-j"); run(self.home)
            self.assertIn(" ".join(want[1].split()), " ".join(tmux("list-keys", "-T", "root").split()))
            self.assertEqual(sorted(tmux("list-panes", "-t", "ws", "-F", "#{pane_id}").split()), sorted(panes))
        finally: tmux("kill-server")

    # --- goals: the charter's ordered checklist, and its stopping condition ---

    def test_a_charter_without_goals_is_noted(self):
        # An exit: line is not goals: nothing reads it any more.
        me = self.me(); self.write_charter("G", extra='goals:\n  - "[ ] Ship it"\n'); self.write_charter("N"); self.write_charter("E", extra="exit: it ships\n")
        self.put("g1", "G", me); self.put("n1", "N", me); self.put("e1", "E", me)
        rows = {l.split()[0]: l for l in self.render("--no-color").splitlines() if l.startswith("  ") and not l.startswith("   ")}
        self.assertNotIn("[no goals]", rows["G"]); self.assertTrue(rows["N"].endswith("  [no goals]"), rows["N"]); self.assertTrue(rows["E"].endswith("  [no goals]"), rows["E"])

    def test_unread_label_counts_open_goals(self):
        me = self.me()
        charters = {"NONE": "", "TWO": 'goals:\n  - "[x] Ship the renderer"\n  - "[ ] Run the schema migration"\n  - "[ ] Sweep"\n',
                    "ONE": "goals:\n  - [x] Ship\n  - [ ] Migrate\n", "DONE": "goals:\n  - [x] Ship\n  - '[x] Migrate'\n", "PERM": "goals:\n  - [ ] Ship\n"}
        for k, extra in charters.items():
            self.write_charter(k, extra=extra)
            self.put(f"s-{k}", k, me, state="waiting", waiting_for="permission" if k == "PERM" else "replied")
        wide = {l.split()[0]: l for l in self.render("--no-color").splitlines() if l.startswith("  ") and not l.startswith("   ")}
        want = {"NONE": "waiting · unread ", "TWO": "waiting · unread · 2 goals left ", "ONE": "waiting · unread · 1 goal left ",
                "DONE": "waiting · unread · all done ", "PERM": "waiting · permission "}
        for k, label in want.items(): self.assertIn(label, wide[k], k)
        self.assertIn("\x1b[33mwaiting · unread · all done", self.render("--color"))   # a finished turn stays yellow whatever its label
        narrow = {l.split()[1]: l for l in self.tree(40).splitlines() if l.startswith("  ") and l[2] in "●"}
        for k, label in {"NONE": "unread", "TWO": "2 goals left", "ONE": "1 goal left", "DONE": "all done", "PERM": "permission"}.items():
            self.assertTrue(narrow[k].endswith(" " + label), narrow[k])
        self.assertEqual(json.loads((self.board / "s-TWO.json").read_text())["waiting_for"], "replied")   # display only: the record keeps the bare value

    def idle_rows(self):
        """{key: wide state} and {key: tree row, at the sidebar's 40 columns} for every dormant row."""
        wide = dict(re.findall(r"(?m)^  (\S+) +– +(.+?)(?:  |$)", self.render("--no-color")))
        narrow = {l.split()[1]: l for l in self.tree(40).splitlines() if l.startswith("  ") and l[2:3] in ("●", "▶", "○", "◌")}
        return wide, narrow

    def test_a_dormant_row_says_what_is_left(self):
        # The Idle group already says dormant, so the row says what is left, with hours once a turn exists.
        hour_ago, today = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600)), time.strftime("%Y-%m-%d", time.gmtime())
        charters = {"DONE": "goals:\n  - [x] Ship\n  - '[x] Migrate'\n", "TWO": 'goals:\n  - "[x] Ship"\n  - "[ ] Migrate"\n  - [ ] Sweep\n',
                    "ONE": "goals:\n  - [x] Ship\n  - [ ] Migrate\n", "NONE": "", "MANY": "goals:\n" + "".join(f"  - [ ] Goal {i}\n" for i in range(12))}
        for k, extra in charters.items():
            self.write_charter(k, extra=extra); self.put(f"s-{k}", k, None, last_turn_at=hour_ago)
        self.write_charter("NEW", created=today); self.put("s-NEW", "NEW", None)   # a record, but no turn on it
        wide, narrow = self.idle_rows()
        for k, label in {"DONE": "all done", "TWO": "2 goals left", "ONE": "1 goal left", "NONE": "no goals", "MANY": "12 goals left", "NEW": "not started"}.items():
            self.assertRegex(wide[k], rf"^{label}$" if k == "NEW" else rf"^{label} · [\d.]+h$")
            self.assertTrue(narrow[k].startswith(f"  ◌ {k} ") and narrow[k].endswith(" " + label), narrow[k])   # "12 goals left" is the whole state cap
        self.assertNotIn("dormant", self.render("--no-color"))

    def test_dormant_label_precedence(self):
        # Every goal ticked beats no turn yet, and no turn yet beats a count of open goals.
        today = time.strftime("%Y-%m-%d", time.gmtime())
        self.write_charter("FIN", created=today, extra="goals:\n  - [x] Ship\n")
        self.write_charter("OPEN", created=today, extra="goals:\n  - [x] Ship\n  - [ ] Migrate\n")
        wide, narrow = self.idle_rows()
        self.assertEqual((wide["FIN"], wide["OPEN"]), ("all done", "not started"))
        self.assertTrue(narrow["FIN"].endswith(" all done") and narrow["OPEN"].endswith(" not started"), (narrow["FIN"], narrow["OPEN"]))

    def test_a_dormant_row_with_every_goal_ticked_is_done(self):
        # Last, after Unassigned, so Idle holds only work left; a live session, a block and the stale line all outrank it.
        me, done = self.me(), "goals:\n  - [x] Ship\n"
        for k, extra in {"FIN": done, "OPEN": "goals:\n  - [x] Ship\n  - [ ] Migrate\n", "LIVE": done, "HELD": "blocked: PR #12\n" + done, "OLDFIN": done}.items():
            self.write_charter(k, extra=extra)
        self.put("s-fin", "FIN", None, last_turn_at=self.ago(1)); self.put("s-open", "OPEN", None, last_turn_at=self.ago(1))
        self.put("s-live", "LIVE", me, last_turn_at=self.ago(1)); self.put("s-loose", "loose", me); self.put("s-old", "OLDFIN", None, last_turn_at=self.ago(100))
        wide, lines = self.render("--no-color"), self.tree(40).splitlines()
        self.assertEqual({k: self.group_of(wide, k) for k in ("FIN", "OPEN", "LIVE", "HELD", "loose", "OLDFIN")},
                         {"FIN": "Done", "OPEN": "Idle", "LIVE": "Active", "HELD": "Blocked", "loose": "Unassigned", "OLDFIN": None})
        self.assertEqual([l for l in wide.splitlines() if l in ("Active", "Blocked", "Idle", "Unassigned", "Done")], ["Active", "Blocked", "Idle", "Unassigned", "Done"])
        self.assertEqual([l for l in lines if l.startswith("▾")], ["▾ Control", "▾ Active", "▾ Blocked", "▾ Idle", "▾ Unassigned", "▾ Done"])
        self.assertRegex(lines[lines.index("▾ Done") + 1], r"^  ◌ FIN +all done$")
        self.assertEqual(self.row_of("FIN")[1], {"key": "FIN", "session_id": None, "glyph": "◌"})   # no need: Option+n passes over it
        self.assertIn("stale (1, no session in 3d): OLDFIN", wide)

    # --- the open workstream's charter, in the sidebar's spare rows ---

    PANEL_GOALS = ('goals:\n  - "[x] Ship"\n  - "[ ] Short goal"\n  - "[ ] A goal long enough to need a second row and then a third, which it never gets"\n'
                   '  - "[ ] Third goal"\n')

    def panel(self, current="OPEN", height=60, goals=PANEL_GOALS, extra="", color=False):
        """The sidebar at 40 columns and `height` rows with `current` open, under OPEN's charter with `extra` frontmatter; returns its rows after the stale line."""
        from unittest import mock
        ws = Path(self.home) / ".claude" / "projects" / "-Users-x-dev-myproject" / "workstreams"; ws.mkdir(parents=True, exist_ok=True)
        (ws / "OPEN.md").write_text("---\nworkstream: OPEN\npurpose: Ship the board, then retire the memory index it replaces.\n"
                                    "focus: 10-02 — the panel is built; next is landing it on master after Ian's go.\n"
                                    f"status: active\n{goals}{extra}---\n\nHandoff.\n")
        with mock.patch.dict(os.environ, self.env):
            mod = self.module(); lines = mod.tree(mod.load_records(), color=color, width=40, height=height, current=current).splitlines()
        return lines[next(i for i, l in enumerate(lines) if "stale (" in l) + 1:]

    def test_the_open_workstream_fills_the_rows_under_the_footer(self):
        # Under a double rule: its key and what is left, set off by blank rows, then purpose and open goals at two rows apiece. No focus.
        self.assertEqual(self.panel(), [
            "═" * 40,
            "",
            "OPEN                        3 goals left",
            "",
            "Ship the board, then retire the memory",
            "index it replaces.",
            "",
            "☐ Short goal",
            "☐ A goal long enough to need a second",
            "  row and then a third, which it never …",
            "☐ Third goal"])
        head = lambda left: ["═" * 40, "", "OPEN" + " " * (36 - len(left)) + left]
        self.assertEqual(self.panel(goals='goals:\n  - "[x] Ship"\n'), head("all done") + ["", "Ship the board, then retire the memory", "index it replaces."])
        self.assertEqual(self.panel(goals="")[:3], head("no goals"))

    def test_the_panel_shows_the_block_and_the_note_under_the_key(self):
        # What the work waits on, then Ian's note, dim and wrapped under their glyphs, as one section between the key and the purpose.
        rows = self.panel(extra=f'blocked: "api#179 merging, then ci_dev, post-deploy tests and ci_stage"\nblocked_since: {self.ago(3)}\n'
                                'note: "Needs team review"\n')
        self.assertEqual(rows[3:10], ["", "⏸ api#179 merging, then ci_dev, post-", "  deploy tests and ci_stage · 3h", "✎ Needs team review", "",
                                      "Ship the board, then retire the memory", "index it replaces."])
        self.assertIn("\x1b[2m✎ Needs team review\x1b[0m", self.panel(extra='note: "Needs team review"\n', color=True))
        self.assertEqual(self.panel(extra='note: "Only a note"\n')[3:6], ["", "✎ Only a note", ""])

    def test_the_panel_stops_a_row_short_of_the_pane(self):
        # The watch's trailing newline would scroll a full pane; a cut never ends on a blank row, and keeps a goal's indent.
        # Five rows down to the stale line, so a pane of `height` leaves height - 6 for the panel; at 13 the cut lands on a blank.
        for height, n, last in ((16, 10, "  row and then a third, which it never …"), (14, 8, "☐ Short goal …"), (13, 6, "index it replaces. …"),
                                (11, 5, "Ship the board, then retire the memory …")):
            rows = self.panel(height=height)
            self.assertEqual((len(rows), rows[-1]), (n, last), height)
        self.assertEqual(self.panel(height=10), [])   # room for the key, not for a row under it

    def test_no_panel_without_an_open_charter(self):
        # The agent view, control and an unassigned session open nothing to describe.
        for current in (None, "control", "loose"): self.assertEqual(self.panel(current), [], current)

    LONG = "A goal long enough to need a second row and then a third, which it never gets"

    def goal_map(self, height=60, **kw):
        """OPEN's panel drawn in-process with OPEN open, its line map written where a click reads it; returns (rows, map)."""
        from unittest import mock
        rows, hits = self.panel(**kw), {}
        with mock.patch.dict(os.environ, self.env):
            mod = self.module(); lines = mod.tree(mod.load_records(), width=40, height=height, hits=hits, current="OPEN").splitlines()
        (self.board / "tree-rows.json").write_text(json.dumps(hits))
        return lines, hits

    def test_each_row_of_an_open_goal_maps_to_it(self):
        # Both rows of a two-row goal, and nothing else in the panel: not the key, the block, the note, the purpose or a ticked goal.
        self.put("s-open", "OPEN", self.me())
        lines, hits = self.goal_map(extra='note: "Needs review"\n')
        goals = {lines[int(n)]: e["goal"] for n, e in hits.items() if "goal" in e}
        self.assertEqual(goals, {"☐ Short goal": "Short goal", "☐ A goal long enough to need a second": self.LONG,
                                 "  row and then a third, which it never …": self.LONG, "☐ Third goal": "Third goal"})
        self.assertTrue(all(e["key"] == "OPEN" for e in hits.values() if "goal" in e))
        from unittest import mock
        with mock.patch.dict(os.environ, self.env):   # a step and the menu see the rows alone
            mod = self.module(); self.assertEqual(sorted(map(int, mod.tree_rows())), sorted(n for n, e in hits.items() if "goal" not in e))
        lines, hits = self.goal_map(height=18)   # cut on the long goal's second row, which still sends it
        self.assertEqual((lines[-1], hits[len(lines) - 1]["goal"]), ("  row and then a third, which it never …", self.LONG))

    def click_goal(self, goal, title="OPEN"):
        """A click on `goal`'s first row with OPEN open in pane %9, titled `title`; returns the tmux calls after the title lookup."""
        lines, hits = self.goal_map()
        (self.board / "open.json").write_text(json.dumps({"open": "OPEN", "seen": {}}))
        line = next(n for n, e in hits.items() if e.get("goal") == goal)
        calls = self.board_py("open", str(line), "%9", panes=f"%9\t{title}")
        self.assertEqual(calls[0], "list-panes|-t|%9|-F|#{pane_id}\t#{@ws_name}|")
        return calls[1:]

    def test_a_click_on_a_goal_asks_the_open_session_to_focus_on_it(self):
        # Pasted, so a vim prompt in NORMAL mode takes it as text, then Enter, as Ian would type it.
        self.put("s-open", "OPEN", self.me())
        self.assertEqual(self.click_goal(self.LONG), [f"set-buffer|-b|ws-goal|Focus on this goal: {self.LONG}|",
                                                      "paste-buffer|-p|-d|-b|ws-goal|-t|%9|", "send-keys|-t|%9|Enter|"])
        self.put("s-open", "OPEN", self.me(), state="busy")   # a turn running queues it
        self.assertEqual(self.click_goal("Short goal")[-1], "send-keys|-t|%9|Enter|")

    def test_a_goal_click_never_answers_a_prompt(self):
        # Enter would answer a permission or a question; a reply waiting on Ian is no prompt, so it sends.
        for wait in ("permission", "question"):
            self.put("s-open", "OPEN", self.me(), state="waiting", waiting_for=wait)
            self.assertEqual(self.click_goal("Short goal"), ["display-message|-l|workstreams: OPEN is asking you something; answer it first|"], wait)
        self.put("s-open", "OPEN", self.me(), state="waiting", waiting_for="replied")
        self.assertEqual(self.click_goal("Short goal")[-1], "send-keys|-t|%9|Enter|")
        self.assertEqual(self.click_goal("Short goal", title="agent view"), ["display-message|-l|workstreams: OPEN is no longer open|"])

    # --- goals on the task list: mirrored from the charter, and ticked back into it ---

    GOALS = 'goals:\n  - "[x] Ship the renderer"\n  - "[ ] Run the schema migration"\n  - [ ] Sweep\n'

    def task_list(self, lid="s1"):
        return Path(self.home) / ".claude" / "tasks" / lid

    def tasks(self, lid="s1"):
        d = self.task_list(lid)
        return {p.stem: json.loads(p.read_text()) for p in d.glob("*.json")} if d.exists() else {}

    def goal_rows(self, lid="s1"):
        return {i: (t["subject"], t["status"]) for i, t in self.tasks(lid).items()}

    def task_file(self, tid, lid="s1", **kw):
        d = self.task_list(lid); d.mkdir(parents=True, exist_ok=True); p = d / f"{tid}.json"
        p.write_text(json.dumps({"id": tid, "subject": "Mine", "description": "", "status": "pending", "blocks": [], "blockedBy": []} | kw)); return p

    def set_status(self, tid, status, lid="s1"):
        # What Claude Code's TaskUpdate leaves on disk before PostToolUse fires.
        p = self.task_list(lid) / f"{tid}.json"; p.write_text(json.dumps(json.loads(p.read_text()) | {"status": status}))

    def charter_path(self, name="PAYMENTS_API"):
        return Path(self.home) / ".claude" / "projects" / "-Users-x-dev-myproject" / "workstreams" / f"{name}.md"

    def sync(self, *argv, env=None):
        """`board.py mirror s1`, or `board.py tick s1 <task-id>` for ("tick", id), as the mod's queue runs them: exit 0, nothing printed."""
        p = subprocess.run([sys.executable, str(HOOK), argv[0], "s1", *argv[1:]], capture_output=True, text=True, env=env or self.env)
        self.assertEqual((p.returncode, p.stdout), (0, ""), p.stderr)

    def mirror(self):
        self.sync("mirror")

    def task_update(self, tid):
        self.sync("tick", tid)   # what the mod queues once Claude Code's TaskUpdate has written the task's file

    def test_goals_seed_the_task_list_at_session_start(self):
        self.write_charter(extra=self.GOALS)
        d = self.task_list(); d.mkdir(parents=True); (d / ".highwatermark").write_text("4"); (d / ".lock").write_text("")
        self.mirror()
        self.assertEqual(self.goal_rows(), {"5": ("Ship the renderer", "completed"), "6": ("Run the schema migration", "pending"), "7": ("Sweep", "pending")})
        t = self.tasks()["6"]
        self.assertEqual((t["id"], t["blocks"], t["blockedBy"]), ("6", [], []))
        self.assertEqual(t["metadata"], {"workstream": "PAYMENTS_API", "workstream_goal": "Run the schema migration"})
        self.assertIn("PAYMENTS_API charter", t["description"])
        self.assertEqual(sorted(p.name for p in d.iterdir()), [".highwatermark", ".lock", "5.json", "6.json", "7.json"])   # no tmp left behind

    def test_goal_tasks_are_never_duplicated(self):
        self.write_charter(extra=self.GOALS)
        for _ in range(6): self.mirror()   # a start, prompts and turns' ends, and a resume
        self.assertEqual(self.goal_rows(), {"1": ("Ship the renderer", "completed"), "2": ("Run the schema migration", "pending"), "3": ("Sweep", "pending")})

    def test_other_tasks_are_left_alone_and_ids_continue_after_them(self):
        # A task is a goal task by its metadata alone: a matching subject is not one, nor another workstream's goal task.
        self.write_charter(extra=self.GOALS)
        mine = self.task_file("3", subject="Sweep", status="completed")
        theirs = self.task_file("1", subject="Sweep", metadata={"workstream": "OTHER", "workstream_goal": "Sweep"})
        before = (mine.read_text(), theirs.read_text())
        self.mirror()
        self.assertEqual((mine.read_text(), theirs.read_text()), before)
        self.assertEqual({i: r for i, r in self.goal_rows().items() if i not in ("1", "3")},
                         {"4": ("Ship the renderer", "completed"), "5": ("Run the schema migration", "pending"), "6": ("Sweep", "pending")})

    def test_a_deleted_goal_task_comes_back(self):
        self.write_charter(extra=self.GOALS)
        self.mirror()
        charter, d = self.charter_path().read_text(), self.task_list()
        (d / "1.json").unlink(); (d / ".highwatermark").write_text("1")   # what TaskUpdate status=deleted leaves
        self.task_update("1")
        self.assertEqual(self.charter_path().read_text(), charter)   # deleting a task never unticks its goal
        self.mirror()
        self.assertEqual(self.goal_rows(), {"2": ("Run the schema migration", "pending"), "3": ("Sweep", "pending"), "4": ("Ship the renderer", "completed")})

    def test_a_charter_tick_or_untick_reaches_the_task(self):
        self.write_charter(extra=self.GOALS)
        self.mirror()
        self.set_status("2", "in_progress"); self.set_status("3", "in_progress")
        p = self.charter_path(); p.write_text(p.read_text().replace('"[x] Ship', '"[ ] Ship').replace("[ ] Sweep", "[x] Sweep"))
        self.mirror()
        # An open goal leaves a task in progress where it is.
        self.assertEqual(self.goal_rows(), {"1": ("Ship the renderer", "pending"), "2": ("Run the schema migration", "in_progress"), "3": ("Sweep", "completed")})

    def test_a_goal_dropped_from_the_charter_takes_its_task(self):
        self.write_charter(extra=self.GOALS)
        self.mirror()
        p, d = self.charter_path(), self.task_list()
        p.write_text(p.read_text().replace("  - [ ] Sweep\n", ""))
        self.mirror()
        self.assertEqual(sorted(self.tasks()), ["1", "2"]); self.assertEqual((d / ".highwatermark").read_text(), "3")
        # A higher mark stands, and a new goal takes the id after it.
        (d / ".highwatermark").write_text("9")
        p.write_text(p.read_text().replace('"[x] Ship the renderer"', '"[ ] Retire it"'))
        self.mirror()
        self.assertEqual(self.goal_rows(), {"2": ("Run the schema migration", "pending"), "10": ("Retire it", "pending")})
        self.assertEqual((d / ".highwatermark").read_text(), "9")

    def test_completing_a_goal_task_ticks_the_charter(self):
        self.write_charter(extra=self.GOALS)
        self.mirror()
        p = self.charter_path(); original = p.read_text()
        self.set_status("2", "completed"); self.task_update("2")
        self.set_status("3", "completed"); self.task_update("3")
        self.assertEqual(p.read_text(), original.replace('"[ ] Run', '"[x] Run').replace("[ ] Sweep", "[x] Sweep"))   # quoting kept, nothing else moved
        # The file is the truth, not the tool's input.
        self.set_status("2", "pending"); self.task_update("2")
        self.set_status("1", "in_progress"); self.task_update("1")
        ticked = original.replace('"[x] Ship', '"[ ] Ship').replace("[ ] Sweep", "[x] Sweep")
        self.assertEqual(p.read_text(), ticked)
        # A task that is not this workstream's goal task ticks nothing.
        self.task_file("7", subject="Run the schema migration", status="completed"); self.task_update("7")
        self.task_file("8", status="completed", metadata={"workstream": "OTHER", "workstream_goal": "Run the schema migration"}); self.task_update("8")
        self.assertEqual(p.read_text(), ticked)

    def test_a_finished_charter_seeds_nothing_until_a_goal_reopens(self):
        # Claude Code clears a list whose every task is completed; seeding finished goals would loop against it.
        self.write_charter(extra='goals:\n  - "[x] Ship the renderer"\n  - [x] Sweep\n')
        turn = lambda: [self.mirror() for _ in range(3)]   # a start, a prompt and the turn's end
        turn(); self.assertEqual(self.tasks(), {})
        d = self.task_list(); d.mkdir(parents=True, exist_ok=True); (d / ".highwatermark").write_text("2")   # the list after Claude Code's reset
        turn(); self.assertEqual(sorted(p.name for p in d.iterdir()), [".highwatermark"])
        p = self.charter_path(); p.write_text(p.read_text().replace("[x] Sweep", "[ ] Sweep"))
        self.mirror()
        self.assertEqual(self.goal_rows(), {"3": ("Ship the renderer", "completed"), "4": ("Sweep", "pending")})
        # Finished again: existing tasks still follow the charter, and a dropped goal still takes its task.
        p.write_text(p.read_text().replace('  - "[x] Ship the renderer"\n', "").replace("[ ] Sweep", "[x] Sweep"))
        self.mirror()
        self.assertEqual(self.goal_rows(), {"4": ("Sweep", "completed")}); self.assertEqual((d / ".highwatermark").read_text(), "3")

    def test_the_task_list_id_from_the_environment_names_the_list(self):
        self.write_charter(extra=self.GOALS); self.env["CLAUDE_CODE_TASK_LIST_ID"] = "shared list/1"
        self.mirror()
        self.assertEqual(len(self.tasks("shared-list-1")), 3); self.assertEqual(self.tasks(), {})
        self.set_status("3", "completed", lid="shared-list-1"); self.task_update("3")
        self.assertIn("\n  - [x] Sweep\n", self.charter_path().read_text())

    def test_only_an_active_bound_session_gets_goal_tasks(self):
        self.write_charter(extra=self.GOALS); self.write_charter("OTHER", extra=self.GOALS)
        for name in ("PAYMENTS_API work: fix thing", "control", "loose"):   # worker, control, unbound
            (Path(self.home) / ".claude" / "sessions" / "4242.json").write_text(json.dumps({"pid": 4242, "sessionId": "s1", "name": name, "cwd": self.proj}))
            self.mirror()
            self.assertFalse((Path(self.home) / ".claude" / "tasks").exists(), name)
        # A worker finishing a task that carries a goal's metadata ticks nothing either.
        self.become_worker(); self.mirror()
        self.task_file("1", status="completed", metadata={"workstream": "PAYMENTS_API", "workstream_goal": "Sweep"})
        charter = self.charter_path().read_text(); self.task_update("1")
        self.assertEqual(self.charter_path().read_text(), charter)

    def test_a_failed_mirror_exits_non_zero_for_the_mod_to_log_and_writes_no_record(self):
        self.write_charter(extra=self.GOALS)
        (Path(self.home) / ".claude" / "tasks").write_text("a file where the task lists would go")
        p = subprocess.run([sys.executable, str(HOOK), "mirror", "s1"], capture_output=True, text=True, env=self.env)
        self.assertEqual((p.returncode, p.stdout), (1, "")); self.assertIn("NotADirectoryError", p.stderr.splitlines()[-1])
        self.assertFalse((self.board / "s1.json").exists())

    # --- the charter tool: `board.py write`, the one way a bound session writes its charter ---

    def write(self, sid="s1", env=None, **fields):
        """`board.py write <sid>` as the mod runs it, the fields on stdin: what it printed, on exit 0 whether it wrote or refused."""
        p = subprocess.run([sys.executable, str(HOOK), "write", sid], input=json.dumps(fields), capture_output=True, text=True, env=env or self.env)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    def frontmatter(self, key="PAYMENTS_API"):
        return re.match(r"---\n(.*?)\n---\n", self.charter_path(key).read_text(), re.S).group(1)

    def test_the_tool_sets_the_focus_on_one_line_where_it_stands_or_after_the_purpose(self):
        self.write_charter(extra=self.GOALS); p = self.charter_path(); before = p.read_text()
        self.assertEqual(self.write(focus="Migrating.\n  Next:\tthe sweep. "), {"ok": True, "key": "PAYMENTS_API", "summary": "focus set\n2 goals left"})
        self.assertEqual(p.read_text(), before.replace("focus: PROJ-1\n", 'focus: "Migrating. Next: the sweep."\n'))
        p.write_text(before.replace("focus: PROJ-1\n", ""))
        self.write(focus="Back.")
        self.assertEqual(p.read_text(), before.replace("focus: PROJ-1\n", "").replace("purpose: Rebuild the API.\n", 'purpose: Rebuild the API.\nfocus: "Back."\n'))
        p.write_text("---\nworkstream: PAYMENTS_API\n---\nHandoff.\n")   # no purpose either: last
        self.assertEqual(self.write(focus="Last.")["summary"], "focus set\nno goals")
        self.assertEqual(p.read_text(), '---\nworkstream: PAYMENTS_API\nfocus: "Last."\n---\nHandoff.\n')

    def test_the_tool_appends_a_record_entry_as_a_dated_bullet(self):
        self.write_charter(); p = self.charter_path(); before = p.read_text(); today = time.strftime("%m-%d")
        self.assertEqual(self.write(record="Pushed the fix.\n  - **PR:** #12\n\n")["summary"], "record entry added\nno goals")
        self.assertEqual(p.read_text(), before + f"- **{today}** Pushed the fix.\n  - **PR:** #12\n")
        self.write(record='- **10-05, Ian: "go".** Landed.')   # a date of its own, and the bullet the tool adds: neither doubled
        self.assertTrue(p.read_text().endswith('#12\n- **10-05, Ian: "go".** Landed.\n'), p.read_text())
        p.write_text(p.read_text() + "\n\n\n"); self.write(record="Third.")
        self.assertTrue(p.read_text().endswith(f"Landed.\n- **{today}** Third.\n"), p.read_text())   # one newline at the end, and no gap
        p.write_text("---\nworkstream: PAYMENTS_API\n---\n"); self.write(record="First.")   # an empty record
        self.assertEqual(p.read_text(), f"---\nworkstream: PAYMENTS_API\n---\n\n- **{today}** First.\n")

    def test_the_tool_adds_each_new_goal_once_and_mirrors_its_task(self):
        self.write_charter(extra=self.GOALS); p = self.charter_path(); before = p.read_text(); self.mirror()
        out = self.write(add_goals=["Retire  the\nold path", "Sweep", "Ship the renderer", "Retire the old path"])
        self.assertEqual(out["summary"], "goal added: Retire the old path\ngoal already listed: Sweep\ngoal already listed: Ship the renderer\n"
                                         "goal already listed: Retire the old path\n3 goals left")
        self.assertEqual(p.read_text(), before.replace("  - [ ] Sweep\n", '  - [ ] Sweep\n  - "[ ] Retire the old path"\n'))
        self.assertEqual(self.goal_rows()["4"], ("Retire the old path", "pending"))   # its task, at once
        p.write_text("---\nworkstream: PAYMENTS_API\nfocus: x\n---\nHandoff.\n")   # no goals: list yet
        self.write(add_goals=["First"])
        self.assertEqual(p.read_text(), '---\nworkstream: PAYMENTS_API\nfocus: x\ngoals:\n  - "[ ] First"\n---\nHandoff.\n')

    def test_the_tool_sets_a_block_stamped_in_utc_and_clears_it(self):
        import calendar
        self.write_charter(); p = self.charter_path(); before = p.read_text()
        env = {**self.env, "TZ": "Asia/Kolkata"}   # a stamp in local time would read hours off
        self.assertEqual(self.write(env=env, block="Dana's review")["summary"], "block set: Dana's review\nno goals")
        m = re.search(r'\nfocus: PROJ-1\nblocked: "Dana\'s review"\nblocked_since: (\S+)\nstatus: active\n', p.read_text())
        self.assertTrue(m, p.read_text()); self.assertLess(abs(calendar.timegm(time.strptime(m.group(1), "%Y-%m-%dT%H:%M:%SZ")) - time.time()), 120)
        self.write(block="PR #12")   # set again: in place
        self.assertRegex(p.read_text(), r'\nfocus: PROJ-1\nblocked: "PR #12"\nblocked_since: \S+\nstatus: active\n')
        self.assertEqual(self.write(clear_block=True)["summary"], "block cleared\nno goals"); self.assertEqual(p.read_text(), before)
        self.assertEqual(self.write(clear_block=True)["summary"], "no block to clear\nno goals"); self.assertEqual(p.read_text(), before)
        self.assertEqual(self.write(block="x", clear_block=True), {"error": "block and clear_block cannot go in one call"}); self.assertEqual(p.read_text(), before)

    def test_quotes_backslashes_colons_and_hashes_read_back_through_the_charters_readers(self):
        b, odd = self.module(), 'it\'s "50%" #1: ok \\ done'
        self.write_charter(extra=self.GOALS); self.write(focus=odd, add_goals=[f"say {odd}"], block=odd); fm = self.frontmatter()
        self.assertEqual(b.scalar(fm, "focus"), odd)
        self.assertEqual(b.goals(fm)[-1][:2], (False, f"say {odd}"))
        self.assertTrue(b.block_of(fm).startswith(odd + " · "), b.block_of(fm))
        self.assertIn(("say " + odd, "pending"), self.goal_rows().values())   # its task's subject
        self.assertIn(f"{odd} · 0m  {odd}", self.render("--no-color"))   # the block, then the focus, as written

    def test_the_tool_refuses_a_session_bound_to_no_charter_and_a_call_it_cannot_make(self):
        self.write_charter(); p = self.charter_path(); before = p.read_text(); reg = Path(self.home) / ".claude" / "sessions" / "4242.json"
        for name in ("control", "PAYMENTS_API work: fix thing", None, "NOCHARTER"):
            reg.write_text(json.dumps({"pid": 4242, "sessionId": "s1", "name": name, "cwd": self.proj}))
            self.assertEqual(self.write(focus="x"), {"error": "this session is not bound to a workstream"}, name)
        self.assertEqual(self.write("s-unknown", focus="x"), {"error": "this session is not bound to a workstream"})   # no registry file
        reg.write_text(json.dumps({"pid": 4242, "sessionId": "s1", "name": "PAYMENTS_API", "cwd": self.proj}))
        nothing = "nothing to write: give focus, record, add_goals, block or clear_block"
        for fields, why in (({}, nothing), ({"focus": None, "clear_block": False, "note": "mine"}, nothing), ({"focus": " \n"}, "focus must be text"),
                            ({"record": 3}, "record must be text"), ({"add_goals": "x"}, "add_goals must be a list of goal texts"),
                            ({"add_goals": []}, "add_goals must be a list of goal texts"), ({"add_goals": ["ok", " "]}, "add_goals must be a list of goal texts"),
                            ({"clear_block": "yes"}, "clear_block must be true"), ({"focus": "fine", "block": ""}, "block must be text")):
            self.assertEqual(self.write(**fields), {"error": why}, fields)
        self.assertEqual(p.read_text(), before)

    def test_no_write_touches_the_note_or_a_goals_checkbox(self):
        self.write_charter(extra=self.GOALS); self.note("PAYMENTS_API", self.NOTE); b = self.module()   # the note right after focus:, where Ian's prompt puts it
        for fields in ({"focus": "note: x"}, {"record": 'note: "y"'}, {"add_goals": ["[x] note"]}, {"block": "note"}, {"clear_block": True}):
            self.write(**fields)
            fm = self.frontmatter()
            self.assertEqual(([l + "\n" for l in fm.split("\n") if l.startswith("note:")], [d for d, *_ in b.goals(fm)][:3]), ([self.NOTE_LINE], [True, False, False]), fields)

    def test_every_charter_write_holds_the_lock_across_its_read_and_its_write(self):
        # The test holds the lock and changes the charter while each waits: each reads after it, so both changes stand.
        import fcntl
        self.write_charter(extra=self.GOALS); p = self.charter_path(); self.mirror(); self.set_status("3", "completed"); self.panes("kept")
        runs = (("write", ["write", "s1"], '{"focus": "new"}', 'focus: "new"'), ("tick", ["tick", "s1", "3"], "", "  - [x] Sweep"),
                ("save-note", ["save-note", "PAYMENTS_API", "%9"], "", 'note: "kept"'))
        for name, argv, stdin, theirs in runs:
            with open(p.parent / ".lock", "a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                run = subprocess.Popen([sys.executable, str(HOOK), *argv], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, text=True, env=self.env)
                run.stdin.write(stdin); run.stdin.close(); time.sleep(0.5)
                self.assertIsNone(run.poll(), f"{name} did not wait for the lock")
                p.write_text(re.sub(r"(?m)^purpose: .*$", f"purpose: {name} waited.", p.read_text()))
            self.assertEqual(run.wait(timeout=10), 0, name)
            text = p.read_text(); self.assertIn(theirs, text, name); self.assertIn(f"purpose: {name} waited.", text, name)

    def test_the_plugin_declares_no_settings_hooks_and_loads_the_mod(self):
        root = HOOK.parents[1]
        self.assertNotIn("hooks", json.loads((root / ".claude-plugin" / "plugin.json").read_text()))
        self.assertEqual(json.loads((root / "hooks" / "hooks.json").read_text()), {"modules": ["./mod.ts"]})

if __name__ == "__main__":
    unittest.main()
