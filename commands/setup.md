# claude-tmux-lights Setup

Configure tmux to display Claude Code session status indicators in the window tab bar.

## Instructions

1. **Check prerequisites**: Verify the user is running inside tmux (`$TMUX` is set). If not, inform them this plugin requires tmux.

2. **Create state directory**: Run `mkdir -p ~/.local/state/claude-tmux-lights` (or `$XDG_RUNTIME_DIR/claude-tmux-lights` if `$XDG_RUNTIME_DIR` is set).

3. **Determine the renderer path**: The renderer script is at `${CLAUDE_PLUGIN_ROOT}/scripts/tmux_render.py`. Resolve this to an absolute path.

4. **Detect tmux configuration style**:
   - Check if `~/.tmux-powerline.conf` exists and contains `window-status-format` — if so, edit that file.
   - Otherwise, check `~/.tmux.conf` for `window-status-format` — if found, edit that file.
   - If neither has `window-status-format` set, append new `setw` lines to `~/.tmux.conf`.

5. **Wire the renderer**: ONE renderer process runs per `status-interval` from `status-left` and publishes every window's indicator as the window option `@light`. The window formats read it with plain format expansion — nothing is spawned per window.

   a. Prepend the renderer call to `status-left` (it prints nothing, so it is invisible):

   **Before:**
   ```
   set -g status-left '#(some-existing-command)'
   ```

   **After:**
   ```
   set -g status-left '#(python3 /absolute/path/to/scripts/tmux_render.py --all)#(some-existing-command)'
   ```

   If `status-left` is not set anywhere, add `set -g status-left '#(python3 /absolute/path/to/scripts/tmux_render.py --all)'`.

   b. Append `#{@light}` to both `window-status-format` and `window-status-current-format`, just before the trailing `'`:

   **Before:**
   ```
   setw -g window-status-format ' #I:#W '
   setw -g window-status-current-format ' #I:#W '
   ```

   **After:**
   ```
   setw -g window-status-format ' #I:#W#{@light} '
   setw -g window-status-current-format ' #I:#W#{@light} '
   ```

   Replace `/absolute/path/to/scripts/tmux_render.py` with the actual resolved path from step 3.

   **Migrating from the legacy wiring**: if the window formats contain `#(python3 .../tmux_render.py #{window_id} #{window_active})`, replace that whole `#(...)` with `#{@light}` and add the `status-left` call from step (a). The legacy form spawned a Python process per window per tick and is the known cause of very high load averages on machines with many windows.

   **Important**: If `status-left` already contains `tmux_render.py --all` and the formats contain `#{@light}`, skip modification and inform the user it's already configured.

6. **Reload tmux config**: Suggest the user press their tmux prefix key followed by `R` (capital R) to reload, or run `tmux source-file ~/.tmux.conf`.

7. **Verify**: Explain what they should see:
   - Green dot (●) next to the window name when Claude is actively working
   - Yellow diamond (◆) with elapsed time when Claude stopped and they haven't viewed the window yet
   - White circle (○) when they've viewed/acknowledged the idle state
   - No indicator when no Claude session is running in that window

## Notes

- The renderer runs once per `status-interval` (e.g. every 5 seconds), regardless of window count. Hooks also publish lights immediately on state changes, so the interval only governs the elapsed-time counter and orphan cleanup.
- If the plugin is reinstalled and the path changes, re-run this setup command.
- Configuration options are in `~/.config/claude-tmux-lights/config.json` (optional).
