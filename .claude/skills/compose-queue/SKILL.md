---
name: compose-queue
description: Translate a natural-language description of a queue into a valid Sisyphus queue YAML block. Works for both creating a new queue from scratch and editing an existing one. Emits a single minimal YAML block the user can review before saving.
worktree_required: false
---

# Compose a queue config from a prompt

You produce ONE queue YAML block that the user will review and save
to `config.yaml`. You do NOT write to disk — the caller handles that.
You are a one-shot translator, not an agent.

## Inputs (runtime context)

- `prompt` — the user's natural-language description. Required.
- `current_yaml` — optional. If present, this is the queue's
  current YAML block; treat the prompt as an edit request and
  preserve any fields the user didn't mention.
- `existing_ids` — list of queue ids already in config. You MUST
  NOT reuse one when composing a new queue.
- `pr` not used here.

## Schema reference

Required keys on every queue block:
- `id` — slug-shaped (lowercase, `a-z 0-9 _ -`, 1–64 chars). Stable;
  the state file is keyed by it.
- `title` — display name used on tabs / headers.
- `initial_state` — the state new cards land in.
- `states` — ordered list of state names (e.g.
  `[in triage, in progress, awaiting update, done]`).

Optional but commonly useful:
- `max_in_flight` — cap on non-done items; default 10 when unsure.
- `done_state` — default `done`.
- `awaiting_state` — the "parked waiting" column, typically
  `awaiting update`.
- `query` — a mapping of GitHub search filters:
  - `author` — login, `self`, or `app/<bot>`.
  - `state` — `open` / `closed` / `merged` / `all`.
  - `review_requested` — login or `self`.
  - `assignee` — login or `self`.
  - `milestone` — name (exact match).
  - `labels` — list of label names; GitHub requires ALL to match.
  - `search` — raw GitHub search-bar syntax (e.g.
    `is:pr is:open updated:<90d sort:updated-asc no:draft`).
    When set, takes over for the structured fields above. Use this
    for filters the structured fields can't express: relative
    date filters (`updated:<90d`, `created:>2026-01-01`), sort
    (`sort:updated-asc`, `sort:created-desc`), draft exclusion
    (`no:draft`), commenters (`commenter:LOGIN`), etc. Prefer
    structured fields when the prompt fits cleanly — they read more
    obviously in YAML. Only emit `search:` when the user names
    operators or filters that need it.

A query MUST narrow beyond `state:`. If you can't extract any
discriminator from the prompt (author, search string, label, etc.),
you're under-specified — either ask the user implicitly (don't
emit) or pick a sensible default like `author: self`.

Per-fetch hydration knobs (separate from `query:`):
- `hydrate.ci_status` (bool) — adds a `gh pr view` per PR; sets
  `ci_status` for triage to branch on. +1 GH API call per PR.
- `hydrate.merge_state` (bool) — adds a GraphQL call per PR;
  sets `mergeStateStatus` + `has_conflicts`.
- `hydrate.review_threads` (bool) — same GraphQL call (free if
  `merge_state` is on); sets `unresolved_threads`.

Post-fetch filters:
- `filter.attention_only` (bool) — drops PRs without conflicts /
  failing CI / unresolved threads. Requires the matching hydration
  toggles to be on; otherwise the filter has nothing to read.
- `filter.non_draft` (bool) — drops draft PRs. No extra API cost.

Emit hydrate / filter only when the prompt asks for them or when
they're load-bearing for the queue's intent (e.g. "PRs needing
attention" → `hydrate.{ci_status, merge_state, review_threads}` +
`filter.attention_only`).

Use the minimum set that expresses the user's intent. Don't emit
empty maps, empty lists, or defaults the user didn't ask for.

## Standard state machine

Unless the user asks for something different, use:
```yaml
initial_state: in triage
done_state: done
awaiting_state: awaiting update
states:
  - in triage
  - in progress
  - awaiting update
  - done
```

Only deviate when the user explicitly asks for different states.

## Procedure

1. If `current_yaml` is set, parse it and treat `prompt` as a delta:
   keep everything the user didn't change, apply their modifications.
2. If composing from scratch, infer a sensible `id` (slug-shaped,
   unique against `existing_ids`) from the user's description. If you
   can't derive a clean id, use `custom-queue`.
3. Build the minimum valid YAML that matches the intent.
4. Sanity-check: `states` must contain `initial_state` and
   `done_state` (and `awaiting_state` if set).
5. Validate the YAML parses back to a mapping.

## Output

Return a single JSON object fenced as ```json ... ```:

```json
{
  "status": "completed | error",
  "yaml": "id: my-prs\ntitle: My PRs\n...",
  "message": "One sentence describing what you did. First person."
}
```

- `yaml` MUST be a string, not a nested object — the caller dumps it
  into a textarea.
- On parse failure or ambiguity, return `status: "error"` with a
  short `message` explaining what's missing from the prompt. Do NOT
  invent values.
- Never include backticks, fences, or prose outside the top-level
  `"yaml"` string. Just the YAML body.

## Examples

**Prompt:** "All Dependabot PRs, including drafts, with failing CI."
```yaml
id: dependabot-failing
title: Failing Dependabot PRs
max_in_flight: 10
initial_state: in triage
done_state: done
awaiting_state: awaiting update
states:
  - in triage
  - in progress
  - awaiting update
  - done
query:
  author: app/dependabot
  state: open
```
(The "failing CI" half is handled by the triage skill's branching;
the query doesn't need to filter on CI status since open Dependabot
PRs get triaged either way.)

**Prompt (edit):** "change the title to 'My Reviews'" with
`current_yaml` present — edit just the title, leave everything
else.

**Prompt:** "give me only PRs labeled 'area:dashboard' by dpgaspar"
```yaml
id: dashboard-dpgaspar
title: Dashboard — dpgaspar
max_in_flight: 10
initial_state: in triage
done_state: done
awaiting_state: awaiting update
states:
  - in triage
  - in progress
  - awaiting update
  - done
query:
  author: dpgaspar
  state: open
  labels:
    - area:dashboard
```
