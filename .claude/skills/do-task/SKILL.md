---
name: do-task
description: Execute an ad-hoc user task against a repo worktree. Routes to investigate-and-answer (question), draft-an-issue (issue), or implement-and-open-PR (pr) based on task.task_type. When task_type=auto, classifies from the prompt language.
worktree_required: true
---

# Do an ad-hoc task

The user dropped a free-form prompt into the Tasks board and picked
a repo. You're already `cd`'d into a fresh worktree at
`workspasisyphus/tasks/task-{task.id}/` on branch `sisyphus/task-{task.id}`
branched off the repo's default. No PR exists yet.

## Inputs (runtime context)

- `task.id` — integer task id.
- `task.repo_id` — registry id of the repo.
- `task.prompt` — the user's free-form description.
- `task.task_type` — one of `auto | question | issue | pr`.
- `repo.owner`, `repo.name`, `repo.slug` — the repo you're working
  against.
- `branch` — the local branch you're on (`sisyphus/task-{task.id}`).
- `dry_run` — if true, skip any side-effecting `gh`/`git push` steps;
  log what you would have done.

## Step 1 — Classify (auto only)

If `task_type != "auto"`, skip to step 2.

Read `task.prompt` and pick the best-fit mode:

- **question** — "how does", "why", "where is", "explain",
  "investigate", "what's the deal with…". No code change needed.
- **issue** — "file an issue", "track", "we should open an issue for…",
  "report this bug" (without asking you to fix it).
- **pr** — "fix", "add", "implement", "rename", "refactor",
  "migrate", or anything that implies a code change. Default here
  when the signal is mixed — a PR is strictly more informative than
  an issue draft and lets the human decide.

State your chosen mode in one sentence so the transcript makes the
classification visible.

## Step 2 — Execute

### Mode: question

Goal: produce a concrete, cited answer. Do NOT commit. Do NOT push.

1. Orient — `git log --oneline -5`, `pwd`, skim the tree with a
   light `ls` / targeted grep based on the prompt.
2. Investigate. Prefer reading code and commit history over
   speculation. When pointing at a thing, cite `path/file.py:NN` so
   the reader can jump.
3. Write the answer as your final assistant turn — a few paragraphs
   is fine; include code excerpts when they clarify. Don't dump
   walls of output you didn't read.

Emit at the end:
```json
{
  "status": "completed",
  "mode": "question",
  "message": "one-line summary of the answer",
  "title": "short noun-phrase for the card"
}
```

### Mode: issue

Goal: draft a GitHub issue body the human can open manually. Do NOT
open it yourself (the UI will, on click).

1. Understand the problem. One or two targeted reads is fine —
   don't write a full investigation.
2. Draft title + body. Body uses Markdown with `## Steps to
   reproduce`, `## Expected`, `## Actual`, `## Context` sections as
   the shape fits. Keep it under 300 words unless the prompt
   demands depth.

Emit:
```json
{
  "status": "completed",
  "mode": "issue",
  "message": "draft issue: <title>",
  "title": "<title>",
  "issue_title": "<title>",
  "issue_body": "<markdown body>"
}
```

### Mode: pr

Goal: implement the change, land it on the branch, push, open a PR.

1. Plan briefly — skim the code paths you'll touch. Don't spend
   more than a couple of turns exploring before the first edit.
2. Implement. Scope strictly to what the prompt asks. Don't refactor
   surrounding code that wasn't called out.
3. Verify cheaply — tsc / ruff / pytest on the file if the repo
   uses them. Skip slow full suites. If verification fails, **do
   not commit broken code**; bail to `needs_human` with the failure.
4. Commit:
   ```
   git add <touched files>
   git commit -m "<concise imperative message, 60 chars max>"
   SHA=$(git rev-parse HEAD)
   ```
5. Pre-commit hygiene (if the repo has pre-commit): run on the
   touched files; amend the commit if auto-fixers rewrote anything.
