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
import tempfile
import time


def get_state_dir():
    """Resolve state directory in priority order:
    1. $XDG_RUNTIME_DIR/claude-tmux-lights/ (Linux, volatile)
    2. ~/.local/state/claude-tmux-lights/ (macOS / fallback)
    """
    xdg_runtime = os.environ.get("XDG_RUNTIME_DIR")
    if xdg_runtime:
        return os.path.join(xdg_runtime, "claude-tmux-lights")
    return os.path.join(os.path.expanduser("~"), ".local", "state", "claude-tmux-lights")


def atomic_write_json(path, data):
    """Write JSON atomically using mkstemp + rename."""
    dir_path = os.path.dirname(path)
    fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.rename(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def read_json_safe(path):
    """Read JSON file, returning None on any error."""
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return None


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

    tmux_pane = os.environ.get("TMUX_PANE", "")
    if not tmux_pane:
        # Not running in tmux — skip state file operations but still
        # check setup on SessionStart
        if hook_name == "SessionStart":
            return check_tmux_setup() if os.environ.get("TMUX") else None
        return None

    state_path = os.path.join(state_dir, f"{session_id}.json")
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
        window_id = get_tmux_window_id(tmux_pane)
        state = {
            "session_id": session_id,
            "state": "active",
            "state_changed_ts": now,
            "waiting_since_ts": None,
            "cwd": event.get("cwd", ""),
            "tmux_pane": tmux_pane,
            "tmux_window": window_id or "",
        }
        atomic_write_json(state_path, state)
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
        return None

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
        notification_type = event.get("notification", "")
        if notification_type != "idle_prompt":
            return None

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
