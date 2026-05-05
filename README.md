# Sisyphus

A local kanban-style maintenance tool for a GitHub repo. Reads PRs
and issues into per-queue columns, runs headless
[Claude Agent SDK](https://docs.claude.com/en/api/agent-sdk/overview)
sessions to triage each one, and lets you dispatch skill-driven
actions (approve, rebase, attempt-fix, address review comments, …)
from the UI. Designed for maintainers, not for autonomous
unsupervised ops — every mutation goes through a human-reviewed
modal before it lands.

Originally built to help maintain
[apache/superset](https://github.com/apache/superset), but
configurable to any repo.

---

## How it works

```
fetch → triage (Claude) → propose action → (human click) → run skill → done
         ↑                                                    ↓
         └──────────── auto-refresh every 30s ────────────────┘
```

- Each **queue** pulls items from GitHub (e.g. open Dependabot PRs,
  your own PRs, review-requested PRs).
- Each item is **triaged** by a Claude session whose prompt is a
  Skill tailored to that queue. The skill reads the PR, CI logs,
  review threads, and comments, and emits a proposal + a ranked list
  of **actions** the user could click.
- Clicking an action button spawns a second Claude session with the
  matching action skill, sometimes with the PR branch checked out
  in a worktree. The session runs, reports back with a JSON result,
  and the item moves to `done` (or `awaiting update`, or
  `in progress` stays for human follow-up).
- A mechanical fallback triage runs if the Claude session fails.

## Stack

- **Python 3.10+** — FastAPI + Jinja2 UI, `claude-agent-sdk` for
  headless sessions, `gh` CLI for GitHub ops.
- **State** — single JSON file at `state/queues.json` (atomic
  write via tmp + `os.replace`), one lock for all mutations.
- **Isolation** — per-PR git worktrees at
  `workspace/worktrees/pr-N/` for any action that touches the
  working tree (rebase, fix attempts, deep review, lockfile
  regen).

## Authentication

- **GitHub:** the `gh` CLI reads `GITHUB_TOKEN` or `GH_TOKEN` from
  the environment. Nothing is committed.
- **Claude:** the spawned session uses your Claude Code OAuth
  (Max / Pro). `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` are
  scrubbed from the subprocess env so usage stays on the
  subscription regardless of shell state.

## Configuration (`config.yaml`)

| Key                           | Meaning                                                      |
| ----------------------------- | ------------------------------------------------------------ |
| `repo.owner` / `repo.name`    | Target repo.                                                 |
| `identity.github_username`    | Your handle. Skills use this to avoid @-mentioning yourself. |
| `auth.token_env`              | Name of the env var that holds the GitHub token.             |
| `actions.dry_run`             | If true, mutating steps are logged instead of executed.      |
| `sessions.max_concurrent`     | Global cap on live Claude sessions (triage + actions).       |
| `auto_refresh.interval_seconds` | Seconds between automatic queue top-ups. 0 disables.       |
| `queues`                      | Array of queue definitions (see below).                      |

### Queues

Each queue entry:

| Field             | Meaning                                                       |
| ----------------- | ------------------------------------------------------------- |
| `id`              | Stable identifier (used by API / state).                      |
| `title`           | Label shown in the UI.                                        |
| `max_in_flight`   | Cap counted against non-done items.                           |
| `initial_state`   | State assigned to freshly-fetched items.                      |
| `initial_states`  | Optional list — lets the fetcher pre-bucket items into separate triage columns based on GH signals (used by `review-requested`). |
| `done_state`      | Terminal state (default `done`).                              |
| `awaiting_state`  | Recoverable state when an action needs a human nudge.         |
| `states`          | Ordered columns for the UI.                                   |
| `query`           | Fetch parameters (author, state, review_requested, …).        |

Slot accounting: each refresh pulls `max_in_flight − non_done_count`
new items.

The three queues shipped in `config.yaml`:

- **Dependabot PRs** (`failing-dependabot-prs`) — all open
  Dependabot PRs oldest-first. Triage branches on CI status:
  green → `approve-merge`; failing → root-cause analysis + fix /
  rebase / recreate / close proposals.
- **My PRs** (`my-prs`) — your own open non-draft PRs with
  something to do: conflicts, failing CI, or unresolved review
  threads. Triage branches on those three signals.
- **Review requested** (`review-requested`) — PRs where you're a
  requested reviewer, pre-bucketed by the fetcher into
  `triage: mergeable` (CI green, no conflicts, no blocks) or
  `triage: blocked`. Triage reads the PR body, linked issues, full
  diff, review threads, and every comment (human + bot) to
  produce a reviewer-focused assessment.

## Actions

| Action                     | Skill                           | Worktree | What it does (non-dry-run)                                 |
| -------------------------- | ------------------------------- | -------- | ---------------------------------------------------------- |
| `skip`                     | —                               | —        | Move to `done` without running anything.                   |
| `close`                    | `close-pr`                      | no       | `gh pr close` with optional comment.                       |
| `prompt`                   | `prompt-on-pr`                  | yes      | Free-form user instruction executed against the PR.        |
| `rebase`                   | `rebase-pr`                     | yes      | Rebase on master, resolve trivial conflicts, force-push.   |
| `update-lockfile`          | `update-pr-lockfile`            | yes      | Delete & regenerate frontend lockfile, commit, force-push. |
| `attempt-fix`              | `attempt-fix-pr`                | yes      | Minimal code fix for a dep-bump regression; force-push.    |
| `fix-precommit`            | `fix-precommit-pr`              | yes      | Run `pre-commit run`, commit auto-fixes, force-push.       |
| `plan-fix`                 | `plan-pr-fix`                   | yes      | Draft a fix plan when `attempt-fix` needed a human.        |
| `address-comments`         | `address-review-comments`       | yes      | Walk unresolved review threads: apply fixes + draft replies. |
| `retrigger-ci`             | `retrigger-pr-ci`               | no       | `gh run rerun` failed workflows on the PR head.            |
| `approve-merge`            | `approve-and-merge-pr`          | no       | `gh pr review --approve` + `gh pr merge --squash --auto`.  |
| `approve-review`           | `pr-review-approve`             | no       | Approve the code without merging.                          |
| `add-review-comment`       | `add-pr-review-comment`         | no       | Post a top-level comment on the PR (human-edited body).    |
| `request-changes-review`   | `pr-review-request-changes`     | no       | Submit a formal "request changes" review.                  |
| `dismiss-review-request`   | `dismiss-review-request`        | no       | Remove yourself from the requested-reviewers list.         |
| `summarize-diff`           | `summarize-pr-diff`             | no       | Read-only 3-bullet diff summary, stashed on the card.      |
| `assess-on-worktree`       | `assess-pr-on-worktree`         | yes      | Deep read-only review on a checked-out worktree.           |
| `dependabot-rebase`        | `dependabot-rebase-comment`     | no       | Post `@dependabot rebase`.                                 |
| `dependabot-recreate`      | `dependabot-recreate-comment`   | no       | Post `@dependabot recreate`.                               |

Every mutating action is preceded by a modal where the human
reviews / edits the draft comment or review body before it gets
posted.

## Running

```bash
# one-time setup
pip install -e .

# clone the target repo into workspace/
.venv/bin/python -m repobot init

# web UI on http://127.0.0.1:8000
.venv/bin/python -m repobot serve

# manual backfill (optional; auto-refresh handles this by default)
.venv/bin/python -m repobot fetch failing-dependabot-prs
```

Requires a working `gh` CLI and either a Claude Code session or a
Claude Pro / Max OAuth login.

## Layout

```
.
├── config.yaml
├── pyproject.toml
├── state/                              # gitignored; runtime queue state
├── workspace/                          # gitignored
│   ├── <target-repo>/                  # main clone
│   └── worktrees/                      # per-PR worktrees
├── .claude/skills/                     # every skill the app dispatches
│   ├── triage-dependabot-pr/
│   ├── triage-my-pr/
│   ├── triage-review-requested/
│   ├── assess-pr-on-worktree/
│   ├── address-review-comments/
│   ├── approve-and-merge-pr/
│   ├── attempt-fix-pr/
│   ├── plan-pr-fix/
│   ├── rebase-pr/
│   ├── update-pr-lockfile/
│   ├── fix-precommit-pr/
│   ├── retrigger-pr-ci/
│   ├── pr-review-approve/
│   ├── pr-review-request-changes/
│   ├── add-pr-review-comment/
│   ├── summarize-pr-diff/
│   ├── dismiss-review-request/
│   ├── dependabot-rebase-comment/
│   ├── dependabot-recreate-comment/
│   ├── prompt-on-pr/
│   └── close-pr/
├── repobot/
│   ├── __main__.py                     # CLI: init / fetch / serve
│   ├── api.py                          # FastAPI app
│   ├── runner.py                       # fetch + triage orchestration
│   ├── triage.py                       # skill-backed + mechanical triage
│   ├── actions.py                      # action registry, dispatch, hooks
│   ├── sessions.py                     # Claude Agent SDK wrapper
│   ├── queues.py                       # atomic JSON state storage
│   ├── github.py                       # gh CLI wrappers
│   ├── worktree.py                     # per-PR git worktrees
│   ├── workspace.py                    # main clone bootstrap
│   ├── config.py
│   ├── templates/index.html
│   └── static/style.css
└── tests/                              # pytest: triage / queues / runner / actions
```

## Safety model

- **Dry-run by default** — set `actions.dry_run: false` only once
  you trust the flow. Under dry-run the Claude session runs every
  non-mutating step and logs what it *would* have pushed /
  commented.
- **Human-in-the-loop modals** — comment and review bodies are
  draft-then-review. You never find out what went out by reading
  it on GitHub.
- **Worktree isolation** — anything that touches the working tree
  operates in a disposable worktree; your main clone stays on
  master.
- **Atomic state writes** — crashes mid-write can't corrupt
  `queues.json`.

## Notes

- Python package name is `repobot` (unchanged across two product
  renames — `repobot` → `CustodialEngineer` → `Sisyphus`). The
  package stays put because every import path would otherwise
  ripple, and renaming code that nobody outside the repo imports
  would be churn for churn's sake.
- `ANTHROPIC_API_KEY` is intentionally scrubbed from the subprocess
  env to keep Claude usage on the OAuth subscription.
