#!/usr/bin/env python3
"""tmux status line renderer for Claude Code session indicators.

Preferred (one process per status-interval), called from status-left:
  #(python3 /path/to/tmux_render.py --all)
Computes every window's indicator and publishes each as the window option
@light in a single tmux invocation; window-status-format then reads it with
plain format expansion:
  setw -g window-status-format ' #I:#W#{@light} '

Legacy (one process per window per tick), called from window-status-format:
  #(python3 /path/to/tmux_render.py #{window_id} #{window_active})
Prints the indicator for one window to stdout.

Reads per-session state files; writes viewed.json when an active window has
waiting sessions.
"""

import json
import os
import subprocess
import sys
import time

# Add scripts directory to path for sibling imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from utils import CLIENT_COMMAND_RE, atomic_write_json, get_state_dir, read_json_safe


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


def daemon_job_record(session_id):
    """Return the Claude daemon job record for a session, or None.

    The record (~/.claude/jobs/<short>/state.json) exists only for
    daemon-hosted sessions; its presence proves the session is alive even
    when no tmux pane hosts a client for it.
    """
    path = os.path.join(os.path.expanduser("~"), ".claude", "jobs",
                        session_id[:8], "state.json")
    record = read_json_safe(path)
    if isinstance(record, dict) and record.get("sessionId") == session_id:
        return record
    return None


def job_is_busy(record):
    """A job record counts as busy while work is actually running.

    Trust only `state == "working"`. inFlight.tasks was dropped: live
    daemon data showed done/idle records carrying inFlight.tasks > 0 for
    days (leaked counters from fan agents), which under an OR clause made
    a session green forever once acknowledged.
    """
    if not record:
        return False
    return record.get("state") == "working"


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
            # Viewed/acknowledged — but show green if daemon-hosted work
            # is still running (turn ended, background task grinding on)
            if job_is_busy(daemon_job_record(session_id)):
                icon = config["active_icon"]
                color = config["active_color"]
            else:
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


