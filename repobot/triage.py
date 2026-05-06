"""Triage logic for queue items.

## The mechanical-first contract

For every item, two layers run:

1. **Mechanical (rules in code, authoritative).** Reads signals
   already on the item (mergeable, ci_status, has_conflicts,
   review_decision, labels, age, …) and emits the action menu
   directly. Deterministic; tests can exercise it offline; no
   prompt fragility around action lists.

2. **Skill (Claude session, advisory).** Runs in parallel/after
   mechanical. Drafts the *proposal text* the user reads on the
   card and the *comment bodies* (close_comment, nudge_comment,
   approval_comment, suggested_comment, …) that the action modal
   pre-fills. The skill's `actions` field, if emitted, is
   IGNORED — the menu has already been computed.

The skill remains valuable for the things skills are good at:
reading PR bodies and comment threads, drafting language that
matches the situation, classifying ambiguous cases. It just
doesn't make the action-list decision anymore — that lives in
Python and is testable / debuggable.

Skill failure is silent. The card still gets a proposal (from
mechanical) and the menu (from mechanical), with `triage_error`
recorded in `triage_notes` for diagnostics.

`_triage_with_mechanical_first(item, queue_id, skill_name,
mech_func, extra_pr_fields)` is the shared helper every public
triager wraps.
"""
from datetime import datetime, timezone

from . import sessions, worktree
from .config import load_config


STALE_DAYS = 7

# Labels that mean "the maintainers explicitly do not want this PR
# auto-approved or auto-merged." Triage suppresses approve-merge
# from the action menu when any of these is present and proposes
# `await-update` instead. Kept lowercased for case-insensitive
# matching against label names.
_HOLD_LABELS = {
    "hold", "wip", "do-not-merge", "do-not-merge/hold",
    "do not merge", "blocked", "needs-design", "needs-discussion",
    "draft", "rfc", "needs-rebase",
}


def _has_hold_label(raw: dict) -> bool:
    """True iff the PR carries any of the well-known hold/wip/blocked
    labels. Used by mechanical triage to suppress approve-merge
    proposals on explicitly-held PRs."""
    for l in (raw.get("labels") or []):
        name = (l.get("name") or "").strip().lower()
        if name in _HOLD_LABELS:
            return True
    return False


def _hold_label_names(raw: dict) -> list[str]:
    """Return the matching hold-label names so the proposal text can
    cite the actual label that fired the guard."""
    return [
        (l.get("name") or "").strip()
        for l in (raw.get("labels") or [])
        if (l.get("name") or "").strip().lower() in _HOLD_LABELS
    ]


# Bot logins whose review threads almost always represent process /
# CI / coverage / lockfile boilerplate rather than substantive code
# review concerns. The `[bot]` suffix catches generic bot
# integrations; this set covers integrations that have non-suffix
# logins.
_KNOWN_REVIEW_BOTS = {
    "bito-code-review", "coderabbitai", "coderabbitai[bot]",
    "dosu", "dosu[bot]", "sonarcloud", "sonarcloud[bot]", "sonar",
    "codecov", "codecov-commenter", "codecov[bot]",
    "github-actions", "github-actions[bot]", "bitbot",
    "dependabot[bot]",
}

# Body-substring patterns that indicate a bot thread is boilerplate
# (process / coverage / changelog / etc. — not a substantive code
# concern). Case-insensitive match. If any pattern hits AND no
# substantive pattern hits, the thread classifies as boilerplate.
_BOILERPLATE_PATTERNS = (
    "process violation", "process check", "lockfile",
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
    "cargo.lock",
    "coverage", "code coverage", "coverage decreased", "coverage increased",
    "this pr does not satisfy",
    "cla check", "contributor license",
    "changelog entry", "release notes",
    "no issues found", "0 issues", "no findings",
    "lgtm", "looks good",
    "size: large", "size: small", "size: medium",
)

# Body-substring patterns that flip a bot thread from "boilerplate"
# to "substantive" — concrete code concerns we should NOT auto-
# resolve. Case-insensitive.
_SUBSTANTIVE_PATTERNS = (
    "vulnerability", "cve-", "credential", "secret leak",
    "null deref", "null pointer", "n+1 query",
    "regression", "breaking change",
    "memory leak", "race condition", "deadlock",
    "todo:", "fixme:", "xxx:",
    "potential bug", "incorrect logic",
)


def is_bot_login(login: str) -> bool:
    """True when `login` looks like a GitHub bot account — either has
    the `[bot]` suffix (the standard convention) or is in the known-
    bot set."""
    if not login:
        return False
    if login.endswith("[bot]"):
        return True
    return login in _KNOWN_REVIEW_BOTS


def classify_bot_thread(thread: dict) -> str:
    """Classify an unresolved review thread by its first-comment
    author + body. Returns one of:

      - 'human' — first author is a person; auto-resolve never OK.
      - 'boilerplate' — bot author, body matches process/coverage/
        lockfile patterns, no substantive concern. Safe to auto-
        resolve.
      - 'substantive' — bot author but body raises a real concern
        (security, regression, code citation). Block until human
        addresses.
      - 'ambiguous' — bot author but body matches neither pattern
        set. Default to "block" but let the user decide.
    """
    first_author = (thread.get("first_author") or "").strip()
    if not is_bot_login(first_author):
        return "human"
    body = (thread.get("first_body") or "").lower()
    has_substantive = any(p in body for p in _SUBSTANTIVE_PATTERNS)
    if has_substantive:
        return "substantive"
    has_boilerplate = any(p in body for p in _BOILERPLATE_PATTERNS)
    if has_boilerplate:
        return "boilerplate"
    return "ambiguous"


