#!/usr/bin/env bash
# TPM (Tmux Plugin Manager) entry point for claude-tmux-lights.
#
# When installed via TPM, this script auto-configures tmux to display
# Claude Code session status indicators in the window tab bar.

CURRENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RENDER_SCRIPT="${CURRENT_DIR}/scripts/tmux_render.py"

# Check if python3 is available
if ! command -v python3 &>/dev/null; then
    return 0 2>/dev/null || exit 0
fi

# Create state directory
if [ -n "$XDG_RUNTIME_DIR" ]; then
    STATE_DIR="$XDG_RUNTIME_DIR/claude-tmux-lights"
else
    STATE_DIR="$HOME/.local/state/claude-tmux-lights"
fi
mkdir -p "$STATE_DIR"

RENDER_CALL="#(python3 ${RENDER_SCRIPT} #{window_id} #{window_active})"

for format_option in "window-status-format" "window-status-current-format"; do
    current_value=$(tmux show-options -gv "$format_option" 2>/dev/null)

    # Skip if empty or already configured
    if [ -z "$current_value" ]; then
        continue
    fi
    if echo "$current_value" | grep -q "tmux_render.py"; then
        continue
    fi

    # Strip trailing whitespace, append renderer call, re-add trailing space
    trimmed="${current_value% }"
    new_value="${trimmed}${RENDER_CALL} "

    tmux set-option -g "$format_option" "$new_value"
done
