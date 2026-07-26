# Codex CLI Support for claude-tmux-lights

**Date:** 2026-07-26
**Status:** Approved

## Goal

Show session status lights in tmux window tabs for Codex CLI sessions, exactly as
for Claude Code sessions, by feeding the existing state-file + renderer pipeline
from Codex's native hooks mechanism. Packaged as a full Codex plugin installable
from this repo.

## Background

Codex CLI (verified against 0.145.0, `hooks` feature stable and enabled) adopted
Claude Code's hook architecture nearly wholesale:

- Same event names: `SessionStart`, `SessionEnd`, `UserPromptSubmit`, `Stop`,
  `PreToolUse`, `PostToolUse`, `PreCompact`, `PostCompact`, `SubagentStart`,
  `SubagentStop` — plus `PermissionRequest`, which Claude Code does not have.
- Same stdin JSON: `hook_event_name`, `session_id`, `cwd`, `transcript_path`,
  plus event-specific fields.
- Same `hooks.json` shape: event → matcher groups → `{type: "command", command,
  timeout}` handlers.
- Plugin manifest at `.codex-plugin/plugin.json` (mirror of
  `.claude-plugin/plugin.json`), marketplace manifest at
  `.agents/plugins/marketplace.json`, local-path marketplace sources supported.
- Plugin hooks receive `PLUGIN_ROOT`, with `CLAUDE_PLUGIN_ROOT` as a
  compatibility alias.

Differences that shape the design:

- **No async hooks.** Codex parses an `async` option but rejects/ignores it;
  all hooks run synchronously (default timeout 600s; `SessionEnd` capped ~1s).
- **No `Notification` event.** Permission blocking surfaces as
  `PermissionRequest` instead; there is no idle-prompt equivalent.
- **Hook trust.** Non-managed command hooks require one-time approval via
  `/hooks` inside Codex, recorded against the hook's SHA.

## Design

### Layout — one repo, dual plugin

```
claude-tmux-lights/
├── .claude-plugin/plugin.json        existing (Claude manifest)
├── .claude-plugin/marketplace.json   existing (Claude marketplace)
├── .codex-plugin/plugin.json         NEW: Codex manifest; declares
│                                     "hooks": "./codex-hooks/hooks.json"
├── .agents/plugins/marketplace.json  NEW: Codex marketplace; plugin source "./"
├── codex-hooks/hooks.json            NEW: Codex-flavored hook manifest
├── hooks/hooks.json                  existing Claude manifest — untouched
└── scripts/                          shared, unchanged pipeline:
    ├── hook_handler.py               (small additions, below)
    ├── tmux_render.py                (no changes)
    └── utils.py                      (no changes)
```

Separate hook manifests per CLI because the flavors differ concretely: Claude's
uses `async: true` and `Notification` matchers; Codex's registers
`PermissionRequest` and must omit async flags. Both invoke the same
`scripts/hook_handler.py` via `${CLAUDE_PLUGIN_ROOT}` (alias works in both).

### codex-hooks/hooks.json

Registers, all `type: "command"`, no `async`, command
`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/hook_handler.py"`:

- `SessionStart` — creates state file (idle/white), evicts same-pane ghosts
- `SessionEnd` — deletes state file (unlink happens before tmux refresh, so
  Codex's ~1s SessionEnd cap cannot leave stale state)
- `UserPromptSubmit` — flips to active (green)
- `Stop` — flips to waiting (yellow until viewed)
- `PostToolUse` — waiting → active recovery; recreates missing state files
- `PermissionRequest` — flips to waiting (yellow) while blocked on approval

### scripts/hook_handler.py changes (the only code changes)

1. New `PermissionRequest` branch with identical semantics to the existing
   `Notification`/`permission_prompt` path: write state `waiting`, recreate the
   file if missing. The existing `PostToolUse` branch already restores `active`
   when work resumes.
2. Extend `CLAUDE_COMMAND_RE` so the cwd→pane fallback recognizes Codex panes
   as clients: match `codex` and `codex-*` process names in addition to
   `claude*` and bare version strings.

No renderer, state-schema, or tmux changes. State files are CLI-agnostic; a
pane runs one client at a time, so SessionStart pane-eviction and pane-liveness
cleanup work unchanged across both CLIs.

### Install flow (documented in README)

```
codex plugin marketplace add ~/src/claude-tmux-lights
codex plugin add claude-tmux-lights@claude-tmux-lights
```

then approve the hooks once via `/hooks` inside Codex. tmux setup is shared
with the Claude plugin (window-status-format already points at the repo's
renderer).

## Error handling

- Unknown/absent `TMUX_PANE` in hook env: existing fallback chain (recorded
  pane → cwd match → give up silently). Codex hooks are host-side child
  processes and are expected to inherit `TMUX_PANE`; verified empirically
  during implementation.
- Hook not yet trusted: Codex simply doesn't run it — no lights until `/hooks`
  approval; no error states in our code.
- Sync execution budget: the handler's tmux subprocess calls each carry 5s
  timeouts and the hot path is ~tens of ms; well inside Codex's 600s default.
  `SessionEnd`'s 1s cap is safe because unlink precedes the refresh call.

## Testing

Extend `tests/test_lights.py` (same fake-tmux harness):

1. `PermissionRequest` with existing active state → waiting.
2. `PermissionRequest` with missing state file → file created (self-heal).
3. cwd fallback picks a `codex`-named pane (e.g. `codex-aarch64-a`) over a
   shell in the same directory.
4. Manifest sanity: `codex-hooks/hooks.json` parses, contains only
   Codex-supported events, and no `async` flags.

Live verification: install the plugin into the user's Codex, trigger a session
in a Codex tab (windows 3 / 9), confirm the light appears, transitions on
prompt/stop, and clears on exit.

## Empirical unknowns (probe during implementation, in order)

1. **In-place execution:** whether local-marketplace plugins run from the repo
   (evidence: bundled plugins execute from their marketplace directory). If
   Codex copies instead, repo edits need `codex plugin` reinstall to go live —
   document whichever holds.
2. **Manifest `hooks` path:** whether `.codex-plugin/plugin.json` honors a
   `"hooks"` field (docs say manifest-specified paths exist). Fallback: plugin
   source `"./codex"` subdirectory with its own `hooks/hooks.json` and
   `../scripts` command paths.
3. **`TMUX_PANE` passthrough:** verified with a cheap `codex exec` probe before
   declaring done.

## Out of scope

- Idle-prompt / interrupt-recovery lights for Codex (no equivalent event).
- Codex daemon/cloud-hosted sessions (`codex cloud`, app-server) — the cwd
  fallback may incidentally cover them, but they are not a target.
- Distribution beyond this machine (publishing the marketplace for others) —
  the packaging supports it, but docs target local install.
