# claude-tmux-lights

Show Claude Code session status indicators in your tmux window tabs.

When running Claude Code inside tmux, each window tab shows:
- **Green dot** (●) — Claude is actively working
- **Yellow diamond** (◆) + elapsed time — Claude stopped, you haven't looked yet
- **White circle** (○) — You've viewed/acknowledged the idle session

Multiple Claude sessions in the same tmux window each get their own indicator.

## Installation

### As a Claude Code plugin

```bash
claude /install-plugin /path/to/claude-tmux-lights
```

Then run the setup command inside Claude Code:

```
/claude-tmux-lights:setup
```

### With TPM (Tmux Plugin Manager)

Add to your `~/.tmux.conf`:

```bash
set -g @plugin 'mpercy/claude-tmux-lights'
```

Press `prefix + I` to install. The plugin auto-configures your window-status-format.

### Manual

1. Clone this repo somewhere persistent:
   ```bash
   git clone https://github.com/mpercy/claude-tmux-lights.git ~/.tmux/plugins/claude-tmux-lights
   ```

2. Add to your tmux config (e.g. `~/.tmux.conf`):
   ```bash
   setw -g window-status-format ' #I:#W#(python3 ~/.tmux/plugins/claude-tmux-lights/scripts/tmux_render.py #{window_id} #{window_active}) '
   setw -g window-status-current-format ' #I:#W#(python3 ~/.tmux/plugins/claude-tmux-lights/scripts/tmux_render.py #{window_id} #{window_active}) '
   ```

3. Reload tmux: `prefix + R` or `tmux source-file ~/.tmux.conf`

## How it works

Claude Code hooks write per-session JSON state files to `~/.local/state/claude-tmux-lights/`. The tmux renderer reads these files every `status-interval` seconds (default 2s) and outputs formatted indicator strings.

### State transitions

| Event | Display |
|-------|---------|
| Claude starts working | Green ● |
| Claude stops (response complete) | Yellow ◆ + time |
| You switch to that tmux window | White ○ |
| You send a new prompt | Green ● |
| You interrupt (Esc), ~60s passes | White ○ (auto-acknowledged) |
| Session ends | Indicator removed |

### Known limitation

When you interrupt Claude with Esc/Ctrl+C, the `Stop` hook [does not fire](https://github.com/anthropics/claude-code/issues/9516). The green dot stays for ~60 seconds until the `idle_prompt` notification fires, at which point it transitions directly to white (since you were obviously present when you cancelled).

## Configuration

Create `~/.config/claude-tmux-lights/config.json` (optional):

```json
{
  "active_icon": "●",
  "waiting_icon": "◆",
  "viewed_icon": "○",
  "active_color": "colour46",
  "waiting_color": "colour226",
  "viewed_color": "colour252",
  "orphan_threshold_seconds": 1800,
  "show_elapsed_time": true,
  "max_indicators_per_window": 5
}
```

## Requirements

- tmux
- Python 3.6+
- Claude Code with plugin support

## License

Apache License 2.0
