# Busy-Green Upgrade and Stale-Active Demotion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** White idle lights turn green while daemon-hosted work runs, and sessions stuck green after a missed turn-end event demote to white after 30 minutes — per `docs/superpowers/specs/2026-07-26-busy-shell-lights-design.md`.

**Architecture:** All behavior lands in `scripts/tmux_render.py` (render-time inference; no hook changes). `CLIENT_COMMAND_RE` moves to `scripts/utils.py` so both scripts share it. The Claude daemon job record (`~/.claude/jobs/<short>/state.json`) supplies both signals: existence = alive (demotion exemption), `working`/`inFlight.tasks>0` = busy (green upgrade).

**Tech Stack:** Python 3 stdlib; `unittest` via `python3 -m unittest tests.test_lights`. Existing fake-tmux + fake-HOME harness.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-26-busy-shell-lights-design.md`
- Unviewed yellow always wins; only white (waiting+acknowledged) upgrades to green.
- No timer may ever touch a session whose pane command matches `CLIENT_COMMAND_RE`; no new reap timers anywhere.
- Jobs record path: `$HOME/.claude/jobs/<session_id[:8]>/state.json`; must verify `sessionId` equals the full session id; malformed → treated as absent.
- All commits end with:
  `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`
  `Claude-Session: https://claude.ai/code/session_01KpgY8kANwqjPcPnn5NfoaR`
- Repo root: `/Users/mpercy/src/claude-tmux-lights`; run everything from there. Suite currently: 20 tests passing.

---

### Task 1: Share `CLIENT_COMMAND_RE` via utils, extend fake tmux, and add `get_pane_commands`

**Files:**
- Modify: `scripts/utils.py` (add `re` import and the constant)
- Modify: `scripts/hook_handler.py` (delete its constant definition; import from utils)
- Modify: `scripts/tmux_render.py` (replace `get_live_pane_ids` with `get_pane_commands`)
- Test: `tests/test_lights.py`