def pick_unblock_action(raw: dict | None, actions: list[str] | None) -> str:
    """Choose the single best 'unblock this PR' action for a card,
    based on its current signals + the actions the mechanical
    triager already put on the menu. Returns the action id (e.g.
    `approve-ci`, `rebase`, `fix-precommit`, `resolve-bot-threads`,
    `approve-merge`) or empty string if no obvious unblock.

    Priority is by *blockingness* — the action whose absence is
    actually preventing merge gets picked first. We require the
    chosen action to already be on the card's actions menu so we
    never propose something the mechanical layer judged
    unavailable (push not allowed, hold label fired, etc.).

    Returns "" when:
      - The PR is held by label (mechanical short-circuited the menu).
      - The PR is in draft state (mark-as-draft / await-update is
        still the right move; "unblock" framing would be misleading).
      - No clear single best move emerges.
    """
    if not raw or not actions:
        return ""
    actions_set = set(actions)
    # The mechanical short-circuit on hold-labels emits exactly
    # ['await-update', 'prompt', 'close', 'skip']. Detect that and
    # return empty — there's no unblock to propose; the label is
    # the explicit hold.
    if "approve-merge" not in actions_set and "rebase" not in actions_set \
            and "fix-precommit" not in actions_set \
            and "attempt-fix" not in actions_set:
        return ""
    if raw.get("isDraft"):
        return ""

    # CI awaiting approval (first-contributor gate). One click and
    # the rest of triage starts working — the highest-leverage
    # unblock by far.
    if "approve-ci" in actions_set and raw.get("needs_ci_approval"):
        return "approve-ci"

    # Bot review threads blocking the merge gate. Resolving the
    # boilerplate ones unsticks approve-merge directly. Place
    # before fix actions because resolving threads is faster and
    # safer than a worktree round-trip.
    if "resolve-bot-threads" in actions_set:
        # Only propose when at least one bot thread classifies as
        # boilerplate or ambiguous — substantive bot threads should
        # NOT be auto-resolved as the unblock move.
        for t in raw.get("unresolved_threads") or []:
            cls = classify_bot_thread(t)
            if cls in ("boilerplate", "ambiguous"):
                return "resolve-bot-threads"

    # Merge conflicts — `rebase` is the right move when push is
    # allowed; the mechanical layer wouldn't have included it
    # otherwise.
    if "rebase" in actions_set and raw.get("has_conflicts"):
        return "rebase"

    # Failing CI — `fix-precommit` is the most common one-shot fix
    # (formatter / linter drift); `attempt-fix` is the broader
    # fallback. We pick fix-precommit when on the menu since the
    # mechanical layer already vetted that push is allowed.
    if (raw.get("ci_status") or "").lower() == "failing":
        if "fix-precommit" in actions_set:
            return "fix-precommit"
        if "attempt-fix" in actions_set:
            return "attempt-fix"

    # All blockers cleared — approve-merge is the unblock.
    if "approve-merge" in actions_set:
        return "approve-merge"

    return ""


def _bot_threads_resolvable(raw: dict) -> bool:
    """True iff this PR has any unresolved review threads from bot
    reviewers — meaning the resolve-bot-threads action would surface
    something for the user to vet. Mechanical triagers add it to
    the action menu in that case so the button is one click away
    from any blocked card."""
    for t in raw.get("unresolved_threads") or []:
        if is_bot_login(t.get("first_author") or ""):
            return True
    return False


def _held_short_circuit(raw: dict) -> tuple[str, list[str]] | None:
    """If the PR carries a hold-label, return the early-return tuple
    every mechanical function uses to skip the rest of its logic.
    Otherwise None. Centralizes the "explicitly held → don't propose
    approve-merge or any auto-fix action" rule so adding it to
    every triager is one-line."""
    if not _has_hold_label(raw):
        return None
    names = _hold_label_names(raw)
    msg = (f"Held by label{'s' if len(names) > 1 else ''}: "
           f"{', '.join(f'`{n}`' for n in names)}. "
           f"Not auto-actionable until removed.")
    return msg, ["await-update", "prompt", "close", "skip"]


def _parse_iso(ts: str | None):
    if not ts:
        return None
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _age_days(raw: dict) -> float | None:
    dt = _parse_iso(raw.get("updatedAt"))
    if dt is None:
        return None
    return (datetime.now(timezone.utc) - dt).total_seconds() / 86400


