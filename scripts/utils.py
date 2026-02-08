"""Shared utilities for claude-tmux-lights."""

import json
import os
import tempfile


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
