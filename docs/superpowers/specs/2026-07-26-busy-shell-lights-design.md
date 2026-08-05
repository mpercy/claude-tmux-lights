# Busy-Green Upgrade and Stale-Active Demotion

**Date:** 2026-07-26 (amended 2026-08-05)
**Status:** Approved

## Goals

1. **Busy-green:** a session that finished its turn but still has daemon-hosted
   work running shows green (●), not idle white (○). Observed: a daemon-hosted
   session sat white while a background `local_bash` task ran.
2. **Stale-active demotion:** a session whose turn-end event was missed (e.g.
   laptop closed mid-turn; the agent lived on, idle) must not stay green
   forever. Observed: a headless Codex agent's file sat `active` for 3 days,
   pinned to a pane it had matched by cwd.

## Rules

- **Unviewed yellow ◆ always wins** — attention beats busy.
- Only **waiting + acknowledged** (white ○) upgrades to green when busy.
- Only sessions on **non-client panes** (pane's foreground command does not
  match claude/codex/version-string) are ever demoted by time. A pane-hosted
  client keeps its state indefinitely — no timer applies to it, ever.
- **No new reap timers.** Cleanup remains: SessionEnd, pane death, same-pane
  SessionStart eviction, and the existing pane-list-unreadable fallback.
  A dead headless agent's white dot persists until one of those fires
  (accepted residual: one-off agents in immortal panes need manual cleanup).

## Detection

`~/.claude/jobs/<session_id[:8]>/state.json` (Claude Code daemon job record):
- **Record exists** (with `sessionId` equal to the full session id — collision
  guard) → session is alive: exempt from demotion.
- **Busy** iff `state == "working"` or `inFlight.tasks > 0` → white upgrades
  to green.
- Missing/malformed file (`read_json_safe`) → not alive, not busy.
- Cost: one small JSON read per white or demotion-candidate session per
  refresh; nothing when the window has none. No subprocesses. The `ps`-tree
  walk for pane-hosted background shells stays excluded (YAGNI; pane-hosted
  sessions self-correct when the task completes and wakes the session).

## Behavior changes

1. **Busy-green** (`render_indicator`): waiting + `viewed_ts >=
   state_changed_ts` + busy → render active icon/color, no elapsed suffix.
2. **Demotion** (cleanup pass in `tmux_render.py` main): a session with
   `state == "active"`, `state_changed_ts` older than
   `orphan_threshold_seconds` (1800s), whose pane's current command is not
   client-like, and with no live jobs record → rewrite the file as `waiting`
   + `waiting_since_ts = now`, and mark it viewed (white, acknowledged — the
   session idled out; nobody needs an alert). Renderer already writes
   viewed.json and unlinks session files; demotion uses `atomic_write_json`.
   Race with a live hook write is benign: demotion only triggers after 30
   silent minutes, and last-writer-wins resolves the once-in-an-epoch overlap
   in favor of whichever reflects newer reality.

## Implementation notes

- `CLIENT_COMMAND_RE` moves from `scripts/hook_handler.py` to
  `scripts/utils.py` (both scripts need it; renderer must judge
  client-likeness of pane commands).
- `get_live_pane_ids()` grows into `get_pane_commands()` returning
  `{pane_id: command}` from one `tmux list-panes -a` call (`None` on
  failure/empty, preserving the existing "couldn't check" semantics); the
  pane-death reap uses its keys, demotion its values.
- New helper `daemon_job_record(session_id)` in `tmux_render.py` returning
  the parsed record or `None`; busy/alive derived from it.

## Testing

Existing harness (fake tmux + fake `HOME`; jobs dirs under test `HOME`):
1. white + jobs `working`/`tasks>0` → green
2. white + jobs `idle`, `tasks: 0` → white
3. white + no jobs record → white
4. unviewed yellow + busy jobs → still yellow
5. jobs `sessionId` mismatch → treated as absent (white; demotable)
6. stale-active (31 min) on non-client pane, no jobs → demoted: file waiting,
   viewed entry added, renders white
7. stale-active on client-like pane → untouched (still green)
8. stale-active on non-client pane WITH live jobs record → untouched
9. fresh-active (1 min) on non-client pane → untouched
10. pane-death reap and pane-list-unreadable fallback still pass (existing
    tests unchanged)

## Out of scope

- `ps`-tree busy detection for pane-hosted sessions.
- Reap timers for waiting files on live panes (none exist; none added).
- Codex-side liveness records (none known; heuristics above apply).