def mechanical_triage(item: dict) -> tuple[str, list[str]]:
    """Fallback triage using only fields already on the item."""
    raw = item.get("raw") or {}
    mergeable = (raw.get("mergeable") or "").upper()
    ci = (raw.get("ci_status") or "").lower()
    age = _age_days(raw)

    held = _held_short_circuit(raw)
    if held:
        return held

    bot_threads = _bot_threads_resolvable(raw)

    if mergeable == "CONFLICTING":
        # `dependabot-rebase` and `dependabot-recreate` are the
        # natural escalation pair: try a rebase first; fall back to
        # recreate when the bot refuses (typical reason: branch was
        # edited by someone other than Dependabot) or when a prior
        # rebase has already been asked. Surface both so the user
        # can pick without re-triaging.
        msg = ("Merge conflicts detected. Post `@dependabot rebase` "
               "first; if the bot refuses (branch edited externally) "
               "or has already been asked, escalate to "
               "`@dependabot recreate`. Manual `rebase` is the "
               "fallback when neither helps.")
        actions = ["dependabot-rebase", "dependabot-recreate",
                   "rebase", "close"]
        if bot_threads:
            actions.insert(2, "resolve-bot-threads")
        return msg, actions

    if ci == "passing" and mergeable == "MERGEABLE":
        # `mergeable == MERGEABLE` only means no textual conflicts — the PR
        # could still be BLOCKED / BEHIND / UNSTABLE. The approve-merge
        # skill re-verifies mergeStateStatus before acting, so clicking
        # this is safe. We still flag the caveat in the proposal.
        msg = ("CI is green and no textual conflicts — approve-merge will "
               "re-verify mergeStateStatus before approving.")
        actions = ["approve-merge", "prompt", "close"]
        if bot_threads:
            # Surface resolve-bot-threads BEFORE approve-merge: bot
            # threads are a common reason approve-merge bails; one
            # click resolves them, then approve-merge runs clean.
            actions.insert(0, "resolve-bot-threads")
        return msg, actions

    if ci == "pending":
        msg = "Checks are still running. Re-triage on the next refresh."
        return msg, ["retrigger-ci", "prompt", "close"]

    if age is not None and age > STALE_DAYS:
        msg = (
            f"Stale PR (~{age:.0f}d since update) with failing CI. "
            "Consider `@dependabot recreate` or close if obsolete."
        )
        return msg, ["dependabot-recreate", "close", "prompt"]

    msg = "CI is failing. Likely a lockfile drift or flaky test — inspect logs."
    return msg, ["retrigger-ci", "update-lockfile", "rebase", "close", "prompt"]


def _build_context(item: dict, extra_pr_fields: dict | None = None) -> dict:
    """Build the skill session's runtime context. `pr.owner`/`pr.name`
    are derived from (in order): the item's stamped repo, the queue's
    configured repo, then top-level config.yaml `repo`. This is how
    cross-repo queues keep their skill calls pointed at the right
    project."""
    cfg = load_config()
    raw = item.get("raw") or {}
    item_repo = raw.get("repo") if isinstance(raw.get("repo"), dict) else None
    if item_repo and item_repo.get("owner") and item_repo.get("name"):
        owner, name = item_repo["owner"], item_repo["name"]
    else:
        from .github import default_repo_slug
        owner, name = default_repo_slug().split("/", 1)
    pr = {
        "owner": owner,
        "name": name,
        "number": item.get("number"),
        "url": item.get("url"),
        "title": item.get("title"),
        "head_ref": raw.get("headRefName"),
        "mergeable": raw.get("mergeable"),
        "updated_at": raw.get("updatedAt"),
        "is_draft": raw.get("isDraft"),
        "ci_status": raw.get("ci_status"),
    }
    if extra_pr_fields:
        pr.update(extra_pr_fields)
    return {"pr": pr, "identity": (cfg.get("identity") or {})}


def _skill_enrich(skill: str, item: dict, queue_id: str | None = None,
                  extra_pr_fields: dict | None = None
                  ) -> tuple[str | None, dict]:
    """Run a triage skill for enrichment ONLY: returns the proposal
    text and informational notes (comment drafts, classification,
    blockers, etc.) for the card to render.

    The skill's `actions` field is ignored under the mechanical-first
    contract — Python rules compute the action menu. Skills can keep
    emitting an actions array for diagnostic legibility; we just
    don't trust it as authoritative. Returns (None, {}) if the skill
    produced no proposal — caller falls back to the mechanical
    summary for the card's proposal text.

    Raises on session errors so the caller can record `triage_error`
    in the result.
    """
    context = _build_context(item, extra_pr_fields)
    session_id, result = sessions.run_session_blocking(
        skill, context,
        cwd=str(worktree.repo_path()),
        kind="triage",
        queue_id=queue_id,
        item_id=item.get("id"),
    )
    proposal = result.get("proposal")
    if not proposal:
        return None, {"session_id": session_id} if session_id else {}
    # Carry every informational top-level field forward as triage_notes
    # so the UI can render `suggested_comment`, `blockers`, `concerns`,
    # `tests_needed`, etc. without each call-site needing its own
    # extraction. Control fields (proposal, actions, status, meta,
    # action) are excluded; the skill's `notes` dict is merged in last
    # so it wins on key collisions.
    _EXCLUDE = {"proposal", "actions", "status", "action", "meta"}
    notes = {k: v for k, v in result.items() if k not in _EXCLUDE and k != "notes"}
    nested = result.get("notes")
    if isinstance(nested, dict):
        notes.update(nested)
    notes["session_id"] = session_id
    return proposal, notes


