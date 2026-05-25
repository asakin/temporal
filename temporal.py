#!/usr/bin/env python3
"""
temporal.py — Context-injection hook for Claude Code

Implements the Context-Injection Hook SPEC v1 (companion gist).
Single file, stdlib only, every field independently toggleable, sensible defaults.

INSTALL
-------
1. Save this file somewhere stable, e.g.:
     ~/.claude/hooks/temporal.py

2. Make it executable:
     chmod +x ~/.claude/hooks/temporal.py

3. Wire it into Claude Code via ~/.claude/settings.json (or project-scoped
   .claude/settings.json — see SPEC §6 for scope guidance):

     {
       "hooks": {
         "UserPromptSubmit": [
           {
             "hooks": [
               {"type": "command",
                "command": "python3 ~/.claude/hooks/temporal.py"}
             ]
           }
         ]
       }
     }

4. (Optional) Override defaults via the CONFIG block below, a sibling
   ~/.claude/hooks/temporal.yaml, or environment variables:
     CLAUDE_HOOK_REPO_DRIFT_CADENCE_SEC=120
     CLAUDE_HOOK_SPOTIFY_ENABLED=1
     CLAUDE_HOOK_HEALTH_PROJECTS=my-prod-1,my-prod-2

5. Privacy-sensitive fields (spotify, screenshots) default to disabled.
   Enable only on a machine where you accept that the agent in your
   coding session will see what you're listening to / what you just
   screenshotted (including from unrelated conversations).
"""