**Interfaces:**
- Produces: `utils.CLIENT_COMMAND_RE` (compiled regex, exactly `r"^(claude|codex)([-.]|$)|^\d+\.\d+\.\d+$"`); `tmux_render.get_pane_commands()` returning `{pane_id: command}` or `None` when the pane list is unavailable or empty. Task 2/3 consume both.
- Consumes: existing `cleanup_orphans` (its `live_panes` set becomes the dict's keys).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_lights.py`, inside `TestOrphanCleanup`:

```python
    def test_cleanup_uses_pane_commands_map(self):
        # get_pane_commands returns {pane_id: command}; pane-death reaping
        # must keep working through the new structure
        self.write_state("dead", tmux_pane="%99", tmux_window="@9")
        self.write_state("alive", tmux_pane="%12", tmux_window="@4")
        self.run_render("@1", active=True)
        self.assertFalse(os.path.exists(self.state_path("dead")))
        self.assertTrue(os.path.exists(self.state_path("alive")))
```

- [ ] **Step 2: Run it — it PASSES already (behavioral no-op guard); the RED step for this task is import-level**

Run: `python3 -m unittest tests.test_lights` → 21 tests OK. This task is a refactor guarded by the whole suite; the new test locks the reap behavior across the restructure.

- [ ] **Step 3: Implement the refactor**

In `scripts/utils.py`, after the imports (`import re` joins them):

```python
CLIENT_COMMAND_RE = re.compile(r"^(claude|codex)([-.]|$)|^\d+\.\d+\.\d+$")
```

In `scripts/hook_handler.py`: remove the `CLIENT_COMMAND_RE = re.compile(...)` line (and the now-unused `import re` if nothing else uses it), and extend the utils import to `from utils import atomic_write_json, get_state_dir, read_json_safe, CLIENT_COMMAND_RE`.

In `scripts/tmux_render.py`: rename `get_live_pane_ids` to `get_pane_commands`; change the format string to `"#{pane_id}\t#{pane_current_command}"` and build a dict:

```python
def get_pane_commands():
    """Map live pane IDs to their foreground commands.

    Returns None when the list can't be read or comes back empty (we render
    from inside a pane, so an empty list can't be real) — callers treat None
    as "couldn't check" and skip pane-based decisions.
    """
    try:
        result = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", "#{pane_id}\t#{pane_current_command}"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            panes = {}
            for line in result.stdout.splitlines():
                pane_id, _, command = line.partition("\t")
                if pane_id:
                    panes[pane_id] = command
            return panes or None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None
```

In `cleanup_orphans`, replace `live_panes = get_live_pane_ids()` with `pane_commands = get_pane_commands()` and adjust the two uses: `if pane_commands is not None and pane:` and `if pane not in pane_commands:`.

- [ ] **Step 4: Run the full suite**

Run: `python3 -m unittest tests.test_lights`
Expected: OK, 21 tests.

- [ ] **Step 5: Commit**

```bash
git add scripts/utils.py scripts/hook_handler.py scripts/tmux_render.py tests/test_lights.py
git commit -m "Share client regex via utils and map pane IDs to commands"
```

---

### Task 2: Busy-green upgrade from the daemon job record

**Files:**
- Modify: `scripts/tmux_render.py` (new helper + `render_indicator` change; `main` passes session busy-ness)
- Test: `tests/test_lights.py`

**Interfaces:**
- Consumes: nothing new from Task 1 (independent of pane commands).
- Produces: `daemon_job_record(session_id)` → parsed dict or `None`; `job_is_busy(record)` → bool. Task 3 reuses `daemon_job_record` for the alive-exemption.

- [ ] **Step 1: Write the failing tests**

Add a new class to `tests/test_lights.py` (after `TestOrphanCleanup`). Include a helper for fabricating job records:

```python
class TestBusyGreen(LightsTestCase):
    """A white (waiting+acknowledged) session with daemon-hosted work
    running renders green; yellow (unviewed) always wins."""

    def write_job(self, session_id, state="working", tasks=1, real_id=None):
        jobs = os.path.join(self.home, ".claude", "jobs", session_id[:8])
        os.makedirs(jobs, exist_ok=True)
        with open(os.path.join(jobs, "state.json"), "w") as f:
            json.dump({"sessionId": real_id or session_id, "state": state,
                       "inFlight": {"tasks": tasks, "queued": 0, "kinds": []}}, f)

    def acknowledge(self, session_id, state):
        viewed = {session_id: state["state_changed_ts"] + 1}
        with open(os.path.join(self.state_dir, "viewed.json"), "w") as f:
            json.dump(viewed, f)

    def test_white_session_with_busy_job_renders_green(self):
        state = self.write_state("busy1234-0000-0000-0000-000000000000")
        self.acknowledge("busy1234-0000-0000-0000-000000000000", state)
        self.write_job("busy1234-0000-0000-0000-000000000000")
        out = self.run_render("@4", active=False)
        self.assertIn("colour46", out)   # active green, not viewed grey

    def test_white_session_with_idle_job_stays_white(self):
        state = self.write_state("idle1234-0000-0000-0000-000000000000")
        self.acknowledge("idle1234-0000-0000-0000-000000000000", state)
        self.write_job("idle1234-0000-0000-0000-000000000000",
                       state="done", tasks=0)
        out = self.run_render("@4", active=False)
        self.assertIn("colour252", out)  # viewed grey

    def test_white_session_without_job_stays_white(self):
        state = self.write_state("nojo1234-0000-0000-0000-000000000000")
        self.acknowledge("nojo1234-0000-0000-0000-000000000000", state)
        out = self.run_render("@4", active=False)
        self.assertIn("colour252", out)

    def test_unviewed_session_with_busy_job_stays_yellow(self):
        self.write_state("yell1234-0000-0000-0000-000000000000")
        self.write_job("yell1234-0000-0000-0000-000000000000")
        out = self.run_render("@4", active=False)
        self.assertIn("colour226", out)  # waiting yellow wins

    def test_job_with_mismatched_session_id_is_ignored(self):
        state = self.write_state("mism1234-0000-0000-0000-000000000000")
        self.acknowledge("mism1234-0000-0000-0000-000000000000", state)
        self.write_job("mism1234-0000-0000-0000-000000000000",
                       real_id="other567-0000-0000-0000-000000000000")
        out = self.run_render("@4", active=False)
        self.assertIn("colour252", out)
```

- [ ] **Step 2: Run to verify RED**

Run: `python3 -m unittest tests.test_lights.TestBusyGreen -v`
Expected: `test_white_session_with_busy_job_renders_green` FAILS (`colour46` absent — renders grey); the other four PASS (they assert current behavior and guard the new code). RED is carried by the busy-green test.

- [ ] **Step 3: Implement**

In `scripts/tmux_render.py`, after `load_config`:

```python
def daemon_job_record(session_id):
    """Return the Claude daemon job record for a session, or None.

    The record (~/.claude/jobs/<short>/state.json) exists only for
    daemon-hosted sessions; its presence proves the session is alive even
    when no tmux pane hosts a client for it.
    """
    path = os.path.join(os.path.expanduser("~"), ".claude", "jobs",
                        session_id[:8], "state.json")
    record = read_json_safe(path)
    if record and record.get("sessionId") == session_id:
        return record
    return None


def job_is_busy(record):
    """A job record counts as busy while work is actually running."""
    if not record:
        return False
    in_flight = record.get("inFlight") or {}
    return record.get("state") == "working" or (in_flight.get("tasks") or 0) > 0
```

In `render_indicator`, replace the viewed/acknowledged branch:

```python
        viewed_ts = viewed_json.get(session_id, 0)
        if viewed_ts >= state_changed_ts:
            # Viewed/acknowledged — but show green if daemon-hosted work
            # is still running (turn ended, background task grinding on)
            if job_is_busy(daemon_job_record(session_id)):
                icon = config["active_icon"]
                color = config["active_color"]
            else:
                icon = config["viewed_icon"]
                color = config["viewed_color"]
            return f"#[fg={color}]{icon}#[fg=default]"
```

- [ ] **Step 4: Run the full suite**

Run: `python3 -m unittest tests.test_lights`
Expected: OK, 26 tests.

- [ ] **Step 5: Commit**

```bash
git add scripts/tmux_render.py tests/test_lights.py
git commit -m "Show green for acknowledged sessions with running daemon work"
```

---

### Task 3: Stale-active demotion on non-client panes

**Files:**
- Modify: `scripts/tmux_render.py` (demotion inside `cleanup_orphans`)
- Test: `tests/test_lights.py`

**Interfaces:**
- Consumes: `get_pane_commands()` dict from Task 1; `daemon_job_record` from Task 2; `utils.CLIENT_COMMAND_RE`, `utils.atomic_write_json` (already imported in tmux_render), `mark-viewed` semantics via direct viewed.json update in the same pass.
- Produces: demotion behavior; no new public functions.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_lights.py` (new class; reuse `TestBusyGreen.write_job` by inheriting from it):

```python
class TestStaleActiveDemotion(TestBusyGreen):
    """An active session whose events went missing (laptop-close) demotes
    to acknowledged-waiting after 30 min — but only on non-client panes."""

    def stale_active(self, session_id, pane="%21", window="@4", age=1900):
        old = time.time() - age
        return self.write_state(session_id, state="active",
                                state_changed_ts=old, waiting_since_ts=None,
                                tmux_pane=pane, tmux_window=window)

    def test_stale_active_on_shell_pane_demotes_to_white(self):
        sid = "stal1234-0000-0000-0000-000000000000"
        self.stale_active(sid, pane="%21")   # %21 runs zsh in PANES
        self.run_render("@9", active=True)   # cleanup runs from active window
        state = self.read_state(sid)
        self.assertEqual(state["state"], "waiting")
        self.assertIsNotNone(state["waiting_since_ts"])
        out = self.run_render("@4", active=False)
        self.assertIn("colour252", out)      # acknowledged white, not yellow

    def test_stale_active_on_client_pane_is_untouched(self):
        sid = "clie1234-0000-0000-0000-000000000000"
        self.stale_active(sid, pane="%12")   # %12 runs "2.1.220" (client)
        self.run_render("@9", active=True)
        self.assertEqual(self.read_state(sid)["state"], "active")

    def test_stale_active_with_live_job_record_is_untouched(self):
        sid = "aliv1234-0000-0000-0000-000000000000"
        self.stale_active(sid, pane="%21")
        self.write_job(sid, state="done", tasks=0)  # exists = alive
        self.run_render("@9", active=True)
        self.assertEqual(self.read_state(sid)["state"], "active")

    def test_fresh_active_on_shell_pane_is_untouched(self):
        sid = "fres1234-0000-0000-0000-000000000000"
        self.stale_active(sid, pane="%21", age=60)
        self.run_render("@9", active=True)
        self.assertEqual(self.read_state(sid)["state"], "active")
```

- [ ] **Step 2: Run to verify RED**

Run: `python3 -m unittest tests.test_lights.TestStaleActiveDemotion -v`
Expected: `test_stale_active_on_shell_pane_demotes_to_white` FAILS (state stays `active`); the three guards PASS.

- [ ] **Step 3: Implement**

In `scripts/tmux_render.py` `cleanup_orphans`, the demotion slots into the existing per-session loop. After the pane-death check (`if pane not in pane_commands: unlink; continue`), add — still inside the `if pane_commands is not None and pane:` branch:

```python
            # Demote stale-active sessions on non-client panes: the pane
            # isn't visibly hosting a client, no events have arrived for
            # 30+ minutes, and no daemon job record proves it alive — the
            # turn-end event was likely missed (e.g. laptop closed).
            # Green would lie forever; acknowledged-white is the truth.
            if (session.get("state") == "active"
                    and now - session.get("state_changed_ts", 0) > threshold
                    and not CLIENT_COMMAND_RE.match(pane_commands.get(pane, ""))
                    and daemon_job_record(session_id) is None):
                demoted = dict(session)
                demoted["state"] = "waiting"
                demoted["state_changed_ts"] = now
                demoted["waiting_since_ts"] = now
                try:
                    atomic_write_json(
                        os.path.join(state_dir, f"{session_id}.json"), demoted)
                except OSError:
                    pass
                demoted_ids.append((session_id, now))
            continue
```

`cleanup_orphans` gains a `demoted_ids = []` accumulator and returns it; `main`'s two call sites mark them viewed (acknowledged) by updating viewed.json once:

```python
    demoted = cleanup_orphans(state_dir, all_sessions, config["orphan_threshold_seconds"])
    if demoted:
        viewed_json = read_json_safe(viewed_path) or {}
        for sid, ts in demoted:
            viewed_json[sid] = ts
        atomic_write_json(viewed_path, viewed_json)
```

(For the early-exit call site — active window with no sessions of its own — define `viewed_path` locally the same way; keep both sites small and identical in behavior. `CLIENT_COMMAND_RE` joins the existing `from utils import ...` line.)

- [ ] **Step 4: Run the full suite**

Run: `python3 -m unittest tests.test_lights`
Expected: OK, 30 tests.

- [ ] **Step 5: Commit**

```bash
git add scripts/tmux_render.py tests/test_lights.py
git commit -m "Demote stale-active sessions on non-client panes to acknowledged white"
```

---

## Self-Review

- **Spec coverage:** busy-green → Task 2 (tests 1–5 of spec); demotion incl. client-pane/jobs/freshness exemptions → Task 3 (tests 6–9); shared regex + pane-command map → Task 1; spec test 10 (existing reap/fallback) → Task 1's guard test plus untouched existing tests. No-new-reap-timers honored: no task adds one.
- **Placeholder scan:** all steps carry complete code; no TBDs.
- **Type consistency:** `get_pane_commands()` dict consumed as keys in Task 1 and values in Task 3; `daemon_job_record`/`job_is_busy` defined in Task 2, reused by name in Task 3; `write_job`/`acknowledge` helpers defined in Task 2's class and inherited by Task 3's. Test counts: 20 + 1 + 5 + 4 = 30. ✓