def _triage_with_mechanical_first(item: dict, queue_id: str | None,
                                  skill_name: str | None,
                                  mech_func,
                                  extra_pr_fields: dict | None = None
                                  ) -> tuple[str, list[str], dict]:
    """Run the mechanical triage to get the authoritative action menu,
    then (optionally) run the skill for proposal text + notes
    enrichment. Skill failure is silent — the card still gets a
    proposal (from mechanical) and the menu (from mechanical), with
    `triage_error` recorded in notes for diagnostics.

    `skill_name=None` means mechanical-only — no skill call. Useful
    for queues that don't have a skill configured."""
    mech_msg, mech_actions = mech_func(item)
    proposal = mech_msg
    notes: dict = {}
    source = "mechanical"
    triage_session_id: str | None = None

    if skill_name:
        try:
            skill_proposal, skill_notes = _skill_enrich(
                skill_name, item, queue_id=queue_id,
                extra_pr_fields=extra_pr_fields)
            triage_session_id = skill_notes.get("session_id")
            if skill_proposal:
                proposal = skill_proposal
                notes = skill_notes
                source = "mechanical+skill"
            else:
                # Skill emitted nothing usable; keep mechanical's
                # proposal but stash the session id so the user can
                # still inspect the transcript.
                if triage_session_id:
                    notes["session_id"] = triage_session_id
        except Exception as exc:
            notes["triage_error"] = str(exc)

    extra: dict = {"triage_source": source, "triage_notes": notes}
    if triage_session_id:
        extra["triage_session_id"] = triage_session_id
    return proposal, mech_actions, extra


def triage_dependabot_pr(item: dict, queue_id: str | None = None
                         ) -> tuple[str, list[str], dict]:
    """Mechanical-first: rules compute the action menu, skill drafts
    the proposal text + comment bodies. The skill's `actions` field
    is ignored — see `_triage_with_mechanical_first` for rationale."""
    return _triage_with_mechanical_first(
        item, queue_id,
        skill_name="triage-dependabot-pr",
        mech_func=mechanical_triage,
    )


def _mechanical_my_pr_triage(item: dict) -> tuple[str, list[str]]:
    """Fallback triage for my-prs. Used when the skill fails — picks
    one primary action from the three signals and lists the rest as
    fallbacks."""
    raw = item.get("raw") or {}
    held = _held_short_circuit(raw)
    if held:
        return held
    has_conflicts = bool(raw.get("has_conflicts"))
    ci = (raw.get("ci_status") or "").lower()
    threads = raw.get("unresolved_threads") or []
    reasons = []
    actions: list[str] = []
    if has_conflicts:
        reasons.append("merge conflicts")
        actions.append("resolve-conflicts")
    if ci == "failing":
        reasons.append("failing CI")
        actions.extend(["attempt-fix", "fix-precommit", "update-lockfile"])
    if threads:
        reasons.append(f"{len(threads)} unresolved review thread"
                       + ("s" if len(threads) != 1 else ""))
        actions.append("address-comments")
        if _bot_threads_resolvable(raw):
            # Bot threads can be batch-resolved without going through
            # address-comments (which is heavier — drafts a reply per
            # thread). Surface both so the user picks based on whether
            # the bot threads need a reply or just a resolve.
            actions.append("resolve-bot-threads")
    if not actions:
        return ("No blocking signal detected — manual triage.",
                ["prompt"])
    actions.append("prompt")
    # Dedup keeping order.
    seen: set = set()
    ordered = [a for a in actions if not (a in seen or seen.add(a))]
    return f"Needs attention: {', '.join(reasons)}.", ordered


def _mechanical_review_requested_triage(item: dict) -> tuple[str, list[str]]:
    """Mechanical triage for review-requested PRs. Mixes contributor-
    side actions (nudge-author / await-update) with maintainer-side
    fix actions (fix-precommit / attempt-fix / rebase) when pushing
    to the PR's head branch is feasible — i.e. it's an in-repo PR
    OR a fork PR with maintainer_can_modify enabled. Without those
    fix actions on the menu, a maintainer reviewing a green-CI-
    blocked-by-formatting PR has no one-click way to land the auto-
    fix even though their token has push rights."""
    raw = item.get("raw") or {}
    held = _held_short_circuit(raw)
    if held:
        return held
    has_conflicts = bool(raw.get("has_conflicts"))
    ci = (raw.get("ci_status") or "").lower()
    threads = raw.get("unresolved_threads") or []
    is_bot_author = bool((raw.get("author") or {}).get("is_bot")) if isinstance(raw.get("author"), dict) else False
    push_allowed = _can_push_back(raw)
    others = [t for t in threads if t.get("first_author")
              and t["first_author"] != (load_config().get("identity") or {})
              .get("github_username")]
    reasons: list[str] = []
    actions: list[str] = []

    if has_conflicts:
        reasons.append("merge conflicts")
        if push_allowed:
            actions.append("rebase")
        if not is_bot_author:
            actions.append("nudge-author")
        actions.append("await-update")
    if ci == "failing":
        reasons.append("failing CI")
        if push_allowed:
            # Maintainer-side fix paths — same trio as generic-pr /
            # my-pr mechanical. The skills inspect the failing-check
            # rollup themselves and bail cleanly when their assumption
            # doesn't fit.
            actions.extend(["fix-precommit", "attempt-fix", "plan-fix"])
        if not is_bot_author:
            actions.append("nudge-author")
    if others:
        reasons.append(
            f"{len(others)} unresolved thread"
            + ("s" if len(others) != 1 else "")
            + " from others")
        if not is_bot_author:
            actions.append("nudge-author")

    if not reasons:
        msg = "No blockers on signal check — safe to review."
        actions = ["approve-merge", "add-review-comment", "await-update",
                   "close", "prompt", "skip"]
    else:
        msg = "Blockers: " + ", ".join(reasons) + "."
        actions.extend(["add-review-comment", "await-update",
                        "close", "prompt", "skip"])
    if _bot_threads_resolvable(raw):
        # Insert before approve-merge / address-comments so the user
        # can clear bot threads first if approve-merge would bail
        # on them.
        actions.insert(0, "resolve-bot-threads")
    seen: set = set()
    ordered = [a for a in actions if not (a in seen or seen.add(a))]
    return msg, ordered


