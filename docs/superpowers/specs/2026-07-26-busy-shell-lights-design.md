# Busy-Shell Detection for Idle Lights

**Date:** 2026-07-26
**Status:** Approved (slim variant — daemon-hosted sessions only)

## Goal

A session that has finished its turn but still has work running should show
green (●), not idle white (○). Observed case: window 5's daemon-hosted
session sat white while a background `local_bash` task ran for hours.

## Rule

- Only the **waiting + acknowledged** (white ○) state upgrades to green.
- **Unviewed yellow ◆ always wins** — if the session stopped to ask the user
  something, attention beats busy.
- Renderer-side inference only: no hook or state-file changes (no hook event
  exists for background-task lifecycle).

## Detection (the slim cut)

For each waiting+viewed session in the rendered window, read
`~/.claude/jobs/<session_id[:8]>/state.json` (Claude Code's daemon job
record). Busy iff the file exists, its `sessionId` equals the session's full
id (collision guard), and `state == "working"` or `inFlight.tasks > 0`.

- One small JSON read per white session per refresh; nothing at all when the
  window has no white sessions. No subprocesses.
- Malformed/missing file → not busy (use `read_json_safe`).
- Deliberately **excluded** (YAGNI, cost): `ps`-tree walking for pane-hosted
  sessions' background shells. Pane-hosted sessions self-correct when the
  task completes and wakes the session; revisit with a TTL-cached `ps`
  snapshot only if a real pane-hosted case bothers the user.

## Implementation

`scripts/tmux_render.py` only:
- New helper `daemon_job_busy(session_id)` implementing the check above;
  jobs dir is `os.path.expanduser("~/.claude/jobs")` (tests isolate via the
  harness's fake `HOME`).
- In `render_indicator`'s waiting branch: when `viewed_ts >= state_changed_ts`
  and `daemon_job_busy(session_id)`, render the active icon/color instead of
  the viewed icon. No elapsed-time suffix (matches active rendering).

## Testing

Extend `tests/test_lights.py` (existing harness; fake jobs dirs under the
test `HOME`):
1. white + jobs `working`/`tasks>0` → green
2. white + jobs `idle`/`tasks: 0` → white
3. white + no jobs dir → white
4. unviewed yellow + busy jobs → still yellow (precedence)
5. jobs file whose `sessionId` mismatches → white (collision guard)

## Out of scope

- Pane-hosted background-shell detection (`ps` walking).
- Codex sessions (no daemon job records; no behavior change).
- New icons/config knobs.
