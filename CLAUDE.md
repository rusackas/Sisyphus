# Sisyphus — context for Claude Code sessions

Welcome. This file is loaded into every Claude Code session that
runs inside this repo (including Ad Hoc Tasks spawned by the app's
own `?` feedback button). Read it once before touching anything —
the invariants below are load-bearing and the architecture won't
make sense without them.

There are three context files at the repo root, with deliberate
separation:

- **CLAUDE.md** (this file) — stable architecture and load-bearing
  invariants. What's true *by design*. Changes only when the
  architecture changes.
- **MEMORY.md** — curated, learned-pattern facts distilled from
  incidents and feedback PRs. What we've *learned the hard way*.
  Append-only-ish; entries get amended, not deleted.
- **BACKLOG.md** — running list of drive-by ideas and unfinished
  threads. What's *queued or unfinished*. No ceremony.

Read MEMORY.md before recommending anything that has historically
been a sharp edge — it's where the "we tried X and it caused Y"
lessons live.

## What this is

Sisyphus is a personal repo-maintainer toolkit that triages and
acts on PRs and Issues across watched GitHub repos. A FastAPI +
Jinja + HTMX web app surfaces queues of cards; each card has a
mechanical action menu plus a Claude-authored narrative proposal.
Click a button → either a one-shot side-effect (via `gh`) or a
Claude Agent SDK session that does worktree work (rebase, fix,
draft a comment for review, etc.). The Python package is called
`repobot` for legacy reasons (it's been through two product
renames — first `repobot`, then `CustodialEngineer`, now
`Sisyphus`); the package name stays put because every import path
would otherwise ripple, and renaming code that nobody outside the
repo imports is churn for churn's sake.

The user is **a maintainer reading and approving the bot's
suggestions** — not a passive consumer. Every public-facing side
effect (comments, approvals, closes, PR descriptions) routes
through a review modal where the user edits the body before it's
posted. Sisyphus drafts; the maintainer ships.

## Invariants — read these first

These are the load-bearing rules. Most of the architecture is a
direct consequence of one of them. Violating them is how we
introduced the bugs we keep fixing.

### 1. Mechanical-first triage

The **action menu** on every card is built mechanically from `raw`
signals (CI status, mergeStateStatus, conflicts, labels, age,
author, etc.) by code in `repobot/triage.py`. The Claude triage
**skill** writes the *narrative* (`proposal` text + supporting
notes) but **does not** decide what actions are available. We
went mechanical-first after the prompt-driven menu kept dropping
critical actions (approve-merge missing on clean PRs, fix-precommit
missing on lint failures, etc.).

Corollary: if a card has the wrong action menu, fix it in
`triage.py`, **not** by tweaking a skill prompt. If the narrative
contradicts the menu, fix the skill so it stops contradicting.

### 2. Editorial control — every public comment is reviewed

Anything that posts to GitHub as the user (comments, approval
review bodies, close comments, PR descriptions, replies to review
threads) routes through a review modal where the user edits the
body before submit. The skill drafts; the modal lets the user
approve, edit, or cancel. No skill is ever permitted to post a
public comment without this gate.

Pattern: triage skills emit `notes.<something>_comment` (e.g.
`approval_comment`, `close_comment`, `nudge_comment`). The card
template surfaces a button with `data-editable-body="{{ that }}"`.
Clicking opens the comment-edit modal; on confirm we re-submit
with the edited body to the action endpoint, which the action
skill uses verbatim via `extra_context.comment_body`.

### 3. Never bypass commit hooks or signing

`--no-verify`, `--no-gpg-sign`, `-c commit.gpgsign=false` — never,
unless the user explicitly asks. If a hook fails, fix the
underlying issue. We've never had a case where bypassing was the
right call.

### 4. Mechanical action menu reflects fresh signals

The mechanical menu is computed from `item.raw`. If `raw` is
stale, the menu is wrong. `retriage_item` and `refresh_one_item`
both refetch `raw` from GitHub before rebuilding the menu — don't
add new triage entry points that skip the refetch.