def _signal_extra_pr_fields(raw: dict) -> dict:
    """Common bundle of signal fields most PR triage skills want
    threaded through their runtime context without a re-fetch."""
    return {
        "has_conflicts": bool(raw.get("has_conflicts")),
        "merge_state_status": raw.get("mergeStateStatus"),
        "unresolved_threads": raw.get("unresolved_threads") or [],
    }


def triage_review_requested_pr(item: dict, queue_id: str | None = None
                                ) -> tuple[str, list[str], dict]:
    """Mechanical-first; skill enriches proposal text + comment drafts.
    Skill's `actions` field is ignored — the menu is computed from
    rules in `_mechanical_review_requested_triage`."""
    return _triage_with_mechanical_first(
        item, queue_id,
        skill_name="triage-review-requested",
        mech_func=_mechanical_review_requested_triage,
        extra_pr_fields=_signal_extra_pr_fields(item.get("raw") or {}),
    )


def triage_my_pr(item: dict, queue_id: str | None = None
                 ) -> tuple[str, list[str], dict]:
    """Mechanical-first; skill enriches proposal text + comment drafts.
    Skill's `actions` field is ignored — the menu is computed from
    rules in `_mechanical_my_pr_triage`."""
    return _triage_with_mechanical_first(
        item, queue_id,
        skill_name="triage-my-pr",
        mech_func=_mechanical_my_pr_triage,
        extra_pr_fields=_signal_extra_pr_fields(item.get("raw") or {}),
    )


# Default skill name used by `triage_generic_pr` when a queue doesn't
# pin one via its `triage_skill` config field. Resolves to
# `.claude/skills/triage-generic-pr/SKILL.md`.
DEFAULT_GENERIC_TRIAGE_SKILL = "triage-generic-pr"


def _resolve_triage_skill(queue_id: str | None) -> str:
    """Return the skill name to invoke for a queue's generic triage.
    Queue YAML may pin one via `triage_skill: …`; otherwise we fall
    back to the bundled `triage-generic-pr` skill."""
    if not queue_id:
        return DEFAULT_GENERIC_TRIAGE_SKILL
    cfg = load_config()
    for q in cfg.get("queues") or []:
        if q.get("id") == queue_id:
            skill = (q.get("triage_skill") or "").strip()
            return skill or DEFAULT_GENERIC_TRIAGE_SKILL
    return DEFAULT_GENERIC_TRIAGE_SKILL


def _can_push_back(raw: dict) -> bool:
    """True when we can push commits back to the PR's head branch —
    either an in-repo PR (we already have origin push) or a fork PR
    where the author opted into maintainer edits. Skills like
    `rebase`, `fix-precommit`, and `attempt-fix` need this to actually
    do anything; without it they bail to `needs_human` and the user
    wastes a click."""
    if not raw.get("is_cross_repository"):
        return True
    return bool(raw.get("maintainer_can_modify"))


