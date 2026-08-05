"""Tests for hook_handler.py and tmux_render.py.

Runs the real scripts as subprocesses against an isolated state dir
(via XDG_RUNTIME_DIR) and a fake `tmux` binary on PATH whose pane
topology is declared per-test. No mocking of the code under test.
"""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK_HANDLER = os.path.join(REPO_ROOT, "scripts", "hook_handler.py")
TMUX_RENDER = os.path.join(REPO_ROOT, "scripts", "tmux_render.py")

FAKE_TMUX = '''#!/usr/bin/env python3
import json, os, sys

spec = json.load(open(os.environ["FAKE_TMUX_STATE"]))
if spec.get("fail"):
    sys.exit(1)
args = sys.argv[1:]

def render(fmt, pane):
    return (fmt.replace("#{pane_id}", pane["pane_id"])
               .replace("#{window_id}", pane["window_id"])
               .replace("#{pane_current_command}", pane["command"])
               .replace("#{pane_current_path}", pane["path"]))

if args[:1] == ["list-panes"]:
    fmt = args[args.index("-F") + 1]
    for pane in spec["panes"]:
        print(render(fmt, pane))
elif args[:1] == ["display-message"]:
    target = args[args.index("-t") + 1]
    fmt = args[args.index("-p") + 1]
    for pane in spec["panes"]:
        if pane["pane_id"] == target:
            print(render(fmt, pane))
            sys.exit(0)
    sys.exit(1)
elif args[:1] == ["show-options"]:
    print("tmux_render.py configured")
elif args[:1] == ["refresh-client"]:
    pass
sys.exit(0)
'''

PANES = [
    {"pane_id": "%12", "window_id": "@4", "command": "2.1.220", "path": "/proj/game"},
    {"pane_id": "%21", "window_id": "@4", "command": "zsh", "path": "/proj/game"},
    {"pane_id": "%8", "window_id": "@3", "command": "zsh", "path": "/home/other"},
]


class LightsTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lights-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        self.state_dir = os.path.join(self.tmp, "runtime", "claude-tmux-lights")
        os.makedirs(self.state_dir)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)

        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir)
        tmux_path = os.path.join(bindir, "tmux")
        with open(tmux_path, "w") as f:
            f.write(FAKE_TMUX)
        os.chmod(tmux_path, os.stat(tmux_path).st_mode | stat.S_IEXEC)

        self.tmux_state = os.path.join(self.tmp, "tmux_state.json")
        self.set_panes(PANES)

        self.env = {
            "PATH": bindir + os.pathsep + os.environ.get("PATH", ""),
            "HOME": self.home,
            "XDG_RUNTIME_DIR": os.path.join(self.tmp, "runtime"),
            "FAKE_TMUX_STATE": self.tmux_state,
        }

    def set_panes(self, panes, fail=False):
        with open(self.tmux_state, "w") as f:
            json.dump({"panes": panes, "fail": fail}, f)

    def run_hook(self, event, tmux_pane=None):
        env = dict(self.env)
        if tmux_pane is not None:
            env["TMUX_PANE"] = tmux_pane
        proc = subprocess.run(
            [sys.executable, HOOK_HANDLER],
            input=json.dumps(event), capture_output=True, text=True,
            env=env, timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc

    def run_render(self, window_id, active):
        proc = subprocess.run(
            [sys.executable, TMUX_RENDER, window_id, "1" if active else "0"],
            capture_output=True, text=True, env=self.env, timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def state_path(self, session_id):
        return os.path.join(self.state_dir, f"{session_id}.json")

    def read_state(self, session_id):
        with open(self.state_path(session_id)) as f:
            return json.load(f)

    def write_state(self, session_id, **overrides):
        state = {
            "session_id": session_id,
            "state": "waiting",
            "state_changed_ts": time.time(),
            "waiting_since_ts": time.time(),
            "cwd": "/proj/game",
            "tmux_pane": "%12",
            "tmux_window": "@4",
        }
        state.update(overrides)
        with open(self.state_path(session_id), "w") as f:
            json.dump(state, f)
        return state


class TestDaemonSessionPaneFallback(LightsTestCase):
    """Sessions hosted in daemon workers have no TMUX_PANE; the handler
    must fall back to matching the event cwd against pane paths."""

    def test_stop_without_tmux_pane_resolves_pane_by_cwd(self):
        self.run_hook({"hook_event_name": "Stop", "session_id": "bg1",
                       "cwd": "/proj/game"})
        state = self.read_state("bg1")
        # %12 (claude client) wins over %21 (idle zsh) in the same cwd
        self.assertEqual(state["tmux_pane"], "%12")
        self.assertEqual(state["tmux_window"], "@4")
        self.assertEqual(state["state"], "waiting")

    def test_without_tmux_pane_prefers_existing_state_over_rescan(self):
        self.write_state("bg1", state="active", tmux_pane="%8", tmux_window="@3")
        self.run_hook({"hook_event_name": "Stop", "session_id": "bg1",
                       "cwd": "/proj/game"})
        state = self.read_state("bg1")
        # The Stop event was processed (active -> waiting) using the
        # already-recorded pane, not a fresh cwd scan (which would pick %12)
        self.assertEqual(state["state"], "waiting")
        self.assertEqual(state["tmux_pane"], "%8")
        self.assertEqual(state["tmux_window"], "@3")

    def test_without_tmux_pane_and_no_cwd_match_writes_nothing(self):
        self.run_hook({"hook_event_name": "Stop", "session_id": "bg1",
                       "cwd": "/nowhere"})
        self.assertFalse(os.path.exists(self.state_path("bg1")))

    def test_without_tmux_pane_ignores_non_claude_programs_in_same_cwd(self):
        # A `top` (or vim, npm...) launched from the session's directory in
        # some other window must not make resolution ambiguous
        self.set_panes(PANES + [
            {"pane_id": "%10", "window_id": "@3", "command": "top",
             "path": "/proj/game"},
        ])
        self.run_hook({"hook_event_name": "Stop", "session_id": "bg1",
                       "cwd": "/proj/game"})
        state = self.read_state("bg1")
        self.assertEqual(state["tmux_pane"], "%12")
        self.assertEqual(state["tmux_window"], "@4")

    def test_without_tmux_pane_single_shell_pane_still_matches(self):
        # No claude client in the cwd, but exactly one pane lives there —
        # light that window rather than staying invisible
        self.run_hook({"hook_event_name": "Stop", "session_id": "bg1",
                       "cwd": "/home/other"})
        state = self.read_state("bg1")
        self.assertEqual(state["tmux_pane"], "%8")
        self.assertEqual(state["tmux_window"], "@3")

    def test_without_tmux_pane_ambiguous_candidates_writes_nothing(self):
        self.set_panes(PANES + [
            {"pane_id": "%30", "window_id": "@5", "command": "2.1.220",
             "path": "/proj/game"},
        ])
        self.run_hook({"hook_event_name": "Stop", "session_id": "bg1",
                       "cwd": "/proj/game"})
        self.assertFalse(os.path.exists(self.state_path("bg1")))

    def test_resolved_session_renders_in_its_window(self):
        self.run_hook({"hook_event_name": "Stop", "session_id": "bg1",
                       "cwd": "/proj/game"})
        out = self.run_render("@4", active=False)
        self.assertIn("#[fg=", out)

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


class TestSelfHealingStateFiles(LightsTestCase):
    """Once a state file is wrongly removed, mid-turn hook events must be
    able to recreate it — not just UserPromptSubmit/Stop."""

    def test_posttooluse_recreates_missing_state_file(self):
        self.run_hook({"hook_event_name": "PostToolUse", "session_id": "s1",
                       "cwd": "/proj/game"}, tmux_pane="%12")
        state = self.read_state("s1")
        self.assertEqual(state["state"], "active")
        self.assertEqual(state["tmux_window"], "@4")

    def test_posttooluse_still_flips_waiting_to_active(self):
        self.write_state("s1", state="waiting")
        self.run_hook({"hook_event_name": "PostToolUse", "session_id": "s1"},
                      tmux_pane="%12")
        self.assertEqual(self.read_state("s1")["state"], "active")

    def test_notification_permission_prompt_recreates_missing_state_file(self):
        self.run_hook({"hook_event_name": "Notification",
                       "notification_type": "permission_prompt",
                       "session_id": "s1", "cwd": "/proj/game"},
                      tmux_pane="%12")
        state = self.read_state("s1")
        self.assertEqual(state["state"], "waiting")
        self.assertEqual(state["tmux_window"], "@4")

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


class TestOrphanCleanup(LightsTestCase):
    """Pane liveness is the source of truth; the time threshold only
    applies when the pane list can't be read."""

    def test_cleanup_keeps_idle_waiting_session_with_live_pane(self):
        old = time.time() - 7200
        self.write_state("s1", state_changed_ts=old, waiting_since_ts=old)
        self.run_render("@9", active=True)  # active window triggers cleanup
        self.assertTrue(os.path.exists(self.state_path("s1")))

    def test_cleanup_removes_session_with_dead_pane(self):
        self.write_state("s1", tmux_pane="%99", tmux_window="@9")
        self.run_render("@1", active=True)
        self.assertFalse(os.path.exists(self.state_path("s1")))

    def test_cleanup_time_reaps_stale_waiting_when_pane_list_unavailable(self):
        old = time.time() - 7200
        self.write_state("s1", state_changed_ts=old, waiting_since_ts=old)
        self.set_panes([], fail=True)
        self.run_render("@9", active=True)
        self.assertFalse(os.path.exists(self.state_path("s1")))

    def test_cleanup_time_spares_fresh_waiting_when_pane_list_unavailable(self):
        self.write_state("s1")
        self.set_panes([], fail=True)
        self.run_render("@9", active=True)
        self.assertTrue(os.path.exists(self.state_path("s1")))

    def test_cleanup_treats_empty_pane_list_as_unavailable(self):
        # list-panes succeeding with zero panes must not mass-delete state
        self.write_state("s1")
        self.set_panes([])
        self.run_render("@9", active=True)
        self.assertTrue(os.path.exists(self.state_path("s1")))

    def test_cleanup_uses_pane_commands_map(self):
        # get_pane_commands returns {pane_id: command}; pane-death reaping
        # must keep working through the new structure
        self.write_state("dead", tmux_pane="%99", tmux_window="@9")
        self.write_state("alive", tmux_pane="%12", tmux_window="@4")
        self.run_render("@1", active=True)
        self.assertFalse(os.path.exists(self.state_path("dead")))
        self.assertTrue(os.path.exists(self.state_path("alive")))


CODEX_EVENTS = {
    "SessionStart", "SessionEnd", "SubagentStart", "SubagentStop",
    "PreToolUse", "PostToolUse", "PermissionRequest",
    "PreCompact", "PostCompact", "UserPromptSubmit", "Stop",
}


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


if __name__ == "__main__":
    unittest.main()