### 5. Plan-then-build for non-trivial work

For anything beyond a small, obvious fix: propose a short plan
(2–3 sentences with the main tradeoff), wait for "yes," then
execute. Don't slide into implementation on exploratory questions.
Don't dump giant diffs without alignment. The user reviews every
PR, so a misaligned implementation is wasted work for both of you.

## File map

```
repobot/
├── api.py            FastAPI routes — all endpoints live here.
│                     Modal-driven custom flows (request-reviewers,
│                     resolve-bot-threads, bulk-approve, track-pr,
│                     spawn-feedback-task, etc.) terminate here.
├── triage.py         Mechanical action menus + bot-thread
│                     classifier + helpers like pick_unblock_action.
│                     This is the authoritative menu builder.
├── runner.py         Triage orchestration: run_queue, retriage_item,
│                     refresh_one_item, _triage_one. Wires the
│                     mechanical menu + skill narrative together.
├── actions.py        Action registry + dispatch + continue_action.
│                     New skill-backed actions register here. Custom
│                     flows (skill: None) register here too so the
│                     card UI sees them in the menu.
├── sessions.py       Claude Agent SDK session pool. Live triage +
│                     action sessions live here. Cap is config-driven.
├── github.py         All `gh` / GraphQL plumbing. Repo registry,
│                     fetch_one_pr, signal computation.
├── queues.py         State setters: set_item_state,
│                     set_item_result, set_item_parked_at, etc.
│                     Append-only history.
├── tasks.py          Ad Hoc Tasks (parallel to queue items).
│                     create_task / dispatch_task — used by the
│                     `?` feedback button.
├── templates/
│   ├── _card.html    The PR/Issue card. Lots of branching for
│                     issue-vs-PR / state / live-action presence.
│   ├── index.html    Page shell + every modal. JS lives here too.
│   ├── pr_modal.html / issue_modal.html  Detail modals.
│   └── ...
└── static/style.css  All styling. CSS-variable theme system; four
                      palettes (paper / blueprint / graphite / carbon).
                      See "Frontend conventions" below.

.claude/skills/       Claude Agent SDK skill prompts. One per
                      skill, each with SKILL.md describing inputs,
                      procedure, output schema. These are pure
                      prompts — no Python here.

config.yaml           Repo registry, queue config, identity,
                      auth, dry_run. Reload by restarting the server.
BACKLOG.md            Drive-by ideas, dated. See below.
```

## How a card flows

1. **Fetch** (`runner.run_queue` → `github.fetch_*`): pull PRs/issues
   matching the queue's search query into `bucket.items`. Each item
   gets a `raw` blob with all the signals.
2. **Triage** (`runner._triage_one`): mechanical builds the action
   menu from `raw`; skill produces narrative `proposal` text +
   `triage_notes`. Both stored on the item. State stays at
   `initial_state`.