def _mechanical_generic_triage(item: dict) -> tuple[str, list[str]]:
    """Signal-based fallback for arbitrary user-defined queues. We don't
    know whether the user is the author, reviewer, or just watching —
    so we propose safe, low-commitment actions and always include
    `prompt` as the human-escape hatch.

    Action priority:
    - Needs CI approval → `approve-ci` primary. One-click unblock.
    - Conflicts → `rebase` if push-allowed, else `nudge-author` /
      `await-update`.
    - Failing CI → `fix-precommit` / `attempt-fix` if push-allowed,
      else `nudge-author` / `await-update`.
    - Unresolved threads → `await-update`.
    - Stale + nothing actionable → `nudge-author`, then `close`.
    - Clean (no blockers) → `approve-merge` as the obvious primary;
      the action dispatcher re-verifies merge state before acting,
      so it's safe to surface even on a fast-path read.
    """
    from .identity import current_user_id
    raw = item.get("raw") or {}
    held = _held_short_circuit(raw)
    if held:
        return held
    has_conflicts = bool(raw.get("has_conflicts"))
    ci = (raw.get("ci_status") or "").lower()
    threads = raw.get("unresolved_threads") or []
    is_draft = bool(raw.get("isDraft"))
    age = _age_days(raw)
    author_login = (raw.get("author") or {}).get("login") if isinstance(raw.get("author"), dict) else None
    is_bot = (raw.get("author") or {}).get("is_bot") if isinstance(raw.get("author"), dict) else False
    needs_ci_approval = bool(raw.get("needs_ci_approval"))
    push_allowed = _can_push_back(raw)
    # Self-merge feasibility check. `reviewDecision` reflects GitHub's
    # branch-protection verdict for the PR — null on repos that don't
    # require reviews, REVIEW_REQUIRED / CHANGES_REQUESTED when an
    # external approver is needed, APPROVED when greenlit. We can't
    # propose approve-merge to the bot's operator on a PR they wrote
    # themselves if branch protection still wants a separate reviewer
    # — GitHub blocks self-approval and would also block the merge.
    me = current_user_id()
    self_authored = (me != "self" and author_login and author_login == me)
    review_decision = (raw.get("reviewDecision") or "").upper()
    review_pending = review_decision in ("REVIEW_REQUIRED", "CHANGES_REQUESTED")

    reasons: list[str] = []
    actions: list[str] = []

    # Highest priority: CI is gated waiting for our click. One button
    # unblocks everything downstream.
    if needs_ci_approval:
        reasons.append("CI awaiting approval")
        actions.append("approve-ci")

    if has_conflicts:
        reasons.append("merge conflicts")
        if push_allowed:
            actions.append("rebase")
        if not is_bot:
            actions.append("nudge-author")
        actions.append("await-update")
    if ci == "failing":
        reasons.append("failing CI")
        if push_allowed:
            # Offer all three CI-fix paths so the user picks based on
            # failure shape: attempt-fix patches in place, plan-fix
            # plans first then patches, fix-precommit handles
            # formatter drift. Skills inspect the rollup themselves
            # and bail cleanly when their assumption doesn't fit.
            actions.extend(["attempt-fix", "plan-fix", "fix-precommit"])
        if not is_bot:
            actions.append("nudge-author")
        actions.append("await-update")
    if threads:
        reasons.append(
            f"{len(threads)} unresolved review thread"
            + ("s" if len(threads) != 1 else ""))
        actions.append("await-update")
    # Stale / draft handling. Two-phase soft-close workflow:
    #
    # 1. **Non-draft stale PR (>30d, someone else's, not a bot):**
    #    Propose `mark-as-draft` primary — that posts a "we may close
    #    this in a future sweep if no further updates" warning and
    #    demotes to draft. `close` stays as a secondary in case the
    #    maintainer would rather just close right away.
    #
    # 2. **Draft stale PR (>90d, someone else's):** likely already
    #    got mark-as-draft'd in a prior pass and never moved. Now
    #    `close` is the right primary — the warning shot has had
    #    its time. The close-pr skill drafts a thankful "thanks for
    #    the PR — feel free to reopen if you want to push it
    #    through" body.
    #
    # Self-authored / bot-authored stale PRs skip the soft-warning
    # step (no point demoting your own PR to draft; bots have their
    # own action paths). They go straight to `close` proposal.

    if is_draft and not self_authored and age is not None and age > 90:
        reasons.append(f"stale draft (~{age:.0f}d since update)")
        actions.append("close")
    elif (not reasons and age is not None and age > 30
          and not is_draft):
        reasons.append(f"stale (~{age:.0f}d since update)")
        if is_bot or self_authored:
            # Skip the soft-warning step — close is the right move.
            actions.append("close")
        else:
            # First-pass warning: demote to draft + post explanation.
            # Close stays as a secondary so the maintainer can choose.
            actions.append("mark-as-draft")
            actions.append("close")
            actions.append("nudge-author")

    # Clean PR (no blockers, not a draft, CI not pending) → propose
    # approve-merge as the obvious next click. The dispatcher
    # re-verifies mergeStateStatus before acting, so this is safe
    # even when our cached signals are slightly stale.
    #
    # Exception: self-authored PR on a repo with branch protection
    # requiring reviews. The bot can't approve its operator's own PR
    # (GitHub blocks self-approval), so proposing approve-merge would
    # only lead to a needs_human bounce. Surface await-update instead
    # — once another reviewer approves, the card auto-unparks and the
    # next triage can propose merge cleanly.
    is_clean = (not reasons
                and not is_draft
                and not has_conflicts
                and ci in ("passing", "")
                and not threads)
    self_merge_blocked = is_clean and self_authored and review_pending
    if is_clean and not self_merge_blocked:
        actions.append("approve-merge")
        msg = "Clean — no blockers detected. Approve-merge is safe to click."
    elif self_merge_blocked:
        actions.append("await-update")
        msg = ("Clean signals, but you're the author and branch "
               "protection requires another reviewer's approval — "
               "parking until someone else greenlights it.")
    elif not reasons:
        msg = "No blocking signal detected — manual triage."
    else:
        msg = "Needs attention: " + ", ".join(reasons) + "."

    # For non-draft PRs that have any blocker AND are someone else's
    # work, mark-as-draft is a soft-warning option that gives the
    # author a clear "you need to move this" without slamming the
    # door. Place it after the stronger fix actions but before
    # close-style escape hatches.
    if (reasons and not is_draft and not is_bot and not self_authored
            and (has_conflicts or ci == "failing" or threads)):
        actions.append("mark-as-draft")

    # Bot review threads (bito / sonarcloud / dosu / coderabbitai /
    # etc.) are a common reason approve-merge bails. Surface a
    # one-click resolver whenever any bot-authored unresolved thread
    # exists; the modal lets the user vet which to actually resolve.
    if _bot_threads_resolvable(raw):
        actions.append("resolve-bot-threads")

    # Universal options always offered. `prompt` is the human escape
    # hatch; `summarize-diff` / `assess-on-worktree` give the user a
    # cheap way to ask for more context before deciding.
    # `mark-as-draft` and `close` are universal "with-comment" escape
    # hatches — the editorial-control modal gates the actual side-
    # effect on a maintainer-edited body, so they're safe to surface
    # even when the card hasn't tripped a specific signal.
    # `mark-as-draft` is gated on `push_allowed` (the action needs
    # write access) and `not is_draft` (no point on already-draft
    # PRs); `close` is unconditional since the GitHub API allows
    # the operator to close anything they can see.
    if push_allowed and not is_draft:
        actions.append("mark-as-draft")
    actions.append("close")
    actions.extend(["summarize-diff", "assess-on-worktree",
                    "prompt", "skip"])
    seen: set = set()
    ordered = [a for a in actions if not (a in seen or seen.add(a))]
    return msg, ordered


