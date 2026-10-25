#!/usr/bin/env bash
# TPM (Tmux Plugin Manager) entry point for claude-tmux-lights.
#
# When installed via TPM, this script auto-configures tmux to display
# Claude Code session status indicators in the window tab bar.
#
# Wiring: ONE renderer process per status-interval runs from status-left and
# publishes each window's indicator as the window option @light; the window
# formats read it with plain format expansion (#{@light}), so no process is
# spawned per window.

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

RENDER_CALL="#(python3 ${RENDER_SCRIPT} --all)"
LEGACY_CALL="#(python3 ${RENDER_SCRIPT} #{window_id} #{window_active})"
LIGHT='#{@light}'

# 1. The single renderer, prepended to status-left (it prints nothing).
status_left=$(tmux show-options -gv status-left 2>/dev/null)
case "$status_left" in
    *"tmux_render.py"*) ;;
    *) tmux set-option -g status-left "${RENDER_CALL}${status_left}" ;;
esac

# 2. Per-window formats read #{@light}; migrate the legacy per-window call.
for format_option in "window-status-format" "window-status-current-format"; do
    current_value=$(tmux show-options -gv "$format_option" 2>/dev/null)

    if [ -z "$current_value" ]; then
        continue
    fi
    case "$current_value" in
        *"$LIGHT"*) continue ;;
        *"tmux_render.py"*)
            new_value="${current_value/"$LEGACY_CALL"/$LIGHT}"
            ;;
        *)
            # Strip trailing whitespace, append the option read, re-add trailing space
            trimmed="${current_value% }"
            new_value="${trimmed}${LIGHT} "
            ;;
    esac

    tmux set-option -g "$format_option" "$new_value"
done
