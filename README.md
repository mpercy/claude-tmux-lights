# claude-tmux-lights

At-a-glance status indicators for Claude Code sessions in your tmux tab bar.

Running multiple Claude Code sessions across tmux windows? **claude-tmux-lights** adds a small colored indicator next to each window name so you can see which sessions are working, which need your attention, and which are idle — without switching windows.

```
 1:backend 🟢   2:frontend ⚠️3m   3:tests ⚪   4:docs
     ↑               ↑              ↑           ↑
  working      needs input        idle     no session
```

## What the indicators mean

| Indicator | Default | Meaning |
|-----------|---------|---------|
| Green | ● | Claude is actively working |
| Yellow + elapsed time | ◆ 3m | Claude finished, needs your attention (you haven't looked yet) |
| White | ○ | Claude finished, you've seen it (acknowledged) |
| *(none)* | | No Claude Code session in this window |

Yellow appears when Claude:
- Finishes a response and waits for your next prompt
- Needs you to approve a tool permission
- Asks you a question (elicitation dialog)

Switching to that tmux window auto-acknowledges it (yellow becomes white). Submitting a new prompt turns it green again.

## Installation

### Claude Code plugin (recommended)

**Option A: Development / testing** (session-only)

```bash
claude --plugin-dir /path/to/claude-tmux-lights
```

**Option B: Permanent install** via local marketplace

```bash
# Inside Claude Code:
/plugin marketplace add /path/to/claude-tmux-lights
/plugin install claude-tmux-lights@claude-tmux-lights
```

Then configure tmux by running the setup command inside Claude Code:

```
/claude-tmux-lights:setup
```

The plugin will also nudge you to run setup if it detects tmux isn't configured.

### With TPM (Tmux Plugin Manager)

Add to your `~/.tmux.conf`:

```bash
set -g @plugin 'mpercy/claude-tmux-lights'
```

Press `prefix + I` to install. The plugin auto-configures your `window-status-format`.

> **Note:** TPM handles the tmux side only. You still need the Claude Code plugin installed for the hooks to fire.

### Manual tmux configuration

If you prefer not to use TPM or the setup command, add the renderer call to your tmux config directly:

```bash
# In ~/.tmux.conf (adjust the path to where you cloned the repo)
setw -g window-status-format ' #I:#W#(python3 /path/to/claude-tmux-lights/scripts/tmux_render.py #{window_id} #{window_active}) '
setw -g window-status-current-format ' #I:#W#(python3 /path/to/claude-tmux-lights/scripts/tmux_render.py #{window_id} #{window_active}) '
```

Reload with `tmux source-file ~/.tmux.conf` or `prefix + R`.

## How it works

The plugin has two components:

**Hook handler** (`scripts/hook_handler.py`) — A Claude Code plugin hook that writes per-session JSON state files whenever Claude starts working, stops, needs permission, or ends a session. It listens to 7 hook events:

| Hook | Effect |
|------|--------|
| `SessionStart` | Creates state file (white/idle) |
| `UserPromptSubmit` | Sets state to active (green) |
| `PostToolUse` | Restores active after permission approval |
| `Stop` | Sets state to waiting (yellow) |
| `Notification(permission_prompt)` | Sets state to waiting (yellow) |
| `Notification(elicitation_dialog)` | Sets state to waiting (yellow) |
| `Notification(idle_prompt)` | Interrupt recovery (white) |
| `SessionEnd` | Deletes state file |

**Renderer** (`scripts/tmux_render.py`) — Called by tmux every `status-interval` (typically 2s) for each window tab via `#()` interpolation. Reads state files, determines display, and outputs tmux-formatted indicator strings.

State files live in `$XDG_RUNTIME_DIR/claude-tmux-lights/` or `~/.local/state/claude-tmux-lights/`.

### State machine

```
SessionStart ──► white ○ (idle)
                    │
         UserPromptSubmit
                    │
                    ▼
              green ● (active) ◄─── PostToolUse (if was waiting)
                    │                     ▲
                    ▼                     │
    Stop / permission_prompt ──► yellow ◆ (waiting)
                                    │
                          [switch to window]
                                    │
                                    ▼
                              white ○ (viewed)
```

### Resilience

- **Ghost cleanup**: If Claude Code is killed without `SessionEnd` firing, the next `SessionStart` in the same pane evicts the stale state file. The renderer also deduplicates by pane as a safety net.
- **Orphan cleanup**: Waiting sessions older than 30 minutes (configurable) are automatically removed by the active window's renderer.
- **Interrupt recovery**: When you press Esc to cancel Claude, the `Stop` hook doesn't fire. After ~60 seconds the `idle_prompt` notification fires, which transitions to white (auto-acknowledged, since you were present when you cancelled).
- **Atomic writes**: All state file writes use `tempfile` + `os.rename()` for POSIX atomicity — no partial reads.

## Configuration

Create `~/.config/claude-tmux-lights/config.json` to customize icons, colors, and behavior. All fields are optional — only include what you want to override:

```json
{
  "active_icon": "🟢",
  "waiting_icon": "⚠️",
  "viewed_icon": "⚪",
  "active_color": "colour46",
  "waiting_color": "colour226",
  "viewed_color": "colour252",
  "show_elapsed_time": true,
  "max_indicators_per_window": 5,
  "orphan_threshold_seconds": 1800
}
```

| Key | Default | Description |
|-----|---------|-------------|
| `active_icon` | `●` | Icon when Claude is working |
| `waiting_icon` | `◆` | Icon when Claude needs attention |
| `viewed_icon` | `○` | Icon when idle/acknowledged |
| `active_color` | `colour46` (green) | tmux color for active icon |
| `waiting_color` | `colour226` (yellow) | tmux color for waiting icon |
| `viewed_color` | `colour252` (light grey) | tmux color for viewed icon |
| `show_elapsed_time` | `true` | Show time since Claude stopped next to waiting icon |
| `max_indicators_per_window` | `5` | Max indicators per window (for multiple panes) |
| `orphan_threshold_seconds` | `1800` | Seconds before stale waiting sessions are cleaned up |

Config changes take effect on the next tmux status refresh (every `status-interval` seconds) — no restart needed.

## Requirements

- tmux (any modern version)
- Python 3.6+
- Claude Code with plugin support

No pip packages. No node modules. Zero external dependencies.

## License

[Apache License 2.0](LICENSE)
