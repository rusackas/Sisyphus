# Backlog

Running list of ideas / fixes Evan tosses out between sessions. Work
through as time allows; mark done with strike-through or remove the
line. No ceremony — this isn't a roadmap.

## Recently done

- ~~Multi-theme system — full CSS-variable tokenization (palettes
  split from base tokens for typography/radii/motion/elevation).
  Four palettes: `paper` (default warm-cream light), `blueprint`
  (pure-white/navy light), `graphite` (charcoal/electric-blue dark),
  `carbon` (black/amber dark). Theme-picker dropdown in the header
  with swatch previews; `auto` follows `prefers-color-scheme` and
  re-applies on OS theme flip. Persisted via `localStorage` and
  applied pre-paint via an inline head script.~~ 2026-04-23

## Active

- **Named feed templates with bundled `initialPrompt`** — line-cook
  (rusackas/line-cook, `line-chef-initial-setup` branch) had a
  `FEED_TEMPLATES` map: named GitHub query templates (newIssues,
  stalePRs, conflictedPRs, staleIssues, popularDiscussions) each
  carrying the query type (REST/GraphQL), params, and an
  `initialPrompt` string seeding the chef's first message. Sisyphus has
  the query mechanics in `github.py` and queue config in
  `config.yaml`, but no "named feed → prompt" pairing. Worth adding
  to `config.yaml` schema so each queue can carry a default
  `initial_prompt` that seeds triage/action skill calls.
  (Salvaged from line-cook 2026-05-01.)

- **Explicit `approved` gate before AI work** — line-cook's task
  state machine had `queued → approved → in_progress`, requiring a
  human to explicitly approve a task before a chef touched it.
  Sisyphus currently goes straight to work on click. Useful if it
  ever grows
  a backlog/queue-preview mode where you triage a batch first and
  then let sessions drain the approved items. Maps to the Tinder-
  swipe UX from the line-cook vision. (Salvaged from line-cook
  2026-05-01.)

- **`Approval` entity schema for public actions** — line-cook
  defined a formal `Approval` record (taskId, chefId, actionType,
  actionPayload, status: pending/approved/rejected, reviewedAt) for
  every proposed GitHub action (comment, PR, commit). Sisyphus
  handles this today via the review modal + `notes.*_comment`
  triage fields, but without a persistent audit trail. If it ever
  gets a proper DB
  layer or web UI, this schema is a solid starting point for the
  approval log. (Salvaged from line-cook 2026-05-01.)

- **Decouple session pool from web process** — web restart
  currently kills all in-flight Claude sessions because
  `ClaudeSDKClient` spawns a child under uvicorn. Split into a
  long-running `sisyphus-worker` process that owns the session
  pool, talking to the web app over a local socket / HTTP. Fat
  benefit: restart web without losing sessions; transcripts
  persist naturally. Intermediate option: `start_new_session=True`
  on each dispatched subprocess so they survive the parent dying,
  writing progress to a JSONL file. (Proposed 2026-04-22 —
  potentially 1–2 sessions of work for the full refactor; the
  intermediate version might fit in one.)

- **Repair venv shebangs after folder rename** — `.venv/bin/pip`,
  `.venv/bin/uvicorn`, etc. still have `#!/Users/evan_1/GitHub/repobot/.venv/bin/python3`
  baked in. `python3 -m <x>` works as a workaround; the proper fix
  is to recreate the venv or run `virtualenv --upgrade-embed-wheels`.
  (Proposed 2026-04-22.)

- **Split button for PR preview + GitHub** — current drawer trigger
  (click `#NNNN` → drawer, ⌘-click → GitHub) is unintuitive for
  new users. Add a small inline split button next to the number
  with two icons: a "preview" icon (drawer) and an "open in browser"
  icon (↗). Important for eventual public release. (Proposed
  2026-04-22.)

- **Richer "parked since" box** — yellow awaiting-update pill should
  show three timestamps in a tidy layout: (1) when we parked it,
  (2) GitHub's `updatedAt` for the PR, (3) when we last checked for
  an update (stamp this on each refresh). Also: small design pass —
  the current pill is plain text. (Proposed 2026-04-22.)

- **Persist session transcripts across reboots** — after a restart the
  in-memory session registry is empty, so the `session` button
  disappears from every card even though `tokens_lifetime` persists.
  Save each turn's SystemMessage / assistant blocks / tool use to disk
  (indexed by SDK session id) so the transcript modal can rehydrate
  from a file when memory is gone. (Proposed 2026-04-22 — #38019
  showed the UX gap: 278k tok pill with no session button.)

- **`nudge-author` heuristic too conservative** — PR #37120 in
  review-requested didn't get a nudge-author button despite being
  the canonical case: failing CI + a bunch of small review-bot
  touch-ups to consolidate into one polite comment. Tune the
  triage-review-requested skill so "multiple small bot findings
  + failing CI" reliably triggers nudge-author with a terse body
  that enumerates what needs fixing (no hand-holding). (Proposed
  2026-04-22.)

