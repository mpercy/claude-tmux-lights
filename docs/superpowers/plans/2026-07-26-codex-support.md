# Codex CLI Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Light up tmux window tabs for Codex CLI sessions using the existing state-file + renderer pipeline, packaged as a Codex plugin in this repo.

**Architecture:** Codex 0.145+ clones Claude Code's hook system (same event names, same stdin JSON, same hooks.json shape, `CLAUDE_PLUGIN_ROOT` compat alias). We add a Codex-flavored hook manifest and plugin/marketplace manifests beside the existing Claude ones; both CLIs invoke the same `scripts/hook_handler.py`. The renderer and state files need no changes.

**Tech Stack:** Python 3 stdlib only. Tests via `unittest` (`python3 -m unittest tests.test_lights`). No new dependencies.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-26-codex-support-design.md`
- Codex hooks are synchronous — the Codex manifest must contain **no `async` flags**.
- Codex has **no `Notification` event**; use `PermissionRequest`. Claude's `hooks/hooks.json` must remain untouched.
- Codex `SessionEnd` hooks are capped at ~1s (max 3s) — omit `timeout` on the Codex `SessionEnd` entry; do not raise it.
- All commits end with:
  `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`
  `Claude-Session: https://claude.ai/code/session_01KpgY8kANwqjPcPnn5NfoaR`
- Repo root: `/Users/mpercy/src/claude-tmux-lights`. Run all commands from there.

---

### Task 1: Handler support for Codex (`PermissionRequest` + client recognition)

**Files:**
- Modify: `scripts/hook_handler.py` (the `CLAUDE_COMMAND_RE` constant near line 37; new event branch in `handle_event`, after the `Notification` block and before `PostToolUse`)
- Test: `tests/test_lights.py`

**Interfaces:**
- Consumes: existing `handle_event(event, state_dir)` dispatch and the `find_pane_by_cwd(cwd)` fallback; the test harness classes `LightsTestCase` (helpers: `run_hook(event, tmux_pane=None)`, `write_state(session_id, **overrides)`, `read_state(session_id)`, `state_path(session_id)`, `set_panes(panes)`) already in `tests/test_lights.py`.
- Produces: handler that treats `hook_event_name == "PermissionRequest"` exactly like Claude's `Notification`/`permission_prompt` (state → `waiting`, self-healing when the file is missing), and a renamed regex `CLIENT_COMMAND_RE` that matches `codex` / `codex-*` process names. Task 2's manifest registers this event; Task 3 exercises it live.

- [ ] **Step 1: Write three failing tests**

Add to `tests/test_lights.py`. The first two go in `TestSelfHealingStateFiles`, the third in `TestDaemonSessionPaneFallback`:

```python
    def test_permission_request_sets_waiting(self):
        self.write_state("s1", state="active", waiting_since_ts=None)
        self.run_hook({"hook_event_name": "PermissionRequest",
                       "session_id": "s1", "cwd": "/proj/game"},
                      tmux_pane="%12")
        state = self.read_state("s1")
        self.assertEqual(state["state"], "waiting")

    def test_permission_request_recreates_missing_state_file(self):
        self.run_hook({"hook_event_name": "PermissionRequest",
                       "session_id": "s1", "cwd": "/proj/game"},
                      tmux_pane="%12")
        state = self.read_state("s1")
        self.assertEqual(state["state"], "waiting")
        self.assertEqual(state["tmux_window"], "@4")
```

```python
    def test_without_tmux_pane_codex_pane_beats_shell_in_same_cwd(self):
        self.set_panes([
            {"pane_id": "%30", "window_id": "@6", "command": "codex-aarch64-a",
             "path": "/proj/codex"},
            {"pane_id": "%31", "window_id": "@6", "command": "zsh",
             "path": "/proj/codex"},
        ])
        self.run_hook({"hook_event_name": "Stop", "session_id": "cx1",
                       "cwd": "/proj/codex"})
        state = self.read_state("cx1")
        self.assertEqual(state["tmux_pane"], "%30")
        self.assertEqual(state["tmux_window"], "@6")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest tests.test_lights -v 2>&1 | grep -E 'permission_request|codex'`