def triage_generic_pr(item: dict, queue_id: str | None = None
                      ) -> tuple[str, list[str], dict]:
    """Mechanical-first generic triager for user-defined PR queues.
    Rules in `_mechanical_generic_triage` compute the action menu;
    the queue-configured triage skill (or `triage-generic-pr` by
    default) enriches with proposal text + comment drafts. The
    skill's `actions` field is ignored — see
    `_triage_with_mechanical_first` for rationale."""
    raw = item.get("raw") or {}
    extra_pr = {
        "has_conflicts": bool(raw.get("has_conflicts")),
        "merge_state_status": raw.get("mergeStateStatus"),
        "unresolved_threads": raw.get("unresolved_threads") or [],
        "maintainer_can_modify": bool(raw.get("maintainer_can_modify")),
        "is_cross_repository": bool(raw.get("is_cross_repository")),
        "needs_ci_approval": bool(raw.get("needs_ci_approval")),
        # Branch-protection signal — feeds the self-merge feasibility
        # branch in the priority ladder (mechanical reads it directly,
        # skill threads it for proposal-text reasoning).
        "review_decision": raw.get("reviewDecision"),
        "author_login": (raw.get("author") or {}).get("login")
                        if isinstance(raw.get("author"), dict) else None,
    }
    skill = _resolve_triage_skill(queue_id)
    proposal, actions, extra = _triage_with_mechanical_first(
        item, queue_id,
        skill_name=skill,
        mech_func=_mechanical_generic_triage,
        extra_pr_fields=extra_pr,
    )
    extra["triage_skill"] = skill
    return proposal, actions, extra


# ============================================================
# Issue triage (parallel to the PR functions above, with
# issue-flavored signals and an issue-specific action set).
# ============================================================

DEFAULT_GENERIC_ISSUE_TRIAGE_SKILL = "triage-generic-issue"

# Labels that, when present on an open issue, mean "the maintainers
# already decided this isn't getting fixed" — close-as-stale becomes
# the obvious primary even for relatively young issues.
_DECIDED_OUT_LABELS = {
    "wontfix", "won't fix", "wont-fix", "not-a-bug",
    "duplicate", "invalid", "cant-reproduce", "cannot-reproduce",
}

# Labels that mean "we asked the reporter for something and never
# heard back" — close-as-stale is appropriate after a timeout.
_AWAITING_REPORTER_LABELS = {
    "needs-info", "needs:info", "more-info-needed", "awaiting-response",
}

# Labels that mean "still open by design" — don't propose close.
_KEEP_OPEN_LABELS = {
    "good-first-issue", "good first issue", "help-wanted", "help wanted",
    "discussion", "rfc", "epic", "tracking",
}


def _label_set(raw: dict) -> set[str]:
    out: set[str] = set()
    for l in raw.get("labels") or []:
        name = (l.get("name") or "").strip().lower()
        if name:
            out.add(name)
    return out