def get_panes():
    """Describe every live pane with one tmux call.

    Returns {pane_id: {"window": window_id, "active": bool, "command": str}}
    where "active" means the pane's window is the current window of its
    session. Returns None when the list can't be read or comes back empty
    (we render from inside a pane, so an empty list can't be real) —
    callers treat None as "couldn't check" and skip pane-based decisions.
    """
    try:
        result = subprocess.run(
            ["tmux", "list-panes", "-a", "-F",
             "#{pane_id}\t#{window_id}\t#{window_active}\t#{pane_current_command}"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            panes = {}
            for line in result.stdout.splitlines():
                fields = line.split("\t", 3)
                if len(fields) < 4 or not fields[0]:
                    continue
                pane_id, window_id, active, command = fields
                panes[pane_id] = {
                    "window": window_id,
                    "active": active == "1",
                    "command": command,
                }
            return panes or None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def get_pane_commands():
    """Map live pane IDs to their foreground commands (None if unavailable)."""
    panes = get_panes()
    if panes is None:
        return None
    return {pane_id: info["command"] for pane_id, info in panes.items()}


def cleanup_orphans(state_dir, sessions, threshold, pane_commands=None):
    """Remove state files for dead sessions; demote stale-active ones.

    Pane liveness is the source of truth: a session whose pane is gone is
    dead (its SessionEnd never fired). The time threshold applies only when
    the pane list can't be read or a file has no pane recorded — an idle
    session in a live pane keeps its light indefinitely, with one
    exception: a stale-active session on a non-client pane demotes to
    acknowledged-waiting once its state has been unchanged past the
    threshold (see below).
    """
    now = time.time()
    if pane_commands is None:
        pane_commands = get_pane_commands()

    for session in sessions:
        session_id = session.get("session_id", "")
        if not session_id:
            continue
        pane = session.get("tmux_pane", "")

        if pane_commands is not None and pane:
            if pane not in pane_commands:
                try:
                    os.unlink(os.path.join(state_dir, f"{session_id}.json"))
                except OSError:
                    pass
                continue

            # Demote stale-active sessions on non-client panes: the pane
            # isn't visibly hosting a client, no events have arrived for
            # 30+ minutes, and no daemon job record proves it alive — the
            # turn-end event was likely missed (e.g. laptop closed).
            # Green would lie forever; acknowledged-white is the truth.
            if (session.get("state") == "active"
                    and now - session.get("state_changed_ts", 0) > threshold
                    and not CLIENT_COMMAND_RE.match(pane_commands.get(pane, ""))
                    and daemon_job_record(session_id) is None):
                # Acknowledge BEFORE rewriting state: a concurrent renderer
                # reading between the two writes must never see a waiting
                # session whose state_changed_ts is newer than viewed_ts —
                # that would flash yellow. Writing viewed.json first means
                # the worst-case interleaving is a harmless viewed entry
                # for a session that (from the reader's view) is still
                # active; a crash between the writes leaves the same
                # harmless state.
                viewed_path = os.path.join(state_dir, "viewed.json")
                viewed_json = read_json_safe(viewed_path) or {}
                viewed_json[session_id] = now
                try:
                    atomic_write_json(viewed_path, viewed_json)
                except OSError:
                    pass
                demoted = dict(session)
                demoted["state"] = "waiting"
                demoted["state_changed_ts"] = now
                demoted["waiting_since_ts"] = now
                try:
                    atomic_write_json(
                        os.path.join(state_dir, f"{session_id}.json"), demoted)
                except OSError:
                    pass
            continue

        # Fallback: pane unknown or list unavailable — reap stale waiting
        # sessions by age (active sessions may be running long tasks)
        if session.get("state") != "waiting":
            continue
        state_changed_ts = session.get("state_changed_ts", 0)
        if now - state_changed_ts > threshold:
            try:
                os.unlink(os.path.join(state_dir, f"{session_id}.json"))
            except OSError:
                pass


def load_sessions(state_dir):
    """Read every session state file in state_dir (viewed.json excluded)."""
    sessions = []
    try:
        for filename in os.listdir(state_dir):
            if not filename.endswith(".json") or filename == "viewed.json":
                continue
            data = read_json_safe(os.path.join(state_dir, filename))
            if data and isinstance(data, dict) and "session_id" in data:
                sessions.append(data)
    except OSError:
        pass
    return sessions


def render_window(sessions, window_active, viewed_json, config, now):
    """Render one window's indicator string from its sessions.

    If the window is active, its waiting sessions are acknowledged in
    viewed_json (mutated in place). Returns (text, viewed_changed).
    """
    changed = False
    if window_active:
        for session in sessions:
            if session.get("state") == "waiting":
                sid = session.get("session_id", "")
                if viewed_json.get(sid, 0) < session.get("state_changed_ts", 0):
                    viewed_json[sid] = now
                    changed = True

    # Deduplicate by tmux_pane — if two sessions claim the same pane,
    # keep only the most recently updated one (the other is a ghost).
    by_pane = {}
    for session in sessions:
        pane = session.get("tmux_pane", "")
        existing = by_pane.get(pane)
        if existing is None or session.get("state_changed_ts", 0) > existing.get("state_changed_ts", 0):
            by_pane[pane] = session
    sessions = sorted(by_pane.values(), key=lambda s: s.get("session_id", ""))

    indicators = []
    for session in sessions[:config["max_indicators_per_window"]]:
        indicator = render_indicator(session, viewed_json, config, now)
        if indicator:
            indicators.append(indicator)

    # Leading space separates the indicators from the window name
    text = " " + "".join(indicators) if indicators else ""
    return text, changed


def publish_lights(lights, refresh=False):
    """Set @light on every window in `lights` with ONE tmux invocation.

    tmux accepts several commands in one argv separated by ';', so nine
    windows cost one fork, not nine. An empty value clears a window whose
    sessions have gone. With refresh=True the status line is redrawn in the
    same call (used from hooks for instant updates).
    """
    argv = ["tmux"]
    for window_id, text in lights.items():
        argv += ["set", "-w", "-t", window_id, "@light", text, ";"]
    if refresh:
        argv += ["refresh-client", "-S", ";"]
    if len(argv) == 1:
        return False
    argv.pop()  # trailing ';'
    try:
        subprocess.run(argv, capture_output=True, timeout=5)
        return True
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def update_all(refresh=False):
    """One-process mode: compute every window's light and publish them.

    Returns True if tmux was reached and the lights were published.
    """
    state_dir = get_state_dir()
    if not os.path.isdir(state_dir):
        return False
    panes = get_panes()
    if panes is None:
        return False  # tmux unreachable: nothing to draw on, change nothing

    config = load_config()
    pane_commands = {pid: info["command"] for pid, info in panes.items()}

    # Reap/demote first so this tick renders the corrected state, then
    # re-read whatever survived.
    cleanup_orphans(state_dir, load_sessions(state_dir),
                    config["orphan_threshold_seconds"], pane_commands)
    sessions = load_sessions(state_dir)
    now = time.time()

    windows = {}  # window_id -> is active window in its session
    for info in panes.values():
        windows[info["window"]] = windows.get(info["window"], False) or info["active"]

    by_window = {}
    for session in sessions:
        window_id = session.get("tmux_window", "")
        if not window_id:
            # Daemon-hosted sessions may know their pane but not its window
            window_id = panes.get(session.get("tmux_pane", ""), {}).get("window", "")
        if window_id:
            by_window.setdefault(window_id, []).append(session)

    viewed_path = os.path.join(state_dir, "viewed.json")
    viewed_json = read_json_safe(viewed_path) or {}
    lights = {}
    viewed_changed = False
    for window_id, active in windows.items():
        text, changed = render_window(by_window.get(window_id, []), active,
                                      viewed_json, config, now)
        lights[window_id] = text
        viewed_changed = viewed_changed or changed
    if viewed_changed:
        try:
            atomic_write_json(viewed_path, viewed_json)
        except OSError:
            pass

    return publish_lights(lights, refresh=refresh)


def render_one(window_id, window_active):
    """Legacy per-window mode: print this window's indicator to stdout."""
    state_dir = get_state_dir()
    if not os.path.isdir(state_dir):
        return

    config = load_config()
    now = time.time()
    all_sessions = load_sessions(state_dir)
    sessions = [s for s in all_sessions if s.get("tmux_window") == window_id]

    if sessions:
        viewed_path = os.path.join(state_dir, "viewed.json")
        viewed_json = read_json_safe(viewed_path) or {}
        text, changed = render_window(sessions, window_active, viewed_json, config, now)
        if changed:
            atomic_write_json(viewed_path, viewed_json)
        sys.stdout.write(text)

    # Opportunistic cleanup — only from the active window to avoid
    # N redundant passes (one per window) every status-interval
    if window_active and all_sessions:
        cleanup_orphans(state_dir, all_sessions, config["orphan_threshold_seconds"])


def main():
    args = sys.argv[1:]
    if args[:1] == ["--all"]:
        update_all()
        return
    if len(args) < 2:
        return
    render_one(args[0], args[1] == "1")


if __name__ == "__main__":
    main()