- **`fix-precommit-review` (other people's PRs)** — pre-commit fix
  action for PRs in the review-requested queue. Needs: (1) worktree
  setup hardened for cross-fork PRs via `refs/pull/N/head`, (2) push
  to the upstream configured by `gh pr checkout`, (3) permissions
  gate on `maintainerCanModify`. Open questions left from the
  earlier thread: hide vs. dim the button when
  `maintainerCanModify: false`; reuse `fix-precommit-pr` skill vs.
  fork a second one. (Proposed 2026-04-22.)

## Done

- ~~Board-of-boards IA: one kanban visible at a time, adjacent
  boards peek in from the sides (coverflow-style), tabs across
  the top switch between queues, each queue renders its state
  columns horizontally as a proper kanban. Attention badge on
  each tab counts rank 0..3 items. Keyboard ←/→/h/l and 1..9
  for fast switching; active-tab index persists in localStorage.
  Scraps the earlier inbox+inspector approach in favor of
  context-rich per-queue boards.~~ 2026-04-23

- ~~Inbox-first IA (first attempt, scrapped): attention-ranked
  row stream + right-side inspector. Clicking rows to open
  inspector felt tedious; rows lacked context for decisions.
  Replaced by board-of-boards above.~~ 2026-04-23

- ~~HTMX migration: full-page reloads replaced with partial fragment
  swaps using Idiomorph for DOM-identity-preserving updates. Drawer
  + modals now survive refresh ticks; open <details>, focused
  inputs, and scroll position persist across polls. Forms use
  hx-boost; server middleware converts 303 redirects to 204 under
  HX-Request (plain browsers still redirect normally).~~ 2026-04-23
- ~~Wider UI design pass (frontend-design skill): "instrument panel /
  observatory" direction. Cool steel canvas, archival-blue brand
  color, burnt-ochre attention. Header reworked: ◆ CUSTODIAL·ENGINEER
  title with wide letter-spacing, thin rule under it, monospace
  "readout" row (SESSIONS · TOKENS · WORKTREES) with label/value
  pairs separated by interpunct dividers, settings gear at right.
  Queue headers: uppercase tracked titles in the brand color, 2px
  top accent rule on each queue. Columns: colored tokens per state
  (ochre triage / blue progress / slate awaiting / green done) +
  monospace count in a thin box + tinted stub-rule under each h3.
  Full theme in CSS variables at `:root` so palette swaps in one
  place.~~ 2026-04-23
- ~~Octicons (GitHub's own icon set, MIT) vendored locally under
  `static/icons/`. Jinja `icon(name, size, cls)` helper inlines
  SVGs with `fill="currentColor"` so CSS tints them. PR state icon
  next to `#NNNN` on each card (green open / slate draft),
  gear in settings, sync on update, history on retriage, rocket
  on Start session, comment on Open session, link-external and x
  in the drawer.~~ 2026-04-23
- ~~Card redesign (frontend-design skill): four distinct zones
  (head / proposal+findings / actions / session) separated by
  uppercase-mono eyebrow labels and dashed rules. Status left-rail
  picks up result state (amber idle / red error / green done /
  blue running). Findings summary now previews counts per bucket
  (`FINDINGS · 3 blockers · 2 concerns · log`) so you can scan
  collapsed. Palette stays in slate/blue.~~ 2026-04-23
- ~~Consolidate card clutter (Option A): hid the `skill` triage
  badge (kept the `fallback` pill for mechanical triage — useful
  signal), and hid the `prompt…` toggle when a session button is
  available (modal has its own prompt input). Toggle still appears
  post-reboot when no session exists.~~ 2026-04-23
- ~~Fix `triage-dependabot-pr` to propose `approve-merge` (not
  `prompt`) on BLOCKED + REVIEW_REQUIRED — you're the maintainer
  crossing that gate, not waiting for one. `CHANGES_REQUESTED`
  still routes to `prompt`. The downstream `approve-and-merge-pr`
  skill already handles BLOCKED + REVIEW_REQUIRED safely.~~
  2026-04-23
- ~~UI polish batch: dedupe `skip`/`await-update` buttons from actions
  loop; show PR author (`@login`) next to title on cards; worktree
  count pill in header next to token usage.~~ 2026-04-23
- ~~Triage skills required to emit `prompt` in every `actions`
  list (escape hatch guarantee); updated `triage-my-pr`,
  `triage-review-requested`, `triage-dependabot-pr`.~~ 2026-04-23
- ~~Capture SDK session id at SystemMessage (turn start) instead
  of ResultMessage (turn end), and persist to
  `last_result.meta.session_id` immediately. Interruptions mid-first-turn
  are now resumable (continue button appears, auto-resume-on-boot
  works).~~ 2026-04-23
- ~~PR drawer (Option A): click any `#NNNN` to preview the PR
  in-app. Title / body (sanitized markdown with GH-style issue
  autolinks) / CI checks / reviewers / comments / linked issues.
  ⌘-click / ctrl-click still goes to GitHub. `GET /queues/{q}/
  items/{id}/drawer` renders a Jinja partial. Endpoint uses
  `markdown-it-py` + `bleach` for safe markdown rendering.~~
  2026-04-22

- ~~`request-reviewers` (mechanical candidate picker modal) +
  `ping-reviewers` (editable-body @-mention comment) actions for
  my-prs queue; triage-my-pr emits them on review-gated blockers.~~
  2026-04-22
- ~~Make ⚙︎ gear icons larger (22px → 28px, 14px font → 18px).~~
  2026-04-22
- ~~Rename `~/GitHub/repobot/` → `~/GitHub/CustodialEngineer/` and
  publish as `rusackas/CustodialEngineer`.~~ 2026-04-22
- ~~Author-aware `approval_comment` draft for approve-merge /
  approve-review.~~ 2026-04-22
- ~~Atomic state-file writes (tmp + `os.replace`).~~ 2026-04-22
- ~~`nudge-author` action + skill for review-requested PRs.~~
  2026-04-22
- ~~Inline continue button inside the interrupted result pill.~~
  2026-04-22
- ~~`auto_resume_on_boot` setting under the ⚙︎ gear.~~ 2026-04-22
- ~~Fix `_is_failure` to exclude CANCELLED (misclassified `ci_status`
  as failing for PRs with only a benign cancelled check).~~
  2026-04-22
