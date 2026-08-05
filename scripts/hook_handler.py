#!/usr/bin/env python3
"""Claude Code hook handler for tmux status indicators.

Reads hook event JSON from stdin, writes per-session state files
to the state directory, and triggers tmux status refresh.

State files: {state_dir}/{session_id}.json
View tracking: {state_dir}/viewed.json
"""

import json
import os
import subprocess
import sys
import time

# Add scripts directory to path for sibling imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from utils import atomic_write_json, get_state_dir, read_json_safe, CLIENT_COMMAND_RE


def get_tmux_window_id(tmux_pane):
    """Resolve tmux pane ID to window ID."""
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-t", tmux_pane, "-p", "#{window_id}"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def find_pane_by_cwd(cwd):
    """Locate the tmux pane for a session that has no TMUX_PANE.

    Daemon-hosted (background) sessions run hooks in a worker process with
    no tmux ancestry, so the pane must be inferred: match the session's cwd
    against pane working directories, preferring the pane whose foreground
    process looks like a Claude or Codex client ("claude", "codex", or a bare version string
    like "2.1.220" — the CLI names its process after its version). Unrelated
    programs launched from the same directory (top, vim, dev servers) must
    not win or block the match. Returns (pane_id, window_id), or
    (None, None) unless exactly one candidate remains — no light beats a
    light on the wrong window.
    """
    if not cwd:
        return None, None
    try:
        result = subprocess.run(
            ["tmux", "list-panes", "-a", "-F",
             "#{pane_id}\t#{window_id}\t#{pane_current_command}\t#{pane_current_path}"],
            capture_output=True, text=True, timeout=5
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None, None
    if result.returncode != 0:
        return None, None

    target = os.path.realpath(cwd)
    candidates = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 4:
            continue
        pane_id, window_id, command, path = parts
        if os.path.realpath(path) == target:
            candidates.append((pane_id, window_id, command))

    claude_like = [c for c in candidates if CLIENT_COMMAND_RE.match(c[2])]
    if len(claude_like) == 1:
        return claude_like[0][0], claude_like[0][1]
    if not claude_like and len(candidates) == 1:
        return candidates[0][0], candidates[0][1]
    return None, None


def tmux_refresh():
    """Trigger tmux status line refresh."""
    try:
        subprocess.run(
            ["tmux", "refresh-client", "-S"],
            capture_output=True, timeout=5
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass


def mark_viewed(state_dir, session_id, timestamp):
    """Add a session to viewed.json (read-modify-write with atomic rename)."""
    viewed_path = os.path.join(state_dir, "viewed.json")
    viewed = read_json_safe(viewed_path) or {}
    viewed[session_id] = timestamp
    atomic_write_json(viewed_path, viewed)


def check_tmux_setup():
    """Check if tmux is configured for this plugin. Returns systemMessage if not.

    Rate-limited to once per 24h via marker file.
    """
    marker_dir = os.path.join(os.path.expanduser("~"), ".config", "claude-tmux-lights")
    marker_path = os.path.join(marker_dir, ".setup-prompted")

    # Check if already prompted recently
    try:
        if os.path.exists(marker_path):
            age = time.time() - os.path.getmtime(marker_path)
            if age < 86400:  # 24 hours
                return None
    except OSError:
        pass

    # Check if tmux has our renderer configured
    try:
        result = subprocess.run(
            ["tmux", "show-options", "-gv", "window-status-format"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0 and "tmux_render.py" in result.stdout:
            return None  # Already configured
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None  # Can't check, don't nag

    # Not configured — write marker and return message
    try:
        os.makedirs(marker_dir, exist_ok=True)
        with open(marker_path, "w") as f:
            f.write("")
    except OSError:
        pass

    return "claude-tmux-lights: tmux not configured. Run /claude-tmux-lights:setup to add status indicators to your tmux tab bar."


def handle_event(event, state_dir):
    """Process a hook event and update state files."""
    hook_name = event.get("hook_event_name", "")
    session_id = event.get("session_id", "")

    if not session_id:
        return None

    state_path = os.path.join(state_dir, f"{session_id}.json")

    tmux_pane = os.environ.get("TMUX_PANE", "")
    if not tmux_pane:
        # No tmux ancestry (e.g. a daemon-hosted background session whose
        # hooks run in a detached worker) — fall back to the pane recorded
        # on a previous event, then to matching the session cwd against
        # pane working directories.
        existing = read_json_safe(state_path)
        if existing and existing.get("tmux_pane"):
            tmux_pane = existing["tmux_pane"]
        else:
            tmux_pane, _ = find_pane_by_cwd(event.get("cwd", ""))
            tmux_pane = tmux_pane or ""
    if not tmux_pane:
        # Genuinely not visible in tmux — skip state file operations but
        # still check setup on SessionStart
        if hook_name == "SessionStart":
            return check_tmux_setup() if os.environ.get("TMUX") else None
        return None

    now = time.time()

    if hook_name == "SessionEnd":
        # Delete state file
        try:
            os.unlink(state_path)
        except OSError:
            pass
        tmux_refresh()
        return None

    if hook_name == "SessionStart":
        # Evict stale state files claiming the same pane — a pane can only
        # run one Claude session, so any prior file is a ghost from a
        # killed/restarted session whose SessionEnd never fired.
        try:
            for filename in os.listdir(state_dir):
                if not filename.endswith(".json") or filename == "viewed.json":
                    continue
                if filename == f"{session_id}.json":
                    continue  # Don't evict ourselves
                path = os.path.join(state_dir, filename)
                data = read_json_safe(path)
                if data and data.get("tmux_pane") == tmux_pane:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass
        except OSError:
            pass

        window_id = get_tmux_window_id(tmux_pane)
        # Start as "waiting" + pre-acknowledged so the light is white (idle).
        # UserPromptSubmit will flip to "active" (green) when work begins.
        state = {
            "session_id": session_id,
            "state": "waiting",
            "state_changed_ts": now,
            "waiting_since_ts": now,
            "cwd": event.get("cwd", ""),
            "tmux_pane": tmux_pane,
            "tmux_window": window_id or "",
        }
        atomic_write_json(state_path, state)
        mark_viewed(state_dir, session_id, now)
        tmux_refresh()
        return check_tmux_setup()

    if hook_name == "UserPromptSubmit":
        existing = read_json_safe(state_path)
        window_id = (existing or {}).get("tmux_window", "") or get_tmux_window_id(tmux_pane)
        state = {
            "session_id": session_id,
            "state": "active",
            "state_changed_ts": now,
            "waiting_since_ts": None,
            "cwd": event.get("cwd", (existing or {}).get("cwd", "")),
            "tmux_pane": tmux_pane,
            "tmux_window": window_id,
        }
        atomic_write_json(state_path, state)
        tmux_refresh()
        return check_tmux_setup()

    if hook_name == "Stop":
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

    if hook_name == "Notification":
        notification_type = event.get("notification_type", "")

        if notification_type in ("permission_prompt", "elicitation_dialog"):
            # Claude is blocked waiting for user action (permission approval
            # or answering a question) — treat like Stop so the light turns
            # yellow (waiting for user action). Writes even when no state
            # file exists so a wrongly-removed file self-heals.
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

        if notification_type == "idle_prompt":
            existing = read_json_safe(state_path)
            if not existing:
                return None

            prev_state = existing.get("state", "")
            window_id = existing.get("tmux_window", "") or get_tmux_window_id(tmux_pane)

            if prev_state == "active":
                # Interrupt recovery: user pressed Esc, idle_prompt fired ~60s later.
                # Set state to waiting and auto-acknowledge (mark viewed).
                state = {
                    "session_id": session_id,
                    "state": "waiting",
                    "state_changed_ts": now,
                    "waiting_since_ts": now,
                    "cwd": existing.get("cwd", ""),
                    "tmux_pane": tmux_pane,
                    "tmux_window": window_id,
                }
                atomic_write_json(state_path, state)
                mark_viewed(state_dir, session_id, now)
                tmux_refresh()
            # If already waiting, no-op (Stop already fired)

            return None

        return None

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

    if hook_name == "PostToolUse":
        # A tool just completed — Claude is working. Flip "waiting" (from a
        # permission prompt or elicitation dialog) back to "active", and
        # recreate the file if it's missing so a wrongly-removed session
        # self-heals mid-turn. Skip if already active to avoid writes.
        existing = read_json_safe(state_path)
        if existing and existing.get("state") == "active":
            return None
        window_id = (existing or {}).get("tmux_window", "") or get_tmux_window_id(tmux_pane)
        state = {
            "session_id": session_id,
            "state": "active",
            "state_changed_ts": now,
            "waiting_since_ts": None,
            "cwd": event.get("cwd", (existing or {}).get("cwd", "")),
            "tmux_pane": tmux_pane,
            "tmux_window": window_id,
        }
        atomic_write_json(state_path, state)
        tmux_refresh()
        return None

    return None


def main():
    # Read hook event from stdin
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    # Ensure state directory exists
    state_dir = get_state_dir()
    os.makedirs(state_dir, exist_ok=True)

    # Process event
    result = handle_event(event, state_dir)

    # Output result (systemMessage for SessionStart setup prompt)
    if result:
        output = {"systemMessage": result}
        json.dump(output, sys.stdout)


if __name__ == "__main__":
    main()