3. **User clicks an action button** — either a skill-backed action
   (POST `/queues/{q}/items/{i}/actions/{action_id}`, dispatches a
   Claude session via `actions.dispatch`) or a custom-flow action
   (POST to the action's dedicated endpoint).
4. **Skill phase 1** runs in a session. Some skills are one-shot
   (post a comment + done). Others are two-phase (phase 1 drafts
   something — comment, plan, PR title/body — and emits a status
   like `proposed` / `pr_ready` / `needs_human`; the card surfaces
   a review modal; user approves/edits; phase-2 message goes back
   into the same session and the skill executes).
5. **State transition** based on skill result: `terminal_state`
   (usually `done` or `awaiting update`) on success,
   `failure_state` on error.

## Skill anatomy

Every skill at `.claude/skills/<name>/SKILL.md` has:
- A YAML front matter (`name`, `description`, `worktree_required`).
- A markdown body describing inputs, procedure, output schema.
- A trailing JSON output spec — the skill's last message must be a
  fenced ` ```json ` block matching that schema. The dispatcher
  parses the last fenced JSON block as the result.

Two-phase skills emit a sentinel status (`proposed`, `pr_ready`,
`needs_human`) at end of phase 1. The dispatcher detects these in
`actions.dispatch`'s `_on_first_turn` hook and stops the session
in an idle state. The user reviews via the appropriate modal,
sends back the approved/edited content as a phase-2 user message,
and the skill resumes.

When you write or edit a skill prompt:
- Be specific about what data to fetch and what to ignore.
- Make the narrative match the available action menu — if the
  mechanical menu won't include `approve-merge`, the skill must
  not recommend it.
- Output JSON last; nothing after it.
- Keep the budget tight (under ~8 turns for triage skills).

## Adding actions

Three flavors:

### Skill-backed action

1. Create `.claude/skills/<name>/SKILL.md`.
2. Register in `actions.ACTIONS_REGISTRY` (`repobot/actions.py`)
   with `skill: "<name>"`, `worktree_required`, and the state
   transitions (`in_progress_state`, `terminal_state`,
   `failure_state`).
3. Add to a mechanical menu in `triage.py` so the button surfaces.
4. If the action posts a comment, define a triage-notes field
   (e.g., `notes.foo_comment`) and surface a `data-editable-body`
   button in `_card.html` so the user reviews before send.

### Custom-flow action (modal-driven, no skill)

1. Register in `ACTIONS_REGISTRY` with `skill: None`. This makes
   the card UI render a button via the standard loop.
2. Add a dedicated endpoint in `api.py` (`POST
   /queues/{q}/items/{i}/<action>`).
3. Add the modal markup in `index.html` and the JS handler.
4. Wire the submit-intercept in `index.html` (the big `submit`
   listener) to route forms with `data-<your-flag>="1"` to your
   modal.

Examples in the repo: `request-reviewers`, `resolve-bot-threads`,
`bulk-approve` (queue-level), `track-pr`, `spawn-feedback-task`.

### Mechanical-only

If you just want a state transition with no Claude in the loop —
add an endpoint that calls `set_item_state` and `set_item_result`,
add the button. See `track-pr` for a minimal example.

## Development loop

```bash
# Run the server (default port 8000):
.venv/bin/python -m repobot serve --host 127.0.0.1 --port 8000

# Or in the background, with logs to a tempfile:
nohup .venv/bin/python -m repobot serve --host 127.0.0.1 --port 8000 \
  > /tmp/repobot-server.log 2>&1 &

# Restart pattern (used a lot during development):
PID=$(ps -ef | grep "repobot serve" | grep -v grep | awk '{print $2}')
[ -n "$PID" ] && kill $PID
sleep 1
nohup .venv/bin/python -m repobot serve --host 127.0.0.1 --port 8000 \
  > /tmp/repobot-server.log 2>&1 &

# Smoke-test after a change:
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/
tail -30 /tmp/repobot-server.log | grep -iE 'error|trace|exception'
```

There is also an in-app `update sisyphus` button (header, next to
the brand) that does `git pull --ff-only origin main` and re-execs
the server. Aborts on dirty working tree. Use it after merging a
Sisyphus PR you've been reviewing in this same instance.

### Dry run

`actions.dry_run: true` in `config.yaml` makes side-effecting
steps (push, force-push, comment, close, merge) report
`skipped_dry_run` instead of executing. Flip to `false` once you
trust the flow.

### State

Persistent state lives in a SQLite file at `state.db` (managed by
`repobot/db.py` + `queues.py`). Sessions live in memory in
`sessions.py`. Restarting the server clears live sessions but
preserves queue state.

## Commit & PR style

- **Always create a new commit; never amend.** Pre-commit hook
  failures mean the commit didn't happen — `--amend` would
  modify the *previous* commit, potentially destroying work.
- **Co-Authored-By trailer** on every commit:
  `Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>`
  Use the actual model id you're running on if it differs.
- **Never `--no-verify`** or skip signing.
- **`git add` specific files** by name, not `-A` / `.`. The repo
  shouldn't have stray secrets but this is a habit.
- **Commit messages**: short subject (~70 char), then a blank
  line, then a body explaining *why*. Reference the symptom that
  motivated the change (PR number, user complaint) when relevant.
- **PR titles**: imperative, short. PR bodies: Summary + Test plan.

## Working with the user

Evan is the maintainer. Work style:

- **Plan-then-build** for anything non-trivial. Propose 2–3
  sentences with the main tradeoff; wait for confirmation; then
  execute.
- **Terse responses.** No pre-amble ("Great question!"), no
  trailing summaries that restate the diff. Lead with the answer.
- **Reference file paths with line numbers** (`repobot/triage.py:182`)
  so Evan can jump there.
- **Don't ask for confirmation on reversible local actions**
  (file edits, running tests, restarting the dev server). Do
  ask on hard-to-reverse stuff (force-push, deleting branches,
  dropping state, sending comments to live GitHub).
- **Only commit when explicitly asked.** Don't auto-commit just
  because work looks done.

## BACKLOG.md

`BACKLOG.md` at the repo root is a running list of drive-by ideas
Evan tosses out between sessions. No ceremony — not a roadmap,
just a notepad. When you finish something that was on the list,
either remove the line or strike it through and stamp with a date.
Reasonable to glance at the "Active" section when you're looking
for related context to a feature you're touching, but don't take
the list as authoritative; the repo state is.

## Anti-patterns — don't do these

These are stylistic / architectural; the action-menu and triage-
specific anti-patterns ("don't propose approve-merge over red
CI", "don't fix menu bugs in skill prompts", etc.) live in
MEMORY.md with their incident context.

- **Don't introduce backwards-compat shims, dead code, or
  re-exports for things that have no consumers.** This is a
  small personal toolkit; YAGNI.
- **Don't add error handling for impossible cases.** Trust
  internal call sites. Validate at system boundaries (HTTP form
  inputs, GraphQL responses) — not between two functions you own.
- **Don't write multi-paragraph docstrings or comment blocks.**
  One short line max. Comments explain *why*, never *what*.
- **Don't add a feature without checking MEMORY.md first** when
  the feature touches an area with a history of sharp edges
  (action menus, comment-posting flows, triage signal staleness).

## Frontend conventions

- **Theme system**: four palettes (`paper` warm-cream light,
  `blueprint` pure-white/navy light, `graphite` charcoal/electric-blue
  dark, `carbon` black/amber dark). Use CSS custom properties
  (`var(--ink)`, `var(--bg-card)`, `var(--accent)`, `var(--rule)`,
  `var(--alert-warning-soft)`, etc.) — never hardcode colors.
- **Typography eyebrows**: tracked-uppercase mono labels at
  ~9.5–10px (`font-family: var(--mono); letter-spacing:
  var(--track-eyebrow); text-transform: uppercase`). Used for
  section headers, button labels, status chips.
- **Dashed rules between zones**, never solid borders for
  section separators. Solid borders are heavy and break the
  scan rhythm.
- **Buttons follow `var(--radius-pill)` for pill-shape.** Mono
  font, lowercase or uppercase per role (uppercase = primary
  action / state badge; lowercase = secondary).
- **Modals**: backdrop + `.modal-body` + `.modal-header` +
  body content + `.comment-modal-actions` (or similar)
  footer. Open/close via `.is-open` class + `aria-hidden`.
  Submit-intercept routes the form through the modal; on
  confirm, set a `data-<flag>-confirmed="1"` and call
  `requestSubmit()` so the second pass goes through.

## When in doubt

- Read `repobot/triage.py` for any action-menu question.
- Read `repobot/actions.py:ACTIONS_REGISTRY` for any
  action-lifecycle question.
- Read the relevant `.claude/skills/<name>/SKILL.md` for any
  skill-narrative question.
- Read recent `git log --oneline` for the last ~20 commits — the
  conversational style + context-rich commit messages give a fast
  read on what's been changing.
- If still unclear, propose a plan instead of guessing.