from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Optional YAML support (graceful degradation if PyYAML isn't installed)
try:
    import yaml  # type: ignore
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False


# ─────────────────────────────────────────────────────────────────────────────
# CONFIG — edit defaults here, or override via env vars / sidecar yaml.
# Every key has an env override: CLAUDE_HOOK_<FIELD>_<KEY> (uppercase).
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_CONFIG: dict = {
    "time": {
        "enabled": True,
        "cadence_sec": 0,            # every prompt
    },
    "gcloud_auth_ttl": {
        "enabled": True,
        "cadence_sec": 600,          # every 10 min
        "show_when_healthy": False,  # silent when fine; only warn on REAUTH
    },
    "repo_drift": {
        "enabled": True,
        "cadence_sec": 300,          # every 5 min
        "src_root": "~/src",         # adapt per machine; see SPEC §5
        "include_clean": False,      # silent when all repos are clean
    },
    "watched_files": {
        "enabled": True,
        "cadence_sec": 300,
        "paths": [
            # Populate from your codebase; SPEC §5 has discovery heuristics.
            # Examples:
            # "~/src/it-infra/ARCHITECTURE.md",
            # "~/.claude/CLAUDE.md",
        ],
    },
    "bg_jobs": {
        "enabled": True,
        "cadence_sec": 0,            # every prompt
        "harness_glob": "/private/tmp/claude-*/*/tasks/*.output",
        "alive_window_sec": 60,
    },
    "gcloud_context": {
        "enabled": True,
        "cadence_sec": 1800,         # every 30 min
        "show_on_change": True,
    },
    "health": {
        "enabled": True,
        "cadence_sec": 600,
        "projects": [],              # e.g. ["sg-platform-portal"]
        "per_project_timeout_sec": 2,
    },
    "spotify": {
        # PRIVACY: the agent in your session will see what's currently
        # playing. Enable only on hosts where you accept that.
        "enabled": False,
        "cadence_sec": 0,
    },
    "screenshots": {
        # PRIVACY: filenames can reveal what you were just looking at
        # (across unrelated apps and conversations). Enable cautiously.
        "enabled": False,
        "cadence_sec": 300,
        "path": "~/Desktop",
        "glob": "Screen*.png",
        "count": 3,
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# Plumbing — config merge, state file, env overrides
# ─────────────────────────────────────────────────────────────────────────────

# State is keyed by session_id so cadences are PER-SESSION, not global. Every
# new Claude Code session — CLI, Desktop, IDE — starts with a clean state file
# and gets all enabled fields on the first emission. Within a session, cadences
# throttle subsequent runs normally. Old session files are GC'd after
# CLAUDE_HOOK_STATE_TTL_DAYS (default 7).
STATE_DIR = Path(
    os.environ.get(
        "CLAUDE_HOOK_STATE_DIR",
        os.path.join(
            os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")),
            "claude-hook",
        ),
    )
)

STATE_TTL_SEC = int(os.environ.get("CLAUDE_HOOK_STATE_TTL_DAYS", "7")) * 86400


def state_path(session_id: str) -> Path:
    # Single-file override remains supported for tests / single-shot smoke runs.
    if os.environ.get("CLAUDE_HOOK_STATE_PATH"):
        return Path(os.environ["CLAUDE_HOOK_STATE_PATH"])
    return STATE_DIR / f"{session_id or 'nosession'}.json"


def gc_old_state_files() -> None:
    if STATE_TTL_SEC <= 0 or not STATE_DIR.is_dir():
        return
    cutoff = time.time() - STATE_TTL_SEC
    for f in STATE_DIR.glob("*.json"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            continue


def _merge(base: dict, override: dict) -> dict:
    out = {k: dict(v) if isinstance(v, dict) else v for k, v in base.items()}
    for field, fcfg in (override or {}).items():
        if not isinstance(fcfg, dict):
            continue
        out.setdefault(field, {}).update(fcfg)
    return out


def _load_yaml_config() -> dict:
    path = Path(
        os.environ.get(
            "CLAUDE_HOOK_CONFIG", os.path.expanduser("~/.claude/hooks/temporal.yaml")
        )
    )
    if not path.is_file() or not _HAS_YAML:
        return {}
    try:
        with path.open() as f:
            data = yaml.safe_load(f) or {}
        return data.get("fields", data) if isinstance(data, dict) else {}
    except Exception:
        return {}


def _coerce_env(value: str):
    low = value.lower()
    if low in ("true", "1", "yes", "on"):
        return True
    if low in ("false", "0", "no", "off"):
        return False
    try:
        return int(value)
    except ValueError:
        pass
    if "," in value:
        return [v.strip() for v in value.split(",") if v.strip()]
    return value


def _apply_env_overrides(cfg: dict) -> dict:
    for field, fcfg in cfg.items():
        for key in list(fcfg.keys()):
            env_key = f"CLAUDE_HOOK_{field.upper()}_{key.upper()}"
            if env_key in os.environ:
                fcfg[key] = _coerce_env(os.environ[env_key])
    return cfg


def load_config() -> dict:
    cfg = _merge(DEFAULT_CONFIG, _load_yaml_config())
    return _apply_env_overrides(cfg)


def state_load(path: Path) -> dict:
    try:
        with path.open() as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def state_save(path: Path, state: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with tmp.open("w") as f:
            json.dump(state, f)
        os.chmod(tmp, 0o600)
        tmp.replace(path)
    except OSError:
        pass  # SPEC 3.1.7 — never crash the hook


def _due(state: dict, field: str, cadence: int, value=None, show_on_change=False) -> bool:
    """Return True if the field should emit this run."""
    fstate = state.get(field, {})
    last_shown = fstate.get("last_shown", 0)
    if show_on_change and value is not None and fstate.get("last_value") != value:
        return True
    if cadence == 0:
        return True
    return time.time() - last_shown >= cadence


def _commit(state: dict, field: str, value=None) -> None:
    state.setdefault(field, {})["last_shown"] = time.time()
    if value is not None:
        state[field]["last_value"] = value


def _safe_run(args, timeout=5):
    """Run a subprocess; return stdout str or None on any failure."""
    try:
        r = subprocess.run(
            args, capture_output=True, timeout=timeout, check=False
        )
        if r.returncode != 0:
            return None
        return r.stdout.decode(errors="replace").strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Fields (SPEC §4)
# ─────────────────────────────────────────────────────────────────────────────


def field_time(cfg: dict, state: dict):
    if not cfg.get("enabled", True):
        return None
    if not _due(state, "time", cfg.get("cadence_sec", 0)):
        return None
    now_local = datetime.now().astimezone()
    hour = now_local.hour
    if 5 <= hour < 12:
        period = "morning"
    elif 12 <= hour < 17:
        period = "afternoon"
    elif 17 <= hour < 21:
        period = "evening"
    else:
        period = "night"
    day = now_local.strftime("%A")
    # Use %I:%M then strip leading zero for portability (no %-I on all platforms).
    clock = now_local.strftime("%I:%M %p").lstrip("0")
    tz = now_local.strftime("%Z") or "local"
    utc = now_local.astimezone(timezone.utc).isoformat(timespec="seconds")
    out = f"⏱ {day} {period}, {clock} {tz} (utc={utc})"
    _commit(state, "time")
    return out


def field_gcloud_auth_ttl(cfg: dict, state: dict):
    if not cfg.get("enabled", True):
        return None
    if not _due(state, "gcloud_auth_ttl", cfg.get("cadence_sec", 600)):
        return None
    out = _safe_run(["gcloud", "auth", "print-access-token", "--quiet"], timeout=3)
    _commit(state, "gcloud_auth_ttl")
    if out is None:
        return "🔑 gcloud-auth: ⚠ REAUTH REQUIRED (run: gcloud auth login)"
    if cfg.get("show_when_healthy", False):
        return "🔑 gcloud-auth: ok"
    return None


def _git_summary(repo: Path):
    """Return (is_dirty, summary) for a git repo."""
    parts = []
    untracked_dirty = _safe_run(
        ["git", "-C", str(repo), "status", "--porcelain"], timeout=3
    )
    if untracked_dirty:
        lines = untracked_dirty.splitlines()
        modified = sum(1 for ln in lines if not ln.startswith("??"))
        untracked = sum(1 for ln in lines if ln.startswith("??"))
        if modified:
            parts.append(f"{modified}m")
        if untracked:
            parts.append(f"{untracked}u")
    ahead_behind = _safe_run(
        ["git", "-C", str(repo), "rev-list", "--left-right", "--count", "HEAD...@{u}"],
        timeout=3,
    )
    if ahead_behind:
        try:
            ahead_s, behind_s = ahead_behind.split()
            ahead, behind = int(ahead_s), int(behind_s)
            if ahead:
                parts.append(f"↑{ahead}")
            if behind:
                parts.append(f"↓{behind}")
        except ValueError:
            pass
    return (len(parts) > 0, " ".join(parts) if parts else "✓")


def field_repo_drift(cfg: dict, state: dict):
    if not cfg.get("enabled", True):
        return None
    if not _due(state, "repo_drift", cfg.get("cadence_sec", 300)):
        return None
    root = Path(os.path.expanduser(cfg.get("src_root", "~/src")))
    if not root.is_dir():
        return None
    rows = []
    for child in sorted(root.iterdir()):
        if not (child / ".git").is_dir():
            continue
        dirty, summary = _git_summary(child)
        if dirty or cfg.get("include_clean", False):
            rows.append(f"{child.name} {summary}")
    _commit(state, "repo_drift")
    if not rows:
        return None
    return "⚠ drift: " + " · ".join(rows)


def _human_age(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds}s ago"
    if seconds < 5400:
        return f"{seconds // 60}m ago"
    if seconds < 86400 * 2:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def field_watched_files(cfg: dict, state: dict):
    if not cfg.get("enabled", True):
        return None
    if not _due(state, "watched_files", cfg.get("cadence_sec", 300)):
        return None
    paths = cfg.get("paths") or []
    if not paths:
        return None
    out = []
    for p in paths:
        path = Path(os.path.expanduser(p))
        if not path.is_file():
            continue
        age = _human_age(time.time() - path.stat().st_mtime)
        out.append(f"{path.name} ({age})")
    _commit(state, "watched_files")
    if not out:
        return None
    return "📄 watched: " + " · ".join(out)


def field_bg_jobs(cfg: dict, state: dict):
    if not cfg.get("enabled", True):
        return None
    if not _due(state, "bg_jobs", cfg.get("cadence_sec", 0)):
        return None
    window = cfg.get("alive_window_sec", 60)
    pattern = cfg.get("harness_glob", "/private/tmp/claude-*/*/tasks/*.output")
    now = time.time()
    alive = []
    for path in glob.glob(pattern):
        try:
            if now - os.path.getmtime(path) < window:
                alive.append(os.path.basename(path).removesuffix(".output"))
        except OSError:
            continue
    _commit(state, "bg_jobs")
    if not alive:
        return None
    return f"⏳ bg: {len(alive)} job(s) ({', '.join(alive[:3])}{'...' if len(alive) > 3 else ''})"


def field_gcloud_context(cfg: dict, state: dict):
    if not cfg.get("enabled", True):
        return None
    account = _safe_run(["gcloud", "config", "get-value", "account"], timeout=2)
    project = _safe_run(["gcloud", "config", "get-value", "project"], timeout=2)
    if not account and not project:
        return None
    value = f"{account or '?'} → {project or '?'}"
    if not _due(
        state,
        "gcloud_context",
        cfg.get("cadence_sec", 1800),
        value=value,
        show_on_change=cfg.get("show_on_change", True),
    ):
        return None
    _commit(state, "gcloud_context", value=value)
    return f"☁ gcloud: {value}"


def field_health(cfg: dict, state: dict):
    if not cfg.get("enabled", True):
        return None
    if not _due(state, "health", cfg.get("cadence_sec", 600)):
        return None
    projects = cfg.get("projects") or []
    if not projects:
        return None
    timeout = cfg.get("per_project_timeout_sec", 2)
    alerts = []
    for proj in projects:
        out = _safe_run(
            [
                "gcloud", "compute", "instances", "list",
                f"--project={proj}", "--filter=status!=RUNNING",
                "--format=value(name,status)",
            ],
            timeout=timeout,
        )
        if out:
            alerts.append(f"{proj}: {out.replace(chr(10), ', ')}")
    _commit(state, "health")
    if not alerts:
        return None
    return "🚨 outages: " + " · ".join(alerts)


def field_spotify(cfg: dict, state: dict):
    # PRIVACY: §4.8 of SPEC. Disabled by default; do not flip without thought.
    if not cfg.get("enabled", False):
        return None
    if not _due(state, "spotify", cfg.get("cadence_sec", 0)):
        return None
    if sys.platform == "darwin":
        script = (
            'tell application "Spotify"\n'
            'if it is running then\n'
            'if player state is playing then\n'
            'return (name of current track) & " — " & (artist of current track)\n'
            'end if\n'
            'end if\n'
            'return ""\n'
            'end tell'
        )
        out = _safe_run(["osascript", "-e", script], timeout=2)
    else:
        out = _safe_run(
            ["playerctl", "metadata", "--format", "{{title}} — {{artist}}"], timeout=2
        )
    _commit(state, "spotify")
    if not out:
        return None
    return f"♪ {out}"


def field_screenshots(cfg: dict, state: dict):
    # PRIVACY: §4.9 of SPEC. Disabled by default.
    if not cfg.get("enabled", False):
        return None
    if not _due(state, "screenshots", cfg.get("cadence_sec", 300)):
        return None
    base = Path(os.path.expanduser(cfg.get("path", "~/Desktop")))
    pat = cfg.get("glob", "Screen*.png")
    n = int(cfg.get("count", 3))
    if not base.is_dir():
        return None
    files = sorted(base.glob(pat), key=lambda p: p.stat().st_mtime, reverse=True)[:n]
    _commit(state, "screenshots")
    if not files:
        return None
    return "📷 last screenshots: " + " · ".join(str(f) for f in files)


# ─────────────────────────────────────────────────────────────────────────────
# Orchestration
# ─────────────────────────────────────────────────────────────────────────────

FIELDS = [
    ("time", field_time),
    ("gcloud_auth_ttl", field_gcloud_auth_ttl),
    ("repo_drift", field_repo_drift),
    ("watched_files", field_watched_files),
    ("bg_jobs", field_bg_jobs),
    ("gcloud_context", field_gcloud_context),
    ("health", field_health),
    ("spotify", field_spotify),
    ("screenshots", field_screenshots),
]


def _read_hook_payload() -> dict:
    """Read the hook event payload from stdin (Claude Code passes JSON).
    Returns {} on parse failure or no stdin; the hook must still produce
    useful output when invoked directly (smoke tests, manual runs)."""
    try:
        raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    except (OSError, ValueError):
        raw = ""
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def main() -> int:
    payload = _read_hook_payload()
    session_id = payload.get("session_id", "nosession")
    event = payload.get("hook_event_name", "UserPromptSubmit")

    gc_old_state_files()

    path = state_path(session_id)
    cfg = load_config()
    state = state_load(path)

    lines = []
    for name, fn in FIELDS:
        try:
            value = fn(cfg.get(name, {}), state)
        except Exception:
            # SPEC §3.1.7 — never crash the hook.
            value = None
        if value:
            lines.append(f"[{value}]")

    state_save(path, state)

    if not lines:
        return 0

    # Claude Code's documented hook output: a JSON envelope with
    # additionalContext injected before the user's next message. Falls back to
    # plain stdout when invoked manually (sys.stdin.isatty()) so smoke tests
    # stay readable.
    additional = "\n".join(lines)
    if sys.stdin.isatty():
        sys.stdout.write(additional + "\n")
    else:
        sys.stdout.write(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": event,
                "additionalContext": additional,
            }
        }) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