6. Push (unless dry_run):
   ```
   git push --set-upstream origin HEAD:sisyphus/task-{task.id}
   ```
7. Open the PR (unless dry_run):
   ```
   gh pr create \
     --repo {repo.owner}/{repo.name} \
     --title "<task title>" \
     --body "<PR body: brief rationale + ref to the task>" \
     --head sisyphus/task-{task.id}
   ```

Emit:
```json
{
  "status": "completed",
  "mode": "pr",
  "message": "Opened PR <url>",
  "title": "<PR title>",
  "pr_url": "<https://github.com/... URL>",
  "commit_sha": "<sha>"
}
```

In dry_run, emit `status: "skipped_dry_run"` with the would-be
title/body and no `pr_url`.

## Step 3 — Completion schema

Every exit emits the JSON above fenced as ```json ... ```. Valid
`status` values:

- `completed` — work landed as described.
- `skipped_dry_run` — dry run; no side-effects taken.
- `needs_human` — blocked (missing info, failing verification you
  can't fix, ambiguous prompt). Include a `message` explaining why.
- `error` — unexpected failure. Include the error in `message`.

`title` is required on every emission — it's what the task card
shows as its display name. Keep it short (≤60 chars) and specific.

**Always emit `commit_sha` if you made any commits**, regardless of
mode. The Tasks board shows a chip + a "Create PR" button when this
field is present, so question-mode investigations that produced an
incidental fix can still get turned into a PR by the user without
re-running the task.

## Guardrails

### Worktree / branch / repo scope

- Never touch files outside the worktree.
- Never push to branches other than `sisyphus/task-{task.id}`.
- Never open issues or PRs on repos other than `{repo.owner}/{repo.name}`.

### Public-mutation authorization

The user's `task.prompt` is your authorization document. Read it
carefully and ask: did the user *name* the mutation as the goal, or
did they ask you to *recommend* one?

**Authorized — execute directly:**
- Prompt names a specific public mutation as the goal AND names the
  target(s) AND (where applicable) gives a body / reason. Examples:
  - "Open a PR that fixes #N." → push + `gh pr create` is authorized.
  - "Comment 'thanks @alice — landing this' on PR #N." → exact body
    + target named, post as-is.
  - "Close issue #N as not-planned with a polite explanation." →
    drafted body still gets confirmed, but `gh issue close` is
    authorized once the user OKs the body.
  - "Triage PR #N and act on it" — when paired with explicit
    "WAIT for my OK before mutating" (the spawn-pr-task default
    prompt), wait per the prompt's own instruction.

**Unauthorized — DRAFT, ASK, WAIT:**
- Prompt is open-ended ("look at #N and tell me what to do",
  "investigate this thread", "review and recommend").
- Prompt is ambiguous about whether to mutate ("comment if
  needed", "feel free to close if it looks stale").
- You think a mutation is the right move but the prompt didn't
  name it.

In all unauthorized cases:
1. Compose the proposed body / action verbatim in your assistant
   turn so the user sees exactly what would land.
2. Ask in plain English for confirmation — "Should I post this
   comment and close the issue? Reply YES to ship or edit the
   wording first."
3. **WAIT** for the user's reply. Do not call `gh pr comment`,
   `gh issue close`, `gh pr close`, `gh pr edit --add-label`, or
   any similar public-state mutation until they confirm.
4. On confirmation, execute. If the user edits the wording in
   their reply, use their edited version.

The Tasks board's session modal is conversational by design — the
user talks back and forth with you. Lean into that rhythm; treat
public mutations as moves you ask permission for unless the prompt
itself is the permission slip.

### Other rules

- Never invoke other long-running skills from here (no triage, no
  address-review-comments recursion). If the task wants that kind of
  work, bail to `needs_human` and recommend the right skill.
- If the task asks you to do something destructive (delete a repo,
  force-push to main, etc.), refuse and emit `needs_human`.
