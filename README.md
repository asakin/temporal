# claude-context-hook

A `UserPromptSubmit` hook for [Claude Code](https://docs.claude.com/en/docs/claude-code/overview) that injects ambient context — time, auth health, repo drift, and a few cool-tax fields — into every prompt the model sees.

v2 of [the original time-awareness gist]([https://gist.github.com/asakin/...](https://gist.github.com/asakin/e4225721bb8f16dd6bc34f4eec5499f9)). Same single-file Python, no dependencies. New fields, per-field cadences, a config block, and a [SPEC.md](./SPEC.md) describing the contract.

## Why

Coding agents lose to ambient drift. Time of day, whether the gcloud token is about to die, what your parallel agents are doing in sibling repos, what files have been touched since the agent last read them — all of these change between turns, and the model can't see them unless something puts them there.

This hook puts them there. Each field is independently toggleable, throttled to a sensible cadence, and degrades silently when its data source is missing.

## Quick install (let your agent do it)

```
"Read https://github.com/asakin/claude-context-hook/blob/main/SPEC.md
and install temporal.py for me. Default cadences are fine.
Point repo_drift's src_root at <YOUR_SRC_DIR>."
```

The agent will ask one question — project, folder, or global scope — and then drop the script in, edit `settings.json`, and you're done.

## Manual install

```bash
mkdir -p ~/.claude/hooks
curl -L https://raw.githubusercontent.com/asakin/claude-context-hook/main/temporal.py \
  -o ~/.claude/hooks/temporal.py
chmod +x ~/.claude/hooks/temporal.py
```

Then in `~/.claude/settings.json`:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {"type": "command", "command": "python3 ~/.claude/hooks/temporal.py"}
        ]
      }
    ]
  }
}
```

Open a new Claude Code session and the bracketed context lines will appear above every prompt.

## What it injects

| Field | Default | What |
|---|---|---|
| `time` | on | `⏱ Monday afternoon, 3:14 PM EDT` — pre-translated phrase + machine timestamp |
| `gcloud_auth_ttl` | on | `🔑 gcloud-auth: ⚠ REAUTH REQUIRED` when `gcloud auth print-access-token` fails |
| `repo_drift` | on | `⚠ drift: it-infra 2m 1u ↑1` per dirty git repo under `src_root` |
| `watched_files` | on | `📄 watched: ARCHITECTURE.md (18m ago)` for files you've named |
| `bg_jobs` | on | `⏳ bg: 1 job(s) (poll-ci)` when Claude Code background tasks are alive |
| `gcloud_context` | on | `☁ gcloud: you@example.com → my-project` (only when it changes / every 30 min) |
| `health` | on (empty) | `🚨 outages: <project>: <vm-name> STOPPING` for non-RUNNING GCE instances |
| `spotify` | **off** | `♪ track — artist` — mood signal |
| `screenshots` | **off** | `📷 last screenshots: …` so the agent can preempt your paste |

Each field has its own cadence. Empty lists / missing tools = silent. No dependencies beyond Python 3.9+; PyYAML is opt-in for sidecar config but not required.

## Configuration

Three layers, last wins:

1. Defaults baked into `temporal.py` (see `DEFAULT_CONFIG` at the top of the file).
2. `~/.claude/hooks/temporal.yaml` if it exists and PyYAML is installed.
3. Environment variables: `CLAUDE_HOOK_<FIELD>_<KEY>` (uppercased). E.g.:

```bash
export CLAUDE_HOOK_REPO_DRIFT_CADENCE_SEC=120
export CLAUDE_HOOK_HEALTH_PROJECTS=my-prod-1,my-prod-2
export CLAUDE_HOOK_SPOTIFY_ENABLED=1
```

To make a one-line tweak without touching the script, an env-var override is enough.

## Privacy

`spotify` and `screenshots` default OFF. They surface personal data (what you're listening to, what's on your desktop) to whichever coding agent is in the current session, including across unrelated conversations. Enable only on hosts where you accept that tradeoff. The script never transmits anything off-host — output is stdout to the local agent only.

State is **per-session** — one JSON file per Claude Code session at `~/.cache/claude-hook/<session_id>.json`, created with mode `0600`, garbage-collected after 7 days (override via `CLAUDE_HOOK_STATE_TTL_DAYS`). New session = fresh state = every enabled field fires on its first prompt. Cadences throttle subsequent runs within the same session.

## SPEC.md — for agents (and humans) who want their own version

The interesting half of this repo isn't the script — it's the [SPEC.md](./SPEC.md). RFC 2119 normative spec, field catalog, cadence model, adaptation guidance for the implementing agent to introspect *its* codebase and pick sensible defaults.

If you're a coding agent and someone said "build this for our codebase," read the SPEC, write your own version, ask one question (installation scope), and ship. The instructions to build the thing are the artifact; this script is the existence proof.

## Token cost

- All clean, defaults: ~80 tokens per prompt.
- Stuff actually firing (drift caught, auth expired, outage live): ~150–250 tokens.
- Worst plausible case (every field maxed): ~400 tokens.

Negligible against working context. Pays for itself the first time it prevents a wrong assumption.

## License

[MIT](./LICENSE) © 2026 Ariel Sakin