def _mechanical_generic_issue_triage(item: dict) -> tuple[str, list[str]]:
    """Signal-based issue triage. The action menu mirrors what a
    human maintainer would reach for on a stale-issue sweep:

    - `close-as-stale` — close the issue with a polite explanatory
      comment.
    - `label-as-stale` — add a `stale` label as a 30-day warning shot
      before close. Useful when the issue might still be valid but
      needs reporter engagement.
    - `nudge-issue-author` — @-mention the reporter to revive the
      thread. Use when there's been recent maintainer engagement
      and the report itself is plausible.
    - `convert-to-discussion` — for "how do I…" / feature-request-
      shaped issues that fit better as discussions.
    - `prompt` / `skip` — universal escape hatches.

    Decision ladder (first match wins for primary):
    1. Decided-out labels (wontfix, duplicate, etc.) → `close-as-stale`.
    2. Awaiting-reporter labels + age > 30d → `close-as-stale`.
    3. Keep-open labels (good-first-issue, help-wanted, rfc) →
       `prompt` primary; offer label-as-stale only if very old.
    4. Stale (>180d, no recent comment) → `label-as-stale` primary
       on the first pass; `close-as-stale` if already labeled stale.
    5. Has recent comments + reporter is the most recent commenter →
       `prompt` primary (a maintainer should weigh in next).
    6. Has recent maintainer comment, no reporter follow-up → `nudge-
       issue-author` primary.
    7. Default (active discussion) → `prompt` primary.
    """
    raw = item.get("raw") or {}
    labels = _label_set(raw)
    age = _age_days(raw)
    last_comment_age = None
    if raw.get("last_comment_at"):
        try:
            dt = _parse_iso(raw["last_comment_at"])
            if dt:
                from datetime import datetime, timezone
                last_comment_age = (datetime.now(timezone.utc) - dt).total_seconds() / 86400
        except Exception:
            pass
    last_commenter = (raw.get("last_commenter") or "").strip()
    author_login = (raw.get("author") or {}).get("login") if isinstance(raw.get("author"), dict) else None
    state_reason = (raw.get("stateReason") or "").upper()

    has_decided_out = bool(labels & _DECIDED_OUT_LABELS)
    has_awaiting = bool(labels & _AWAITING_REPORTER_LABELS)
    has_keep_open = bool(labels & _KEEP_OPEN_LABELS)
    already_stale_labeled = "stale" in labels

    reasons: list[str] = []
    actions: list[str] = []

    if has_decided_out:
        decided_label = next(iter(labels & _DECIDED_OUT_LABELS))
        reasons.append(f"labeled `{decided_label}`")
        actions.append("close-as-stale")
    elif has_awaiting and age is not None and age > 30:
        reasons.append(f"awaiting reporter info for ~{age:.0f}d")
        actions.extend(["close-as-stale", "nudge-issue-author"])
    elif already_stale_labeled and age is not None and age > 60:
        reasons.append(f"labeled `stale`, ~{age:.0f}d old")
        actions.append("close-as-stale")
    elif has_keep_open:
        # Don't auto-close `good-first-issue`, `help-wanted`, etc. —
        # those are meant to stay open. Only suggest stale-labeling
        # if very old AND no recent activity.
        keep_label = next(iter(labels & _KEEP_OPEN_LABELS))
        reasons.append(f"labeled `{keep_label}` (kept open by design)")
        if last_comment_age is not None and last_comment_age > 180:
            actions.append("label-as-stale")
    elif age is not None and age > 180 and (
            last_comment_age is None or last_comment_age > 90):
        reasons.append(f"stale (~{age:.0f}d old, ~{last_comment_age:.0f}d since last comment)"
                       if last_comment_age else f"stale (~{age:.0f}d old, no recent comments)")
        actions.append("label-as-stale")
    elif (last_commenter and author_login
          and last_commenter == author_login
          and last_comment_age is not None and last_comment_age > 30):
        reasons.append(
            f"reporter @{author_login} last spoke ~{last_comment_age:.0f}d ago, no maintainer follow-up")
        # Maintainer should weigh in — keep menu light.
    elif (last_commenter and author_login
          and last_commenter != author_login
          and last_comment_age is not None and last_comment_age > 30):
        reasons.append(
            f"maintainer @{last_commenter} last spoke ~{last_comment_age:.0f}d ago, no reporter follow-up")
        actions.append("nudge-issue-author")

    if not reasons:
        msg = "Active issue — no stale signal."
    else:
        msg = "Triage: " + "; ".join(reasons) + "."

    # `attempt-fix-issue` — only proposed when there isn't already
    # an open PR tackling this issue (don't open a duplicate). Skip
    # for keep-open / decided-out / discussion-shaped issues; those
    # aren't "go fix the bug" candidates.
    linked_prs = raw.get("linked_prs") or []
    has_open_linked_pr = any(
        (lp.get("state") or "").upper() == "OPEN"
        for lp in linked_prs
    )
    safe_to_attempt = (not has_open_linked_pr
                       and not has_decided_out
                       and not has_keep_open)
    if safe_to_attempt:
        actions.append("attempt-fix-issue")

    # Universal options always offered last. Convert-to-discussion
    # is offered freely — it's a soft action and a maintainer can
    # always undo. `close-as-stale` is the universal "close with
    # comment" escape hatch — the editorial-control modal gates
    # the actual close on a maintainer-edited body, so it's safe
    # to surface even on cards that don't trip a stale signal.
    # Skip / prompt are the human escape hatches.
    actions.extend(["nudge-issue-author", "convert-to-discussion",
                    "label-as-stale", "close-as-stale",
                    "prompt", "skip"])
    seen: set = set()
    ordered = [a for a in actions if not (a in seen or seen.add(a))]
    return msg, ordered


def _resolve_issue_triage_skill(queue_id: str | None) -> str:
    if not queue_id:
        return DEFAULT_GENERIC_ISSUE_TRIAGE_SKILL
    cfg = load_config()
    for q in cfg.get("queues") or []:
        if q.get("id") == queue_id:
            skill = (q.get("triage_skill") or "").strip()
            return skill or DEFAULT_GENERIC_ISSUE_TRIAGE_SKILL
    return DEFAULT_GENERIC_ISSUE_TRIAGE_SKILL


def triage_generic_issue(item: dict, queue_id: str | None = None
                         ) -> tuple[str, list[str], dict]:
    """Mechanical-first generic triager for issue queues. Rules in
    `_mechanical_generic_issue_triage` compute the action menu; the
    queue-configured triage skill (or `triage-generic-issue` by
    default) enriches with proposal text + close/nudge/convert
    drafts. The skill's `actions` field is ignored."""
    raw = item.get("raw") or {}
    labels = _label_set(raw)
    extra_pr = {
        "labels": sorted(labels),
        "comments_count": raw.get("comments_count") or 0,
        "last_commenter": raw.get("last_commenter"),
        "last_comment_at": raw.get("last_comment_at"),
        "state_reason": raw.get("stateReason"),
        "author_login": (raw.get("author") or {}).get("login")
                        if isinstance(raw.get("author"), dict) else None,
        # Pass through trimmed comments + body so the skill can read
        # the discussion without a second round-trip.
        "comments": raw.get("comments") or [],
        "body": raw.get("body") or "",
        # Linked PRs (open or closed) — feed both the mechanical
        # `attempt-fix-issue` gate and the skill's reasoning.
        "linked_prs": raw.get("linked_prs") or [],
    }
    skill = _resolve_issue_triage_skill(queue_id)
    proposal, actions, extra = _triage_with_mechanical_first(
        item, queue_id,
        skill_name=skill,
        mech_func=_mechanical_generic_issue_triage,
        extra_pr_fields=extra_pr,
    )
    extra["triage_skill"] = skill
    return proposal, actions, extra
