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

# Append renderer to window-status-format if not already present
RENDER_CALL="#(python3 ${RENDER_SCRIPT} #{window_id} #{window_active})"

for format_option in "window-status-format" "window-status-current-format"; do
    current_value=$(tmux show-options -gv "$format_option" 2>/dev/null)
    if [ -n "$current_value" ] && ! echo "$current_value" | grep -q "tmux_render.py"; then
        # Insert renderer call before the trailing space+quote
        # Typical format: ' #I:#W ' -> ' #I:#W#(...) '
        new_value=$(echo "$current_value" | sed "s/ *'$/${RENDER_CALL} '/; s/ *$/ ${RENDER_CALL} /")
        # If sed didn't change it (no trailing quote), just append
        if [ "$new_value" = "$current_value" ]; then
            new_value="${current_value}${RENDER_CALL}"
        fi
        tmux set-option -g "$format_option" "$new_value"
    fi
done