Expected: `test_permission_request_sets_waiting` ... FAIL or ERROR (state stays `active` — unknown event falls through to `return None`); `test_permission_request_recreates_missing_state_file` ... ERROR (`FileNotFoundError`); `test_without_tmux_pane_codex_pane_beats_shell_in_same_cwd` ... ERROR (`FileNotFoundError` — `codex-aarch64-a` isn't recognized, two candidates → ambiguous → no write).

- [ ] **Step 3: Implement**

In `scripts/hook_handler.py`, replace the regex constant (keep the surrounding blank lines):

```python
CLIENT_COMMAND_RE = re.compile(r"^(claude|codex)([-.]|$)|^\d+\.\d+\.\d+$")
```

Update its single use inside `find_pane_by_cwd` (the `claude_like` list comprehension) to `CLIENT_COMMAND_RE.match(c[2])`, and update the docstring's "looks like a Claude client" sentence to "looks like a Claude or Codex client". Note the old class contained a stray `\b` (backspace escape inside a character class) — the replacement drops it deliberately.

Then add this branch in `handle_event`, after the entire `Notification` block (after its final `return None`) and before the `PostToolUse` branch:

```python
    if hook_name == "PermissionRequest":
        # Codex CLI's equivalent of Claude's permission_prompt notification:
        # the session is blocked waiting for the user to approve a command.
        # Same semantics — go yellow (waiting), self-heal if the file is
        # missing; PostToolUse flips back to active when work resumes.
        existing = read_json_safe(state_path)
        window_id = (existing or {}).get("tmux_window", "") or get_tmux_window_id(tmux_pane)
        state = {
            "session_id": session_id,
            "state": "waiting",
            "state_changed_ts": now,
            "waiting_since_ts": now,
            "cwd": event.get("cwd", (existing or {}).get("cwd", "")),
            "tmux_pane": tmux_pane,
            "tmux_window": window_id,
        }
        atomic_write_json(state_path, state)
        tmux_refresh()
        return None
```

- [ ] **Step 4: Run the full suite to verify green**

Run: `python3 -m unittest tests.test_lights`
Expected: `OK`, 18 tests.

- [ ] **Step 5: Commit**

```bash
git add scripts/hook_handler.py tests/test_lights.py
git commit -m "Handle Codex PermissionRequest and recognize codex panes in cwd fallback"
```

---

### Task 2: Codex plugin packaging (manifests + README)

**Files:**
- Create: `codex-hooks/hooks.json`
- Create: `.codex-plugin/plugin.json`
- Create: `.agents/plugins/marketplace.json`
- Modify: `README.md` (add a "Codex CLI support" section after the existing installation section)
- Test: `tests/test_lights.py`

**Interfaces:**
- Consumes: `scripts/hook_handler.py` from Task 1 (must handle every event the manifest registers).
- Produces: an installable Codex plugin — marketplace name `claude-tmux-lights`, plugin name `claude-tmux-lights`, hook manifest path `codex-hooks/hooks.json` declared via the plugin manifest's `"hooks"` field. Task 3 installs exactly these names.

- [ ] **Step 1: Write the failing manifest-sanity test**

Add to `tests/test_lights.py` as a new top-level class (plain `unittest.TestCase`, not `LightsTestCase` — no tmux/state fixtures needed):

```python
CODEX_EVENTS = {
    "SessionStart", "SessionEnd", "SubagentStart", "SubagentStop",
    "PreToolUse", "PostToolUse", "PermissionRequest",
    "PreCompact", "PostCompact", "UserPromptSubmit", "Stop",
}


class TestCodexManifests(unittest.TestCase):
    """The Codex hook manifest must only use constructs Codex supports:
    known events, sync command hooks, no async flags."""

    def test_codex_hooks_manifest_is_codex_compatible(self):
        with open(os.path.join(REPO_ROOT, "codex-hooks", "hooks.json")) as f:
            manifest = json.load(f)
        self.assertTrue(manifest["hooks"], "no events registered")
        for event, groups in manifest["hooks"].items():
            self.assertIn(event, CODEX_EVENTS)
            for group in groups:
                for hook in group["hooks"]:
                    self.assertEqual(hook["type"], "command")
                    self.assertNotIn("async", hook)
                    self.assertIn("hook_handler.py", hook["command"])
        # SessionEnd must not override Codex's ~1s cap
        for group in manifest["hooks"].get("SessionEnd", []):
            for hook in group["hooks"]:
                self.assertNotIn("timeout", hook)

    def test_codex_plugin_manifest_points_at_hooks(self):
        with open(os.path.join(REPO_ROOT, ".codex-plugin", "plugin.json")) as f:
            plugin = json.load(f)
        self.assertEqual(plugin["name"], "claude-tmux-lights")
        hooks_path = os.path.join(REPO_ROOT, plugin["hooks"])
        self.assertTrue(os.path.isfile(hooks_path), hooks_path)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest tests.test_lights.TestCodexManifests -v`
Expected: both ERROR with `FileNotFoundError` (manifests don't exist yet).

- [ ] **Step 3: Create `codex-hooks/hooks.json`**

```json
{
  "description": "tmux status line indicators for Codex CLI sessions",
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"${CLAUDE_PLUGIN_ROOT}/scripts/hook_handler.py\"",
            "timeout": 10
          }
        ]
      }
    ],
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"${CLAUDE_PLUGIN_ROOT}/scripts/hook_handler.py\"",
            "timeout": 5
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"${CLAUDE_PLUGIN_ROOT}/scripts/hook_handler.py\"",
            "timeout": 5
          }
        ]
      }
    ],
    "PermissionRequest": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"${CLAUDE_PLUGIN_ROOT}/scripts/hook_handler.py\"",
            "timeout": 5
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"${CLAUDE_PLUGIN_ROOT}/scripts/hook_handler.py\"",
            "timeout": 5
          }
        ]
      }
    ],
    "SessionEnd": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"${CLAUDE_PLUGIN_ROOT}/scripts/hook_handler.py\""
          }
        ]
      }
    ]
  }
}
```

(`${CLAUDE_PLUGIN_ROOT}` is Codex's documented compatibility alias for `PLUGIN_ROOT`; using it keeps the command strings identical to the Claude manifest's.)

- [ ] **Step 4: Create `.codex-plugin/plugin.json`**

```json
{
  "name": "claude-tmux-lights",
  "version": "0.1.0",
  "description": "Show Claude Code and Codex session status indicators in tmux window tabs",
  "hooks": "./codex-hooks/hooks.json"
}
```

- [ ] **Step 5: Create `.agents/plugins/marketplace.json`**

```json
{
  "name": "claude-tmux-lights",
  "interface": {
    "displayName": "claude-tmux-lights"
  },
  "plugins": [
    {
      "name": "claude-tmux-lights",
      "source": {
        "source": "local",
        "path": "./"
      },
      "description": "Show Claude Code and Codex session status indicators in tmux window tabs"
    }
  ]
}
```

- [ ] **Step 6: Run the manifest tests to verify green**

Run: `python3 -m unittest tests.test_lights -v 2>&1 | tail -5`
Expected: `OK`, 20 tests.

- [ ] **Step 7: Add the README section**

Append after the existing installation instructions in `README.md`:

```markdown
## Codex CLI support

Codex (0.145+) uses the same hook architecture as Claude Code, so the same
plugin lights up Codex tabs. Install:

```bash
codex plugin marketplace add ~/src/claude-tmux-lights
codex plugin add claude-tmux-lights@claude-tmux-lights
```

Then run `/hooks` once inside a Codex session to trust the hook commands
(Codex records trust against each command's hash; re-approve after changing
the hook manifest). Lights behave identically to Claude sessions, with one
addition: Codex's `PermissionRequest` event turns the light yellow while a
command waits for approval. Codex has no idle-prompt event, so the
interrupt-recovery transition is Claude-only.
```

- [ ] **Step 8: Commit**

```bash
git add codex-hooks/ .codex-plugin/ .agents/ README.md tests/test_lights.py
git commit -m "Package as a Codex plugin: manifests, hook config, README"
```

---

### Task 3: Install, probe the unknowns, verify live

This task resolves the spec's "empirical unknowns" against the real Codex install. It has user-interaction checkpoints — sessions can only be trusted from inside Codex's TUI.

**Files:**
- Modify (only if probes force it): `codex-hooks/hooks.json`, `.codex-plugin/plugin.json`, `.agents/plugins/marketplace.json`
- No new source files. Findings go in the final commit message and the user-facing summary.

**Interfaces:**
- Consumes: the installable plugin from Task 2 (marketplace `claude-tmux-lights`, plugin `claude-tmux-lights@claude-tmux-lights`).
- Produces: a working install on this machine; documented answers to: in-place vs copied install, `"hooks"` manifest field honored, `TMUX_PANE` passthrough.

- [ ] **Step 1: Register the marketplace and install the plugin**

```bash
codex plugin marketplace add /Users/mpercy/src/claude-tmux-lights
codex plugin list 2>&1 | grep -A3 claude-tmux-lights
codex plugin add claude-tmux-lights@claude-tmux-lights
```

Expected: plugin listed as `installed, enabled`. If `marketplace add` rejects the manifest, compare against the bundled schema at `~/.codex/.tmp/bundled-marketplaces/openai-bundled/.agents/plugins/marketplace.json` and adjust `.agents/plugins/marketplace.json` to match (commit the fix with message `Fix Codex marketplace manifest schema`).

- [ ] **Step 2: Probe — in-place or copied?**

```bash
codex plugin list 2>&1 | grep claude-tmux-lights
```

Inspect the PATH column: if it points into `/Users/mpercy/src/claude-tmux-lights`, installs run in-place (repo edits live immediately — same as the Claude side). If it points into a cache dir, note that `codex plugin add` must be re-run after repo changes. Record which for the final summary and memory.

- [ ] **Step 3: Probe — do hooks fire with `TMUX_PANE`? (headless, bypasses trust)**

Run from a tmux pane (this session qualifies — `TMUX_PANE` is set):

```bash
cd /private/tmp/claude-501/-Users-mpercy-src-claude-tmux-lights/2acf6168-8e6a-4192-98de-67294165e6a7/scratchpad
rm -f probe_states.txt
( for i in $(seq 1 40); do ls ~/.local/state/claude-tmux-lights/ >> probe_states.txt; sleep 0.5; done ) &
codex exec --dangerously-bypass-hook-trust "Reply with exactly: ok" 2>&1 | tail -3
wait
sort -u probe_states.txt
```

Expected: `sort -u` output includes a new `<codex-session-uuid>.json` during the run (SessionStart/Stop wrote it) that is absent from the final `ls` (SessionEnd deleted it). This proves: hooks fire, the handler works under Codex, and `TMUX_PANE` reached it (state file would be the probe pane's window).
If NO state file ever appears: check whether hooks ran at all with `codex exec --dangerously-bypass-hook-trust -c 'features.hooks=true' ...`; if they ran but wrote nothing, add the temporary debug-log pattern from git history (`git log --oneline -S'TEMP DEBUG' -- scripts/hook_handler.py`) to diagnose, then remove it before committing.

- [ ] **Step 4: USER CHECKPOINT — trust the hooks interactively**

Ask the user to run `/hooks` inside one of their Codex TUI sessions (window 3 `codex-ai-biz` or window 9 `codex-aarch64-a`) and approve the claude-tmux-lights hook commands. Codex records trust by command hash, so approval in one session covers the others. Wait for confirmation.

- [ ] **Step 5: Live verification in a Codex tab**

After trust approval, ask the user to submit any prompt in the Codex session (or, with their OK, `tmux send-keys -t <their codex pane> "1+1" Enter`). Then:

```bash
ls ~/.local/state/claude-tmux-lights/
python3 /Users/mpercy/src/claude-tmux-lights/scripts/tmux_render.py <window_id> 0
```

Expected: a state file whose `tmux_pane`/`tmux_window` match the Codex pane, and the renderer emits an indicator for that window. Check the actual tmux status bar shows the light.

- [ ] **Step 6: Record the work in beads and memory**

```bash
bd create --title="Codex CLI support: lights for Codex sessions via Codex plugin hooks" --description="Codex 0.145+ clones Claude Code's hook system. Added .codex-plugin/plugin.json, .agents/plugins/marketplace.json, codex-hooks/hooks.json (sync, PermissionRequest instead of Notification), PermissionRequest branch in hook_handler.py, codex names in the client-recognition regex. Spec: docs/superpowers/specs/2026-07-26-codex-support-design.md" --type=feature --priority=2
bd close <id-from-create> --reason="Implemented and verified live on windows 3/9"
```

Update `~/.claude/projects/-Users-mpercy-src-claude-tmux-lights/memory/`: add a `codex-support.md` memory (type: project) recording the probe answers (in-place?, hooks field honored?, TMUX_PANE passthrough) and link it from `MEMORY.md`.

- [ ] **Step 7: Final commit and push**

```bash
git status --short   # should be clean or only expected files
git add -A ':!.claude' && git commit -m "Note Codex probe results" # only if probe steps changed files
git push
```

---

## Self-Review

- **Spec coverage:** handler changes → Task 1; manifests/packaging/README → Task 2; install flow, trust, empirical unknowns, live verification → Task 3. Out-of-scope items (idle-prompt, cloud sessions, external distribution) have no tasks — correct.
- **Placeholder scan:** all code blocks are complete file contents or exact diffs; probe steps state expected outcomes and fallback actions.
- **Type consistency:** `CLIENT_COMMAND_RE` renamed in Task 1 and not referenced by old name anywhere later; test helper names match the existing harness (`run_hook`, `write_state`, `read_state`, `set_panes`); manifest names (`claude-tmux-lights@claude-tmux-lights`) consistent across Tasks 2 and 3. Test counts: 15 existing + 3 (Task 1) = 18; + 2 (Task 2) = 20. ✓
