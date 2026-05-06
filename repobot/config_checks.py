"""Config checks — surfaces missing/broken auth, tokens, MCP setup,
etc. so the UI can show an "auth needed" / "config needed" banner
with actionable remediation instead of letting users hit a cryptic
401 deep inside a skill session log.

Each check is a callable returning a `Check` describing its
status, a human-readable message, and a list of `fix_steps` the
user can act on. Add a new check by writing a function and
appending it to `CHECKS`. Keep checks fast (sub-second) — they
run on page load and on the modal's "Recheck" button.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Check:
    id: str
    label: str
    ok: bool
    severity: str = "error"  # error | warning | info
    message: str = ""
    fix_steps: list[str] = field(default_factory=list)
    docs_url: str = ""


def _run(cmd: list[str], timeout: float = 5.0) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout)
        return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        return 127, "", str(exc)


def _claude_auth_check() -> Check:
    """Claude Code OAuth presence. The CLI stores credentials either
    at `~/.claude/.credentials.json` or (on macOS) in the Keychain
    under service "Claude Code". Either is a green light."""
    creds_file = Path.home() / ".claude" / ".credentials.json"
    if creds_file.exists():
        return Check(id="claude_auth", label="Claude OAuth", ok=True,
                     message="Credentials file present at ~/.claude/.credentials.json")

    if os.uname().sysname == "Darwin":
        rc, _, _ = _run(["security", "find-generic-password",
                          "-s", "Claude Code"], timeout=3)
        if rc == 0:
            return Check(id="claude_auth", label="Claude OAuth", ok=True,
                         message="Credentials in macOS Keychain (service: Claude Code)")

    return Check(
        id="claude_auth", label="Claude OAuth", ok=False,
        message=("No Claude Code credentials found. Skill sessions will "
                 "fail with HTTP 401 until you log in."),
        fix_steps=[
            "In a terminal, run: `claude login`",
            "Pick your subscription tier (Pro / Max / API) when prompted.",
            "Authorize via the browser link the CLI prints.",
            ("No server restart needed — the SDK reads credentials at "
             "subprocess-launch time, so the next skill session picks "
             "up the new auth automatically."),
        ],
        docs_url="https://docs.claude.com/en/docs/claude-code/quickstart",
    )


def _github_auth_check() -> Check:
    """`gh` CLI installed and logged in. We don't try GITHUB_TOKEN
    in-process — the rest of the codebase shells out to `gh`, so
    `gh auth status` is the authoritative check."""
    if not shutil.which("gh"):
        return Check(
            id="github_auth", label="GitHub auth", ok=False,
            message=("`gh` CLI is not on PATH. Sisyphus uses it for "
                     "every GitHub operation."),
            fix_steps=[
                "Install the GitHub CLI: `brew install gh` (or see https://cli.github.com).",
                "Then run `gh auth login` and choose GitHub.com → HTTPS → web browser.",
            ],
            docs_url="https://cli.github.com",
        )

    rc, out, err = _run(["gh", "auth", "status"], timeout=5)
    if rc == 0:
        # gh prints "Logged in to github.com account ..." on success
        first_line = (out or err).splitlines()[0] if (out or err) else ""
        return Check(id="github_auth", label="GitHub auth", ok=True,
                     message=first_line or "gh CLI authenticated")

    return Check(
        id="github_auth", label="GitHub auth", ok=False,
        message=("`gh auth status` reports no active login. All GitHub "
                 "actions (fetch, comment, push, merge) will fail."),
        fix_steps=[
            "In a terminal, run: `gh auth login`",
            "Choose GitHub.com → HTTPS → authenticate via web browser.",
            "Verify with `gh auth status` once it completes.",
        ],
        docs_url="https://cli.github.com/manual/gh_auth_login",
    )


# Add new checks here. Each function takes no args and returns a
# `Check`. Order matters only insofar as the UI lists them top-to-
# bottom — put the most foundational first.
CHECKS = [
    _claude_auth_check,
    _github_auth_check,
]


def run_all_checks() -> list[Check]:
    return [fn() for fn in CHECKS]


def summary() -> dict:
    """Shape returned by `/admin/config-status` and passed into
    the index template at render time so the header banner is
    correct on first paint (no flash-of-no-banner)."""
    checks = run_all_checks()
    return {
        "checks": [
            {
                "id": c.id, "label": c.label, "ok": c.ok,
                "severity": c.severity, "message": c.message,
                "fix_steps": c.fix_steps, "docs_url": c.docs_url,
            }
            for c in checks
        ],
        "any_failing": any(not c.ok for c in checks),
        "fail_count": sum(1 for c in checks if not c.ok),
    }
