# Context-Injection Hook for Coding Agents — SPEC v1

**Status:** Draft · Single-implementer reference · Open to alternative implementations
**Audience:** Coding agents implementing this spec for a target codebase, and humans reviewing.

The key words **MUST**, **MUST NOT**, **SHOULD**, **SHOULD NOT**, and **MAY** in this document are to be interpreted as described in [RFC 2119](https://www.rfc-editor.org/rfc/rfc2119).

---

## 1. Problem

Coding agents lose to ambient drift. Time of day, auth expiry, sibling-repo activity, modified architecture docs, background jobs, mood — all of these change between the agent's last verified fact and the user's next prompt. Without an injection mechanism, the agent either (a) re-derives the state every prompt at high token cost, or (b) operates on a stale model and ships wrong assumptions.

A **context-injection hook** runs on every user prompt, computes a small set of facts about the runtime environment, and prepends them to the prompt as the agent sees it. Done right: < 200 tokens per prompt, prevents whole categories of failure, costs less than the failures it prevents.

This SPEC defines the contract a hook implementation **MUST** satisfy to interoperate with the workflow patterns described in §6.

## 2. Terminology

- **Hook** — Program invoked by the agent harness on each user prompt, whose stdout is injected into the agent's context before the prompt is processed.
- **Field** — A single named fact (or absence of fact) emitted by the hook (e.g. `time`, `gcloud_auth_ttl`).
- **Cadence** — Minimum interval between two emissions of the same field.
- **Window** — A field's emission rule: always-on, every-N-seconds, on-change, or on-condition.
- **State file** — Persistent JSON the hook reads/writes to track per-field "last shown" timestamps and values.
- **Target codebase** — The codebase the implementing agent is being installed into.

## 3. Normative requirements

### 3.1 Invariants

- **3.1.1** The hook **MUST** complete in under 500ms in the common case (no expired caches). Implementations **SHOULD** parallelize independent field computations.
- **3.1.2** The hook **MUST NOT** block the user prompt on a network call without a strict timeout (≤ 3s per call, fail-soft).
- **3.1.3** Every field **MUST** be independently disable-able via config.
- **3.1.4** Every field **MUST** have a sensible default (see §4).
- **3.1.5** Fields handling personal data (now-playing, recent files, location) **MUST** default to disabled and **MUST** carry a privacy note in the implementation.
- **3.1.6** A field with no information to emit (e.g. drift field when all repos clean) **MUST** stay silent — no `field: ok` lines unless explicitly configured.
- **3.1.7** The hook **MUST** be safe to invoke when its dependencies (gcloud, git, etc.) are missing. Missing tools **MUST** cause the field to stay silent, never to crash the hook.

### 3.2 Output format

- **3.2.1** When invoked by the agent harness (stdin contains a JSON payload), output **MUST** be a JSON object on stdout matching the harness's hook output contract. For Claude Code:
  ```json
  {"hookSpecificOutput": {"hookEventName": "<event>", "additionalContext": "<text>"}}
  ```
  The `additionalContext` string is what gets injected into the agent's context window.
- **3.2.2** When invoked manually (no stdin / a TTY), output **MAY** be plain text on stdout for human readability during smoke tests.
- **3.2.3** Output **SHOULD** be one line per field when fields are short, with a compact bracket-prefixed style (e.g. `[⏱ Monday afternoon, 3:14 PM EDT]`). Implementations **MAY** combine multiple short fields on one line.
- **3.2.4** Output **MUST NOT** contain ANSI escapes, control characters, or markdown that would render as something other than its literal text.
- **3.2.5** Total output for a single prompt **SHOULD** stay under 600 characters (~150 tokens) in the common case.

### 3.3 Cadence

- **3.3.1** Each field **MUST** declare a default cadence (see §4).
- **3.3.2** Cadence **MUST** be overridable per-field via configuration.
- **3.3.3** A field with cadence `0` **MUST** emit every prompt (subject to its own enable flag).
- **3.3.4** A field with cadence `>0` **MUST** consult the state file and emit only if `now - last_shown >= cadence`.
- **3.3.5** A field with `show_on_change: true` **MUST** emit when its computed value differs from `last_value` in state, even if cadence has not elapsed.

### 3.4 Configuration

- **3.4.1** Configuration **MUST** support a YAML or inline-constants form. Separate config files are **OPTIONAL** (single-file distribution **SHOULD** be supported for gist-style portability).
- **3.4.2** Every config key **MUST** have an environment variable override of the form `CLAUDE_HOOK_<FIELD>_<KEY>` (uppercased, snake_case). Env vars **MUST** take precedence over file/constant config.
- **3.4.3** Unknown fields in config **MUST** be ignored (forward compatibility).

### 3.5 State

- **3.5.1** State **MUST** be keyed by the `session_id` from the hook payload, stored as `$XDG_CACHE_HOME/claude-hook/<session_id>.json` (defaulting to `~/.cache/claude-hook/<session_id>.json`). Cadences are therefore **per-session**: a new session **MUST** start with empty state and **MUST** emit every enabled field on its first prompt.
- **3.5.2** State **MUST** be safe to delete; deletion **MUST** simply re-emit all gated fields on next run.
- **3.5.3** Implementations **MUST** garbage-collect state files older than a configurable TTL (default 7 days) on each invocation to keep the state directory bounded.
- **3.5.4** When `session_id` is absent (manual smoke tests, hooks-tools that don't pass payloads), implementations **SHOULD** fall back to a sentinel filename (`nosession.json`) so the hook stays usable in non-harness contexts.
- **3.5.5** Implementations **MAY** use file locking when concurrent invocations within the same session are expected.

## 4. Field catalog (normative)

Each field is named, has a default cadence, a default enabled state, and a defined output shape.

| Field | Default cadence | Default enabled | Privacy-sensitive |
|---|---|---|---|
| `time` | every prompt (0s) | true | no |
| `gcloud_auth_ttl` | 600s | true | no |
| `repo_drift` | 300s | true | no |
| `watched_files` | 300s | true (empty list) | no |
| `bg_jobs` | every prompt (0s) | true | no |
| `gcloud_context` | 1800s | true | no |
| `health` | 600s | true (empty list) | no |
| `spotify` | every prompt (0s) | **false** | **yes** |
| `screenshots` | 300s | **false** | **yes** |

### 4.1 `time`
Wall-clock time as a **human-grounded phrase** plus a machine-readable timestamp.
**MUST** include semantic period (morning / afternoon / evening / night) so agents do not need to translate `15:14 → 3pm afternoon` (a known failure mode).
Suggested format: `now: Sunday afternoon, 3:14 PM EDT (utc=2026-05-25T19:14:55Z, session=28m)`.

### 4.2 `gcloud_auth_ttl`
Best-effort indicator of whether `gcloud` will fail on next use due to expired credentials.
Implementations **SHOULD** invoke `gcloud auth print-access-token --quiet` with a short timeout; non-zero exit **MUST** surface as a `REAUTH REQUIRED` warning. Silent when healthy is acceptable.

### 4.3 `repo_drift`
For every git repository under a configured `src_root`:
emit a one-line summary if the repo has uncommitted changes, untracked files, OR unpushed/unpulled commits.
**MUST** stay silent for clean repos. Suggested format: `drift: it-infra ⚠ wif/ untracked · platform ahead 1`.

### 4.4 `watched_files`
For each file in a configured `paths` list: emit relative path + mtime age if the file exists.
Implementations **MAY** flag files modified since the agent's last `Read` of them (requires PostToolUse cooperation; out of scope for v1).

### 4.5 `bg_jobs`
Count of background tasks the agent has spawned that are still alive.
Harness-specific. For Claude Code, implementations **MAY** scan `/private/tmp/claude-*/*/tasks/*.output` and infer aliveness from recent mtimes. **MUST** stay silent when zero.

### 4.6 `gcloud_context`
Active gcloud account and project, from `gcloud config get-value`.
Useful to catch wrong-project goofs. Suggested format: `gcloud: user@example.com → project-id`.

### 4.7 `health`
For each configured GCP project (or analogous cloud-status source): count of non-RUNNING workloads.
**MUST** stay silent when nothing is wrong. Implementations **SHOULD** time-box per-project calls aggressively (≤ 2s) and degrade gracefully.

### 4.8 `spotify` (privacy-sensitive)
Currently-playing or last-played track. macOS via `osascript`; Linux via `playerctl`; others as appropriate.
Treated as a mood signal by the agent. **MUST** default to disabled.
**MUST NOT** be enabled without an explicit user opt-in. Implementations **MUST** include a comment in the source explaining the privacy implication.

### 4.9 `screenshots` (privacy-sensitive)
Last N screenshot file paths (default 3) from a configured directory.
Lets the agent pre-empt screenshot pastes by reading the file directly. **MUST** default to disabled.
Privacy concerns identical to `spotify`: a list of filenames can reveal what the user was just looking at, including across unrelated conversations or apps.

## 5. Adaptation guidance (for the implementing agent)

The implementing agent **SHOULD** inspect its target codebase before writing default config values. Specifically:

- **`src_root` for `repo_drift`**: locate the directory containing the user's git repos. Heuristics, in order:
  1. The `cwd` if it is itself a git repo's parent.
  2. A `src/` directory in the workspace root.
  3. A common ancestor of recently-modified git repos.
- **`paths` for `watched_files`**: look for high-value documents in the target codebase. Heuristics: `ARCHITECTURE.md`, `CLAUDE.md`, `AGENTS.md`, `SPEC*.md`, files at the root of every IaC repo (e.g. `*.tf` directly in repo root), and any file the target's own `CLAUDE.md` or `AGENTS.md` explicitly names as load-bearing.
- **`projects` for `health`**: read from the target's IaC if present (e.g. Terraform `google_project` resources), the cloud provider's project list filtered by `lifecycleState=ACTIVE`, or skip if no cloud is in scope.
- **`gcloud_context`**: enable iff `gcloud` is present on the host.

The implementing agent **MUST NOT** enable `spotify` or `screenshots` by default regardless of what it discovers.

## 6. Installation scope (the one question to ask)

The implementing agent **MUST** ask the user one (and only one) clarifying question before installing:

> Where should this hook be installed?
> 1. **Project** — only fires for this project (`<project>/.claude/settings.json`)
> 2. **Folder** — fires for every project under a chosen ancestor folder
> 3. **Global** — fires for every Claude Code session on this machine (`~/.claude/settings.json`)

All other decisions (which fields, what cadences, what `src_root`) **SHOULD** be inferred from the target codebase and emitted as a config the user can review and edit. The agent **SHOULD NOT** ask the user to enumerate fields or set cadences — sensible defaults exist and customization happens later.

## 7. Security & privacy

- **7.1** Privacy-sensitive fields (§4.8, §4.9) **MUST** default disabled and **MUST** carry a source comment.
- **7.2** Implementations **MUST NOT** transmit field values off-host (no telemetry, no remote reporting). The hook's only output **MUST** be stdout to the local agent.
- **7.3** State file **MUST** be created with mode `0600` (readable only by the user).
- **7.4** Fields that read cloud-provider state **MUST** use the user's existing credentials (e.g. `gcloud`) and **MUST NOT** require new credential issuance.

## 8. Non-goals

- This SPEC is **not** a logging system. Failures inside the hook **MUST** be swallowed silently. Debugging is done by invoking the hook directly outside the agent.
- This SPEC does **not** define the agent harness's hook configuration syntax. See the harness documentation (e.g. Claude Code `~/.claude/settings.json`).
- This SPEC does **not** define field rendering for non-text agent UIs (image, voice, etc.). Out of scope.

## 9. Reference implementation

A single-file Python implementation is published as a companion gist (see top of file in the impl for URL). The reference implementation **MAY** be substituted by any implementation that conforms to §3 and §4.

## 10. Open extensions (non-normative)

- **Stale-read flag**: pair this hook with a PostToolUse hook on `Read` that timestamps which files the agent has opened; the `watched_files` field then surfaces a `RE-READ` marker when a watched file has been modified since the agent's last read.
- **Per-agent identification**: when multiple coding agents share a machine, allow each to identify itself via an env var (`CLAUDE_AGENT_ID`) so `bg_jobs` and `repo_drift` can attribute work across sessions.
- **Backstage `/api/status`**: when a target has an internal developer portal exposing service status, `health` can be sourced from there instead of the cloud provider directly. The implementing agent **SHOULD** prefer the portal source when discoverable.
