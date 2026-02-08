#!/usr/bin/env python3
"""tmux status line renderer for Claude Code session indicators.

Called from tmux window-status-format:
  #(python3 /path/to/tmux_render.py #{window_id} #{window_active})

Reads per-session state files and outputs tmux-formatted indicator strings.
Writes to viewed.json when the active window has waiting sessions.
"""

import json
import os
import sys
import time

# Add scripts directory to path for sibling imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from utils import atomic_write_json, get_state_dir, read_json_safe


# Default configuration
DEFAULTS = {
    "active_icon": "\u25cf",       # ●
    "waiting_icon": "\u25c6",      # ◆
    "viewed_icon": "\u25cb",       # ○
    "active_color": "colour46",    # green
    "waiting_color": "colour226",  # yellow
    "viewed_color": "colour252",   # light grey
    "orphan_threshold_seconds": 1800,
    "show_elapsed_time": True,
    "max_indicators_per_window": 5,
}


def load_config():
    """Load config from ~/.config/claude-tmux-lights/config.json, with defaults."""
    config = dict(DEFAULTS)
    config_path = os.path.join(
        os.path.expanduser("~"), ".config", "claude-tmux-lights", "config.json"
    )
    try:
        with open(config_path) as f:
            user_config = json.load(f)
        config.update(user_config)
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return config


def format_elapsed(seconds):
    """Format elapsed seconds as compact string: Ns, Nm, or NhMm."""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    remaining_minutes = minutes % 60
    return f"{hours}h{remaining_minutes}m"


def render_indicator(session, viewed_json, config, now):
    """Render a single session's tmux indicator string."""
    state = session.get("state", "")
    session_id = session.get("session_id", "")
    state_changed_ts = session.get("state_changed_ts", 0)

    if state == "active":
        icon = config["active_icon"]
        color = config["active_color"]
        return f"#[fg={color}]{icon}#[fg=default]"

    if state == "waiting":
        viewed_ts = viewed_json.get(session_id, 0)
        if viewed_ts >= state_changed_ts:
            # Viewed/acknowledged
            icon = config["viewed_icon"]
            color = config["viewed_color"]
            return f"#[fg={color}]{icon}#[fg=default]"
        else:
            # Not yet viewed — show yellow + elapsed time
            icon = config["waiting_icon"]
            color = config["waiting_color"]
            waiting_since = session.get("waiting_since_ts") or state_changed_ts
            elapsed = now - waiting_since
            time_str = ""
            if config.get("show_elapsed_time", True):
                time_str = format_elapsed(elapsed)
            return f"#[fg={color}]{icon}#[fg=default]{time_str}"

    return ""


def cleanup_orphans(state_dir, sessions, threshold):
    """Remove orphaned state files (waiting sessions older than threshold)."""
    now = time.time()
    for session in sessions:
        # Only clean up waiting sessions — active sessions may be running
        # long tasks without state transitions
        if session.get("state") != "waiting":
            continue
        state_changed_ts = session.get("state_changed_ts", 0)
        if now - state_changed_ts > threshold:
            session_id = session.get("session_id", "")
            if session_id:
                try:
                    os.unlink(os.path.join(state_dir, f"{session_id}.json"))
                except OSError:
                    pass


def main():
    if len(sys.argv) < 3:
        sys.exit(0)

    window_id = sys.argv[1]
    window_active = sys.argv[2] == "1"

    state_dir = get_state_dir()
    if not os.path.isdir(state_dir):
        sys.exit(0)

    config = load_config()
    now = time.time()

    # Read all session state files
    sessions = []
    all_sessions = []
    try:
        for filename in os.listdir(state_dir):
            if not filename.endswith(".json") or filename == "viewed.json":
                continue
            path = os.path.join(state_dir, filename)
            data = read_json_safe(path)
            if data and isinstance(data, dict) and "session_id" in data:
                all_sessions.append(data)
                if data.get("tmux_window") == window_id:
                    sessions.append(data)
    except OSError:
        sys.exit(0)

    if not sessions:
        # Opportunistic cleanup — only from the active window to avoid
        # N redundant passes (one per window) every status-interval
        if window_active and all_sessions:
            cleanup_orphans(state_dir, all_sessions, config["orphan_threshold_seconds"])
        sys.exit(0)

    # Read viewed.json
    viewed_path = os.path.join(state_dir, "viewed.json")
    viewed_json = read_json_safe(viewed_path) or {}

    # If this is the active window, mark all waiting sessions as viewed
    if window_active:
        updated = False
        for session in sessions:
            if session.get("state") == "waiting":
                sid = session.get("session_id", "")
                state_changed_ts = session.get("state_changed_ts", 0)
                viewed_ts = viewed_json.get(sid, 0)
                if viewed_ts < state_changed_ts:
                    viewed_json[sid] = now
                    updated = True
        if updated:
            atomic_write_json(viewed_path, viewed_json)

    # Deduplicate by tmux_pane — if two sessions claim the same pane,
    # keep only the most recently updated one (the other is a ghost).
    by_pane = {}
    for session in sessions:
        pane = session.get("tmux_pane", "")
        existing = by_pane.get(pane)
        if existing is None or session.get("state_changed_ts", 0) > existing.get("state_changed_ts", 0):
            by_pane[pane] = session
    sessions = list(by_pane.values())

    # Sort sessions by session_id for deterministic indicator order
    sessions.sort(key=lambda s: s.get("session_id", ""))

    # Render indicators
    max_indicators = config["max_indicators_per_window"]
    indicators = []
    for session in sessions[:max_indicators]:
        indicator = render_indicator(session, viewed_json, config, now)
        if indicator:
            indicators.append(indicator)

    if indicators:
        # Leading space to separate from window name
        sys.stdout.write(" " + "".join(indicators))

    # Opportunistic cleanup — only from the active window
    if window_active:
        cleanup_orphans(state_dir, all_sessions, config["orphan_threshold_seconds"])


if __name__ == "__main__":
    main()
