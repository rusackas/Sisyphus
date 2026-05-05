"""Thin wrappers around the `gh` CLI. Auth comes from GITHUB_TOKEN / GH_TOKEN.

GitHub's GraphQL gets slow when asking for `statusCheckRollup` across many PRs
at once, so we fetch the list with lightweight fields first, then hydrate
check status per-PR. A small retry loop absorbs transient 5xx responses.
"""
import json
import subprocess
import time
from contextlib import contextmanager
from contextvars import ContextVar

from .config import load_config

LIST_FIELDS = "number,title,url,mergeable,createdAt,updatedAt,headRefName,isDraft,author,reviewDecision,labels"
CHECK_FIELDS = "statusCheckRollup"

# Fields we ask `gh issue list --json` for. `comments` is the heavy
# one — we trim it server-side to keep `raw` reasonable for storage
# and SSE serialization, but we want the actual comment bodies for
# the triage skill to read without a second round-trip.
LIST_FIELDS_ISSUE = (
    "number,title,url,author,state,stateReason,labels,createdAt,updatedAt,"
    "body,comments,assignees,milestone"
)


# `_current_repo_slug` is set by `repo_scope()` at the entry point of
# queue-scoped work (run_queue, action dispatch, API endpoints that
# know their queue). Every gh helper reads from it. Falls back to
# config.yaml's top-level `repo` when unset.
_current_repo_slug: ContextVar[str | None] = ContextVar(
    "sisyphus.repo_slug", default=None)


def _normalize_repo_entry(raw: dict) -> dict:
    """Fill in the derived `slug` and default `id` fields on a raw
    `repos:` list entry. `id` defaults to the slug so unadorned
    entries still work as first-class citizens."""
    owner = (raw.get("owner") or "").strip()
    name = (raw.get("name") or "").strip()
    if not owner or not name:
        return {}
    slug = f"{owner}/{name}"
    return {
        "id": (raw.get("id") or slug).strip(),
        "owner": owner,
        "name": name,
        "display_name": raw.get("display_name") or raw.get("title"),
        "slug": slug,
    }


def list_repos() -> list[dict]:
    """Return the configured repo registry as a list of normalized
    dicts `{id, owner, name, display_name, slug}`.

    Prefers the top-level `repos:` list. Falls back to synthesizing a
    single entry from the legacy top-level `repo:` dict so older
    config files keep working unchanged. Always returns at least one
    entry; raises if the config has neither shape set.
    """
    cfg = load_config()
    raw_list = cfg.get("repos")
    out: list[dict] = []
    if isinstance(raw_list, list) and raw_list:
        for r in raw_list:
            if isinstance(r, dict):
                entry = _normalize_repo_entry(r)
                if entry:
                    out.append(entry)
    if out:
        return out
    legacy = cfg.get("repo") or {}
    entry = _normalize_repo_entry(legacy) if isinstance(legacy, dict) else None
    if entry:
        return [entry]
    raise RuntimeError(
        "No repos configured — set `repos:` (a list of "
        "{id, owner, name}) or the legacy top-level `repo:` in "
        "config.yaml."
    )


def repo_by_id(repo_id: str) -> dict | None:
    """Look up a repo registry entry by its `id` (or its slug, since
    bare entries have `id == slug`). Returns None if unknown."""
    if not repo_id:
        return None
    for r in list_repos():
        if r["id"] == repo_id or r["slug"] == repo_id:
            return r
    return None


def queue_repo_id(queue_cfg: dict) -> str:
    """Return the registry-id of the repo this queue resolves to. If
    the queue's `repo:` field is an id reference, use it. If it's an
    inline slug or dict, look the slug up in the registry and return
    the matching id. Otherwise fall back to the global default repo's
    id. Used by the UI for the repo-filter dropdown — each tab needs
    a stable `data-repo-id` so the filter can compare against the
    selected option."""
    slug = queue_repo_slug(queue_cfg)
    for r in list_repos():
        if r["slug"] == slug:
            return r["id"]
    return slug  # unregistered slug — fall back to the slug as id


def default_repo_slug() -> str:
    """The fallback repo slug when nothing more specific is set.

    Honors `default_repo_id:` at the top level of config.yaml if it
    points at a valid registry entry; otherwise returns the first
    entry in the registry (or the legacy `repo:` if that's all that's
    set)."""
    cfg = load_config()
    did = cfg.get("default_repo_id")
    if did:
        r = repo_by_id(did)
        if r:
            return r["slug"]
    return list_repos()[0]["slug"]


# Kept for any external callers (tests / imports elsewhere) that
# still reach for the underscore-prefixed name. New code should call
# `default_repo_slug()`.
_default_repo_slug = default_repo_slug


def _repo_slug() -> str:
    explicit = _current_repo_slug.get()
    return explicit if explicit else default_repo_slug()


@contextmanager
def repo_scope(slug: str | None):
    """Context manager that pins `_repo_slug()` for the duration of
    a block. Pass None to let the default config-level repo win (no-
    op). Nested scopes stack cleanly thanks to contextvars."""
    if slug:
        token = _current_repo_slug.set(slug)
        try:
            yield
        finally:
            _current_repo_slug.reset(token)
    else:
        yield


def queue_repo_slug(queue_cfg: dict) -> str:
    """Derive the repo slug for a queue's config block. Three shapes
    are accepted for the queue's `repo` field, in precedence order:

    1. A registry id string (e.g. `repo: superset`) — resolves via
       `repo_by_id()` against the top-level `repos:` list.
    2. A bare slug string (e.g. `repo: apache/superset`) — used
       as-is.
    3. A dict (`repo: {owner: apache, name: superset}`) — legacy
       inline form, still supported for back-compat.

    Falls back to `default_repo_slug()` when none of the above match.
    """
    r = queue_cfg.get("repo") if queue_cfg else None
    if isinstance(r, dict) and r.get("owner") and r.get("name"):
        return f"{r['owner']}/{r['name']}"
    if isinstance(r, str):
        if "/" in r:
            return r
        # Registry-id reference — resolve via `repos:` list.
        entry = repo_by_id(r)
        if entry:
            return entry["slug"]
    return default_repo_slug()


def item_repo_slug(item: dict) -> str | None:
    """Derive the repo slug for a fetched item. Items get their repo
    stamped on `raw.repo` at fetch time; older items without the stamp
    return None, and the caller should fall back to the queue's repo."""
    raw = item.get("raw") or {}
    r = raw.get("repo")
    if isinstance(r, dict) and r.get("owner") and r.get("name"):
        return f"{r['owner']}/{r['name']}"
    if isinstance(r, str) and "/" in r:
        return r
    return None


def _gh_json(cmd: list[str], retries: int = 2) -> list | dict:
    last_err = None
    for attempt in range(retries + 1):
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            return json.loads(result.stdout)
        last_err = result.stderr.strip()
        if attempt < retries:
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"gh failed: {' '.join(cmd)}\n{last_err}")


# Rate-limit snapshot. `gh api rate_limit` is itself exempt from the
# rate limit, so polling it is free. Cached for 30s anyway to keep
# per-tick work cheap and steady.
_rate_limit_cache: dict = {"at": 0.0, "data": None}
_RATE_LIMIT_TTL = 30.0


def rate_limit_snapshot(force: bool = False) -> dict | None:
    """Return a compact view of the GitHub REST + GraphQL rate limits.

    Shape: `{core: {used, limit, remaining, reset_at}, graphql: {...}}`.
    Returns None if the `gh` call fails (e.g., offline, missing token).
    Never raises — this is an observability helper, not load-bearing.
    """
    now = time.time()
    if (not force
            and _rate_limit_cache["data"] is not None
            and now - _rate_limit_cache["at"] < _RATE_LIMIT_TTL):
        return _rate_limit_cache["data"]
    try:
        result = subprocess.run(
            ["gh", "api", "rate_limit"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return _rate_limit_cache["data"]
        raw = json.loads(result.stdout).get("resources", {})
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return _rate_limit_cache["data"]

    def _pick(key: str) -> dict | None:
        r = raw.get(key)
        if not isinstance(r, dict):
            return None
        limit = r.get("limit") or 0
        remaining = r.get("remaining") or 0
        return {
            "used": r.get("used", max(limit - remaining, 0)),
            "limit": limit,
            "remaining": remaining,
            "reset_at": r.get("reset"),
        }

    data = {
        "core": _pick("core"),
        "graphql": _pick("graphql"),
        "search": _pick("search"),
    }
    _rate_limit_cache["at"] = now
    _rate_limit_cache["data"] = data
    return data


def list_prs(author: str, state: str = "open", limit: int = 50) -> list[dict]:
    return _gh_json([
        "gh", "pr", "list",
        "--repo", _repo_slug(),
        "--author", author,
        "--state", state,
        "--json", LIST_FIELDS,
        "--limit", str(limit),
    ])


def pr_checks(number: int) -> list[dict]:
    data = _gh_json([
        "gh", "pr", "view", str(number),
        "--repo", _repo_slug(),
        "--json", CHECK_FIELDS,
    ])
    return data.get("statusCheckRollup") or []


def _is_failure(check: dict) -> bool:
    # CANCELLED is intentionally NOT a failure. Superset has manual-gate
    # workflows (e.g. `check-hold-label`) that get cancelled as part of
    # normal operation; GitHub's own "all checks passed" banner ignores
    # CANCELLED for the same reason. If a cancellation represents a real
    # upstream problem, the upstream check will be in FAILURE / TIMED_OUT
    # / ACTION_REQUIRED and we'll catch it there.
    conclusion = (check.get("conclusion") or "").upper()
    state = (check.get("state") or "").upper()
    return conclusion in {"FAILURE", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE"} or state == "FAILURE"


def ci_status(checks: list[dict]) -> str:
    """Collapse the statusCheckRollup to one of {passing, failing, pending}.

    Priority order matters:
    - pending: any check still in flight — we don't know the final verdict
      yet, so defer. A transient CANCELLED (e.g. Superset's
      check-hold-label) would otherwise flip this to "failing" and leak
      cards through the pending-CI triage filter.
    - failing: nothing in flight and at least one failure-terminal check.
    - passing: everything completed successfully (or no checks).
    """
    for c in checks:
        status = (c.get("status") or "").upper()
        state = (c.get("state") or "").upper()
        if status in {"QUEUED", "IN_PROGRESS", "PENDING", "WAITING"} or state == "PENDING":
            return "pending"
    if any(_is_failure(c) for c in checks):
        return "failing"
    return "passing"


def fetch_one_pr(number: int) -> dict:
    """Fetch a single PR with its check rollup in the same shape as
    `fetch_dependabot_prs` emits — used by the per-card refresh button."""
    fields = LIST_FIELDS + "," + CHECK_FIELDS
    pr = _gh_json([
        "gh", "pr", "view", str(number),
        "--repo", _repo_slug(),
        "--json", fields,
    ])
    checks = pr.get("statusCheckRollup") or []
    pr["ci_status"] = ci_status(checks)
    _stamp_repo(pr)
    return pr


def _stamp_repo(pr: dict) -> dict:
    """Stamp the current-scope repo onto a PR dict so downstream
    item-level operations know which repo this PR belongs to. Without
    this, cross-repo queues break at action time."""
    slug = _repo_slug()
    if slug and "/" in slug:
        owner, name = slug.split("/", 1)
        pr["repo"] = {"owner": owner, "name": name}
    return pr


def _build_search_query(query_block: dict) -> str:
    """Translate a queue's `query:` block into a GitHub search string
    suitable for `gh pr list --search`. If the block has a non-empty
    `search:` field, it's used verbatim — power-user mode lets you
    paste any filter you'd type into GitHub's search bar (operators
    like `updated:<90d`, `sort:updated-asc`, `no:draft`, etc.).
    Otherwise, build a basic search string from the structured
    fields the form already collects.

    `self` resolves to `@me` so search strings stay portable across
    accounts.
    """
    if not isinstance(query_block, dict):
        return "is:pr is:open"
    explicit = (query_block.get("search") or "").strip()
    if explicit:
        return explicit

    parts = ["is:pr"]
    state = (query_block.get("state") or "open").lower()
    if state in ("open", "closed", "merged"):
        parts.append(f"is:{state}")
    # state == "all" → no is:* filter

    def _resolve(login: str) -> str:
        if (login or "").lower() == "self":
            return _self_handle()
        return login

    if author := query_block.get("author"):
        parts.append(f"author:{_resolve(author)}")
    if rev := query_block.get("review_requested"):
        parts.append(f"review-requested:{_resolve(rev)}")
    if assignee := query_block.get("assignee"):
        parts.append(f"assignee:{_resolve(assignee)}")
    if milestone := query_block.get("milestone"):
        parts.append(f'milestone:"{milestone}"')
    for label in (query_block.get("labels") or []):
        parts.append(f'label:"{label}"')
    return " ".join(parts)


def query_has_discriminator(query_block: dict | None) -> bool:
    """True iff the query block has at least one filter narrower than
    `state:`. A queue with just `state: open` (or empty) would match
    every open PR in the repo — too permissive; treat it as
    underspecified and refuse to fetch.

    A `search:` string counts as a discriminator (we trust the user
    to have written something narrowing). Author, review_requested,
    assignee, milestone, or any labels also count.
    """
    if not isinstance(query_block, dict):
        return False
    if (query_block.get("search") or "").strip():
        return True
    for k in ("author", "review_requested", "assignee", "milestone"):
        if (query_block.get(k) or "").strip() if isinstance(query_block.get(k), str) \
                else query_block.get(k):
            return True
    if query_block.get("labels"):
        return True
    return False


_HYDRATE_CARRY_FIELDS = (
    "statusCheckRollup", "ci_status", "mergeStateStatus",
    "has_conflicts", "unresolved_threads",
    "maintainer_can_modify", "is_cross_repository",
    "needs_ci_approval",
)


def _carry_prior_hydrate(pr: dict, prior_raw: dict) -> None:
    """Copy hydrated fields from a prior fetch onto a fresh list-only PR
    dict. Used in the updatedAt-unchanged fast-path so we can skip the
    per-PR hydrate API calls without losing the hydration data the
    triager needs."""
    for k in _HYDRATE_CARRY_FIELDS:
        if k in prior_raw and k not in pr:
            pr[k] = prior_raw[k]


def fetch_search(query_block: dict, limit: int = 50,
                 hydrate: dict | None = None,
                 post_filter: dict | None = None,
                 prior_by_number: dict | None = None) -> list[dict]:
    """Generic PR fetcher — runs `gh pr list --search` against the
    current repo scope using a search string built from the queue's
    `query:` block.

    `hydrate` opts into per-PR enrichment (each adds API calls,
    so off by default):
      - `ci_status: true` — `gh pr view` per PR; sets
        `statusCheckRollup` + `ci_status` (passing | failing | pending).
      - `merge_state: true` — GraphQL per PR; sets `mergeStateStatus`
        + `has_conflicts`.
      - `review_threads: true` — same GraphQL call as merge_state
        (free if merge_state is on); sets `unresolved_threads`.

    `prior_by_number` is an optional `{number: prior_raw_dict}` map of
    what we already have on disk. For any PR whose `updatedAt` matches
    its prior — no commits, no comments, no review activity since the
    last fetch — we skip the per-PR hydrate calls entirely and carry
    the hydrated fields forward. This is the rate-limit lifeline: a
    quiet tick costs one search call, not 1 + 2N.

    When CI hydration is requested alongside merge_state or threads,
    a single combined GraphQL call replaces the prior `pr_checks` REST
    hit + `_review_threads` GraphQL pair (the rollup state lives on the
    head commit, free to ride along with the merge/threads query).

    `post_filter` drops PRs after the fetch:
      - `attention_only: true` — keeps only PRs with conflicts /
        failing CI / unresolved threads. Requires the matching
        hydration toggles to actually do anything; otherwise its
        inputs are all None and every PR is dropped.
      - `non_draft: true` — drops `isDraft` PRs.

    Refuses to run if the query is underspecified (no filter beyond
    `state`) — returns `[]` rather than "all open PRs".
    """
    if not query_has_discriminator(query_block):
        # Defensive: refuse to fetch when nothing narrows the result
        # set. Logging here so the empty queue isn't a silent mystery.
        print(f"[fetch_search] refusing underspecified query "
              f"(no filter beyond state): {query_block!r}")
        return []
    hydrate = hydrate or {}
    post_filter = post_filter or {}
    search = _build_search_query(query_block)
    prs = _gh_json([
        "gh", "pr", "list",
        "--repo", _repo_slug(),
        # State is owned by the search query; stop `gh` from
        # double-filtering to open-only by default.
        "--state", "all",
        "--search", search,
        "--json", LIST_FIELDS,
        "--limit", str(limit),
    ])
    if not isinstance(prs, list):
        return []
    wants_threads = bool(hydrate.get("review_threads"))
    wants_merge = bool(hydrate.get("merge_state"))
    wants_ci = bool(hydrate.get("ci_status"))
    # Combined GraphQL covers CI + merge_state + threads in one call
    # whenever any of the GraphQL signals are wanted. Falls back to
    # the REST `pr_checks` path only if CI is the lone hydrate request.
    use_combined_graphql = (wants_merge or wants_threads)
    out: list[dict] = []
    for pr in prs:
        number = pr.get("number")
        prior_raw = (prior_by_number or {}).get(number) or {}
        prior_updated = prior_raw.get("updatedAt")
        unchanged = (prior_updated
                     and pr.get("updatedAt")
                     and pr["updatedAt"] == prior_updated)
        if unchanged:
            # Quiet PR — no signal could have changed since last tick.
            # Skip every per-PR API call and carry the hydrated fields
            # forward intact.
            _carry_prior_hydrate(pr, prior_raw)
            _stamp_repo(pr)
            out.append(pr)
            continue

        if use_combined_graphql and number is not None:
            try:
                h = _hydrate_pr_via_graphql(number)
            except Exception:
                h = {"mergeStateStatus": "", "threads": [], "ci_status": "",
                     "maintainer_can_modify": False,
                     "is_cross_repository": False,
                     "needs_ci_approval": False,
                     "head_sha": "", "comments_count": 0}
            if wants_merge:
                pr["mergeStateStatus"] = h["mergeStateStatus"]
                pr["has_conflicts"] = (
                    pr.get("mergeable") == "CONFLICTING"
                    or h["mergeStateStatus"] == "DIRTY")
            if wants_threads:
                pr["unresolved_threads"] = h["threads"]
            if wants_ci:
                # Rollup-state-derived verdict; statusCheckRollup stays
                # empty here since the modal endpoint does its own
                # per-card fetch when individual check rows are needed.
                pr["statusCheckRollup"] = []
                pr["ci_status"] = h["ci_status"] or "passing"
            # Push-feasibility + CI-approval gate are always cheap
            # additions to the same call — stamp regardless of which
            # hydrate toggles fired the GraphQL.
            pr["maintainer_can_modify"] = h["maintainer_can_modify"]
            pr["is_cross_repository"] = h["is_cross_repository"]
            pr["needs_ci_approval"] = h["needs_ci_approval"]
            # Substantive-unpark fields — see _park_signals in queues.py.
            pr["head_sha"] = h.get("head_sha") or ""
            pr["comments_count"] = h.get("comments_count") or 0
        elif wants_ci and number is not None:
            try:
                checks = pr_checks(number)
            except Exception:
                checks = []
            pr["statusCheckRollup"] = checks
            pr["ci_status"] = ci_status(checks)
        _stamp_repo(pr)
        out.append(pr)

    if post_filter.get("non_draft"):
        out = [p for p in out if not p.get("isDraft")]
    if post_filter.get("attention_only"):
        out = [p for p in out
               if p.get("has_conflicts")
               or p.get("ci_status") == "failing"
               or p.get("unresolved_threads")]
    return out


_ISSUE_LINKED_PRS_QUERY = """
query($owner:String!,$name:String!,$number:Int!){
  repository(owner:$owner,name:$name){
    issue(number:$number){
      timelineItems(itemTypes:[CROSS_REFERENCED_EVENT], last:30){
        nodes{
          ... on CrossReferencedEvent{
            isCrossRepository
            source{
              __typename
              ... on PullRequest{
                number url state isDraft title
                repository{ nameWithOwner }
              }
            }
          }
        }
      }
    }
  }
}
"""


def _fetch_linked_prs(issue_number: int) -> list[dict]:
    """Find PRs that reference this issue via timeline cross-
    references. Filters to same-repo PRs (cross-repo references are
    surfaced too but we don't act on them — the maintainer's repo
    is the actionable surface). Returns slim dicts with what the
    card chip needs."""
    owner, name = _repo_slug().split("/", 1)
    try:
        data = _gh_json([
            "gh", "api", "graphql",
            "-f", f"query={_ISSUE_LINKED_PRS_QUERY}",
            "-F", f"owner={owner}",
            "-F", f"name={name}",
            "-F", f"number={issue_number}",
        ])
    except Exception:
        return []
    issue = (((data.get("data") or {}).get("repository") or {})
             .get("issue") or {})
    nodes = ((issue.get("timelineItems") or {}).get("nodes") or [])
    seen: set = set()
    out: list[dict] = []
    for n in nodes:
        if n.get("isCrossRepository"):
            # Cross-repo references are typically less actionable.
            # Skip them on the basic chip render; the maintainer can
            # always click through via the issue itself.
            continue
        src = n.get("source") or {}
        if src.get("__typename") != "PullRequest":
            continue
        pr_num = src.get("number")
        if not pr_num or pr_num in seen:
            continue
        seen.add(pr_num)
        out.append({
            "number": pr_num,
            "url": src.get("url"),
            "state": src.get("state") or "",
            "is_draft": bool(src.get("isDraft")),
            "title": src.get("title") or "",
        })
    return out


def fetch_issues_search(query_block: dict, limit: int = 50,
                        post_filter: dict | None = None,
                        hydrate_linked_prs: bool = True) -> list[dict]:
    """Issue counterpart to `fetch_search`. Runs `gh issue list
    --search` against the current repo scope and returns one dict per
    issue with the issue-flavored signal fields:

      - `comments_count`, `last_comment_at`, `last_commenter` (derived
        from the trimmed `comments` array).
      - `labels` already on the raw output (list of `{name, color}`).
      - `state`, `stateReason` for closed-vs-open + close reason.

    Refuses to run on an underspecified query — same guardrail as
    `fetch_search`. Coerces `is:issue` into the search string if the
    caller forgot it (so a queue with just `state: open` doesn't
    accidentally fetch PRs too).

    `post_filter`:
      - `non_draft: true` is a no-op for issues (no draft state on
        issues — kept silently for queue-config symmetry).
    """
    if not query_has_discriminator(query_block):
        print(f"[fetch_issues_search] refusing underspecified query "
              f"(no filter beyond state): {query_block!r}")
        return []
    post_filter = post_filter or {}
    search = _build_search_query(query_block)
    # GitHub's `gh issue list --search` is implicitly issue-scoped,
    # but `_build_search_query` may have produced a generic `is:open`
    # string. Add `is:issue` defensively so a search like
    # `state:open sort:updated-asc` doesn't get any PR contamination
    # if someone reuses this on `gh search issues` in the future.
    if "is:issue" not in search:
        search = ("is:issue " + search).strip()
    issues = _gh_json([
        "gh", "issue", "list",
        "--repo", _repo_slug(),
        "--state", "all",
        "--search", search,
        "--json", LIST_FIELDS_ISSUE,
        "--limit", str(limit),
    ])
    if not isinstance(issues, list):
        return []
    out: list[dict] = []
    for i in issues:
        comments = i.get("comments") or []
        i["comments_count"] = len(comments)
        if comments:
            last = comments[-1]
            i["last_comment_at"] = last.get("createdAt")
            i["last_commenter"] = (last.get("author") or {}).get("login")
        else:
            i["last_comment_at"] = None
            i["last_commenter"] = None
        # Slim comments to the last 10, with body bodies truncated —
        # the triage skill only really needs recent thread context,
        # and storing every comment in the state file is wasteful.
        i["comments"] = [
            {
                "author": (c.get("author") or {}).get("login"),
                "authorAssociation": c.get("authorAssociation"),
                "createdAt": c.get("createdAt"),
                "body": (c.get("body") or "")[:1500],
            }
            for c in comments[-10:]
        ]
        # Marker that downstream triage / runner / templates branch on.
        i["kind"] = "issue"
        # Linked-PR detection: one GraphQL call per issue, ~free
        # at the queue scale we run at. Stamped on raw so the card
        # template can render link chips and the triage skill /
        # mechanical can branch on "already has a linked PR" → skip
        # attempt-fix-issue (don't open a duplicate).
        if hydrate_linked_prs and i.get("number"):
            try:
                i["linked_prs"] = _fetch_linked_prs(int(i["number"]))
            except Exception:
                i["linked_prs"] = []
        else:
            i["linked_prs"] = []
        _stamp_repo(i)
        out.append(i)
    return out


def fetch_dependabot_prs(limit: int = 50,
                         prior_by_number: dict | None = None) -> list[dict]:
    """All open Dependabot PRs with hydrated check rollups, sorted oldest-
    update-first so long-languishing PRs get triage attention first.

    `prior_by_number` lets us skip the per-PR `pr_checks` call when a
    PR's `updatedAt` is unchanged — Dependabot's churn is bursty (a
    rebase storm can touch many PRs) but most ticks the set is quiet.
    """
    prs = list_prs(author="app/dependabot", state="open", limit=limit)
    prs.sort(key=lambda p: p.get("updatedAt") or "")
    out = []
    for pr in prs:
        prior_raw = (prior_by_number or {}).get(pr["number"]) or {}
        prior_updated = prior_raw.get("updatedAt")
        if prior_updated and pr.get("updatedAt") == prior_updated:
            _carry_prior_hydrate(pr, prior_raw)
            _stamp_repo(pr)
            out.append(pr)
            continue
        checks = pr_checks(pr["number"])
        pr["statusCheckRollup"] = checks
        pr["ci_status"] = ci_status(checks)
        _stamp_repo(pr)
        out.append(pr)
    return out


# Legacy alias — kept so any external caller referencing the old name
# still works. New code should use `fetch_dependabot_prs`.
def fetch_failing_dependabot_prs(limit: int = 50) -> list[dict]:
    return [p for p in fetch_dependabot_prs(limit=limit)
            if p.get("ci_status") == "failing"]


_RESOLVE_THREAD_MUTATION = """
mutation($threadId:ID!){
  resolveReviewThread(input:{threadId:$threadId}){
    thread{ id isResolved }
  }
}
"""


def resolve_review_thread(thread_node_id: str) -> None:
    """Mark a review thread resolved via GraphQL. Thread IDs are the
    GraphQL node ids (the `id` field on reviewThreads.nodes), not
    comment ids."""
    if not thread_node_id:
        raise ValueError("empty thread id")
    result = subprocess.run(
        ["gh", "api", "graphql",
         "-f", f"query={_RESOLVE_THREAD_MUTATION}",
         "-F", f"threadId={thread_node_id}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"gh api graphql (resolveReviewThread {thread_node_id[:8]}…) "
            f"failed: {result.stderr.strip()}"
        )


def pr_push_info(pr_number: int) -> dict:
    """Return the info needed to push back to a PR's head branch:
      - head_ref: the branch name
      - is_cross_repository: True iff the PR is from a fork
      - maintainer_can_modify: True if the author enabled "Allow
        edits and access to secrets by maintainers" (GitHub grants
        your token push access to the fork in that case)
      - head_repo: {owner, name} of the head repo (usually the fork)

    Used by action dispatch to decide whether we CAN push, and to
    set up the right push remote before the skill runs.
    """
    data = _gh_json([
        "gh", "pr", "view", str(pr_number),
        "--repo", _repo_slug(),
        "--json",
        "headRepository,headRepositoryOwner,headRefName,"
        "maintainerCanModify,isCrossRepository",
    ])
    head_repo = data.get("headRepository") or {}
    head_owner = (data.get("headRepositoryOwner") or {}).get("login") or ""
    return {
        "head_ref": data.get("headRefName"),
        "is_cross_repository": bool(data.get("isCrossRepository")),
        "maintainer_can_modify": bool(data.get("maintainerCanModify")),
        "head_repo": {
            "owner": head_owner,
            "name": head_repo.get("name"),
        },
    }


def ensure_push_remote(pr_number: int, worktree_path) -> tuple[str, str]:
    """Set up the right push target for a PR inside its worktree.
    Returns (remote_name, head_ref).

    - For in-repo PRs: returns ("origin", head_ref). Nothing to
      configure — the existing clone already has the branch in origin.
    - For fork PRs with maintainerCanModify: adds (or updates) a
      remote named `pr-fork-<N>` pointing at the fork, returns that
      name. Your GITHUB_TOKEN has push rights to the fork thanks to
      the maintainer-edits grant.
    - For fork PRs without maintainerCanModify: raises RuntimeError.
      The skill should bail with `needs_human` — the only legitimate
      recourse is to ask the PR author to push the fix themselves.
    """
    info = pr_push_info(pr_number)
    head_ref = info["head_ref"]
    if not info["is_cross_repository"]:
        return "origin", head_ref
    if not info["maintainer_can_modify"]:
        raise RuntimeError(
            "Maintainer edits are disabled on this fork PR — we can't "
            "push. Consider nudge-author instead."
        )
    fork_owner = info["head_repo"]["owner"]
    fork_name = info["head_repo"]["name"]
    remote = f"pr-fork-{pr_number}"
    url = f"https://github.com/{fork_owner}/{fork_name}.git"
    from pathlib import Path
    wt = str(worktree_path) if not isinstance(worktree_path, Path) else str(worktree_path)
    check = subprocess.run(
        ["git", "-C", wt, "remote", "get-url", remote],
        capture_output=True, text=True,
    )
    if check.returncode != 0:
        subprocess.run(
            ["git", "-C", wt, "remote", "add", remote, url],
            check=True, capture_output=True,
        )
    else:
        subprocess.run(
            ["git", "-C", wt, "remote", "set-url", remote, url],
            check=True, capture_output=True,
        )
    return remote, head_ref


def post_pr_comment(pr_number: int, body: str) -> None:
    """Post a top-level comment on a PR. Shells out to `gh pr comment`
    so the body can carry any multiline / markdown / @-mentions
    without shell quoting hell."""
    if not body or not body.strip():
        raise ValueError("empty comment body")
    result = subprocess.run(
        ["gh", "pr", "comment", str(pr_number),
         "--repo", _repo_slug(),
         "--body-file", "-"],
        input=body,
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh pr comment failed: {result.stderr.strip()}")


def post_review_reply(pr_number: int, first_comment_id: int, body: str) -> None:
    """Post an inline reply on an existing PR review thread. JSON-encoded
    body goes via stdin so newlines and quotes survive intact. Raises
    RuntimeError on non-zero exit."""
    endpoint = (f"/repos/{_repo_slug()}/pulls/{pr_number}"
                f"/comments/{first_comment_id}/replies")
    result = subprocess.run(
        ["gh", "api", "--method", "POST",
         "-H", "Accept: application/vnd.github+json",
         endpoint, "--input", "-"],
        input=json.dumps({"body": body}),
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"gh api reply (comment {first_comment_id}) failed: "
            f"{result.stderr.strip()}"
        )


# ---------- "my PRs" queue ----------
#
# Pulls the user's own open non-draft PRs that need attention:
# any of (merge conflicts, failing CI, unresolved review threads).
# Unresolved threads aren't exposed on `gh pr view --json`; the graphql
# call below is the authoritative source.

_REVIEW_THREADS_QUERY = """
query($owner:String!,$name:String!,$number:Int!){
  repository(owner:$owner,name:$name){
    pullRequest(number:$number){
      mergeStateStatus
      maintainerCanModify
      isCrossRepository
      comments{ totalCount }
      headRefOid
      commits(last:1){nodes{commit{statusCheckRollup{
        state
        contexts(first:50){nodes{
          __typename
          ... on CheckRun{ name status conclusion }
          ... on StatusContext{ context state }
        }}
      }}}}
      reviewThreads(first:50){
        nodes{
          id
          isResolved
          isOutdated
          path
          line
          comments(first:5){nodes{author{login} body createdAt}}
        }
      }
    }
  }
}
"""


# Map GraphQL StatusState to our compact ci_status verdict. ci_status()
# (the per-context REST path) considers CANCELLED non-fatal; the GraphQL
# rollup state already reflects GitHub's own banner logic, which also
# treats CANCELLED as non-failure, so the verdicts agree.
def _rollup_state_to_ci_status(state: str | None) -> str:
    s = (state or "").upper()
    if s in ("PENDING", "EXPECTED"):
        return "pending"
    if s in ("FAILURE", "ERROR"):
        return "failing"
    # SUCCESS or empty (no checks configured) — treat as passing.
    return "passing"


def _hydrate_pr_via_graphql(number: int) -> dict:
    """Single combined hydrate call. Returns:

      {
        "mergeStateStatus": "CLEAN" | "DIRTY" | "BLOCKED" | …,
        "threads": [...],                # unresolved review threads
        "ci_status": "passing" | "failing" | "pending",
        "maintainer_can_modify": bool,
        "is_cross_repository": bool,
        "needs_ci_approval": bool,       # any CheckRun WAITING / ACTION_REQUIRED
      }

    One GraphQL call covers every signal — merge state, review threads,
    CI verdict, push-feasibility, and the first-time-contributor "Approve
    and run workflow" gate. Pulling all of these together is what made
    the rate-limit-friendly path possible.

    `needs_ci_approval` is best-effort: GitHub flags the gate via
    `CheckRun.status == WAITING` (suite registered but waiting for a
    maintainer click) and via `conclusion == ACTION_REQUIRED` (post-
    approval state for some flows). Either firing means the user can
    likely click "Approve and run workflow" on the PR.
    """
    # Honor the ContextVar scope first — cross-repo queues pin the
    # slug there. Only fall through to the config default when
    # unscoped.
    owner, name = _repo_slug().split("/", 1)
    data = _gh_json([
        "gh", "api", "graphql",
        "-f", f"query={_REVIEW_THREADS_QUERY}",
        "-F", f"owner={owner}",
        "-F", f"name={name}",
        "-F", f"number={number}",
    ])
    pr = (data.get("data") or {}).get("repository", {}).get("pullRequest") or {}
    mss = pr.get("mergeStateStatus") or ""
    threads = (pr.get("reviewThreads") or {}).get("nodes") or []
    commit_nodes = ((pr.get("commits") or {}).get("nodes") or [])
    rollup = (((commit_nodes[0] or {}).get("commit") or {}).get("statusCheckRollup")
              if commit_nodes else None) or {}
    ci = _rollup_state_to_ci_status(rollup.get("state"))
    contexts = ((rollup.get("contexts") or {}).get("nodes") or [])
    needs_approval = False
    for c in contexts:
        if c.get("__typename") != "CheckRun":
            continue
        status = (c.get("status") or "").upper()
        conclusion = (c.get("conclusion") or "").upper()
        if status == "WAITING" or conclusion == "ACTION_REQUIRED":
            needs_approval = True
            break

    slim = []
    for t in threads:
        if t.get("isResolved"):
            continue
        comments = ((t.get("comments") or {}).get("nodes") or [])
        first = comments[0] if comments else {}
        slim.append({
            "id": t.get("id"),
            "path": t.get("path"),
            "line": t.get("line"),
            "is_outdated": bool(t.get("isOutdated")),
            "first_author": (first.get("author") or {}).get("login"),
            "first_body": (first.get("body") or "")[:2000],
            "comments_count": len(comments),
        })
    return {
        "mergeStateStatus": mss,
        "threads": slim,
        "ci_status": ci,
        "maintainer_can_modify": bool(pr.get("maintainerCanModify")),
        "is_cross_repository": bool(pr.get("isCrossRepository")),
        "needs_ci_approval": needs_approval,
        # Substantive-event tracking: head SHA bumps on every commit
        # push; comments_count bumps on every top-level conversation
        # comment. Both feed the auto-unpark "did anything real
        # happen?" gate so awaiting-update cards don't unpark on
        # trivial label/metadata churn.
        "head_sha": pr.get("headRefOid") or "",
        "comments_count": ((pr.get("comments") or {}).get("totalCount") or 0),
    }


# Legacy tuple-shape wrapper kept so callers that haven't migrated
# yet keep working. New code should call `_hydrate_pr_via_graphql`
# and read the additional fields off the returned dict.
def _review_threads(number: int) -> tuple[str, list[dict], str]:
    h = _hydrate_pr_via_graphql(number)
    return h["mergeStateStatus"], h["threads"], h["ci_status"]


def _self_handle() -> str:
    """The PR author handle we filter by in GitHub search syntax.
    Routes through `identity.current_user_id()` so the multi-user
    contract is honored in one place; falls back to GitHub's `@me`
    keyword when no user is configured (matches `gh`'s own behavior
    for an unauthenticated handle slot)."""
    from .identity import current_user_id
    handle = current_user_id()
    return "@me" if handle == "self" else handle


def list_review_requested_prs(limit: int = 50) -> list[dict]:
    """Open PRs where the authenticated user is a requested reviewer.
    `gh pr list --search "review-requested:@me"` is equivalent to
    querying the user's review-request inbox and is the canonical
    source — it respects both individual and team review requests."""
    cfg = load_config()
    handle = (cfg.get("identity") or {}).get("github_username") or "@me"
    # `review-requested` accepts a handle or @me; we use the stored
    # handle so the filter still works when the server is auth'd as a
    # different account (e.g. a bot PAT).
    search = f"review-requested:{handle}"
    return _gh_json([
        "gh", "pr", "list",
        "--repo", _repo_slug(),
        "--state", "open",
        "--search", search,
        "--json", LIST_FIELDS,
        "--limit", str(limit),
    ])


def fetch_review_requested_prs(limit: int = 50,
                               prior_by_number: dict | None = None) -> list[dict]:
    """Open PRs requesting review from the configured user, hydrated
    with CI status, mergeStateStatus, and the list of unresolved review
    threads (from any author) — the triage skill uses those to tell
    whether OTHERS' feedback is still pending before you approve.

    Returns PRs sorted oldest-update-first (longest-waiting gets
    triaged first). Draft PRs are excluded.

    `prior_by_number`: same updatedAt-fast-path as `fetch_search`. CI,
    merge_state, and review threads all come from one combined GraphQL
    call — so a quiet PR avoids one REST hit and one GraphQL hit.
    """
    prs = list_review_requested_prs(limit=limit)
    prs = [p for p in prs if not p.get("isDraft")]
    prs.sort(key=lambda p: p.get("updatedAt") or "")

    out: list[dict] = []
    for pr in prs:
        number = pr["number"]
        prior_raw = (prior_by_number or {}).get(number) or {}
        prior_updated = prior_raw.get("updatedAt")
        if prior_updated and pr.get("updatedAt") == prior_updated:
            _carry_prior_hydrate(pr, prior_raw)
            _stamp_repo(pr)
            out.append(pr)
            continue
        try:
            h = _hydrate_pr_via_graphql(number)
        except Exception:
            h = {"mergeStateStatus": "", "threads": [], "ci_status": "",
                 "maintainer_can_modify": False,
                 "is_cross_repository": False,
                 "needs_ci_approval": False,
                 "head_sha": "", "comments_count": 0}
        pr["statusCheckRollup"] = []
        pr["ci_status"] = h["ci_status"] or "passing"
        pr["mergeStateStatus"] = h["mergeStateStatus"]
        pr["unresolved_threads"] = h["threads"]
        pr["has_conflicts"] = (pr.get("mergeable") == "CONFLICTING"
                               or h["mergeStateStatus"] == "DIRTY")
        pr["maintainer_can_modify"] = h["maintainer_can_modify"]
        pr["is_cross_repository"] = h["is_cross_repository"]
        pr["needs_ci_approval"] = h["needs_ci_approval"]
        pr["head_sha"] = h.get("head_sha") or ""
        pr["comments_count"] = h.get("comments_count") or 0
        _stamp_repo(pr)
        out.append(pr)
    return out


def fetch_my_prs(limit: int = 50,
                 prior_by_number: dict | None = None) -> list[dict]:
    """Open, non-draft PRs authored by the configured user that need
    attention — any of: merge conflicts, failing CI, unresolved review
    threads. Each returned PR has:
      - ci_status: passing | failing | pending
      - unresolved_threads: list of {id, path, line, first_author, first_body, is_outdated}
      - has_conflicts: bool
      - mergeStateStatus: from graphql
    PRs with none of the three signals are excluded.

    `prior_by_number` enables the updatedAt-unchanged fast-path; quiet
    PRs are carried forward without per-PR API calls.
    """
    prs = list_prs(author=_self_handle(), state="open", limit=limit)
    prs = [p for p in prs if not p.get("isDraft")]
    prs.sort(key=lambda p: p.get("updatedAt") or "")

    out: list[dict] = []
    for pr in prs:
        number = pr["number"]
        prior_raw = (prior_by_number or {}).get(number) or {}
        prior_updated = prior_raw.get("updatedAt")
        if prior_updated and pr.get("updatedAt") == prior_updated:
            _carry_prior_hydrate(pr, prior_raw)
            # Apply the same attention filter to carried-forward PRs.
            if (pr.get("has_conflicts") or pr.get("ci_status") == "failing"
                    or pr.get("unresolved_threads")):
                _stamp_repo(pr)
                out.append(pr)
            continue
        try:
            h = _hydrate_pr_via_graphql(number)
        except Exception:
            h = {"mergeStateStatus": "", "threads": [], "ci_status": "",
                 "maintainer_can_modify": False,
                 "is_cross_repository": False,
                 "needs_ci_approval": False,
                 "head_sha": "", "comments_count": 0}
        pr["statusCheckRollup"] = []
        pr["ci_status"] = h["ci_status"] or "passing"
        pr["mergeStateStatus"] = h["mergeStateStatus"]
        pr["unresolved_threads"] = h["threads"]
        pr["has_conflicts"] = (pr.get("mergeable") == "CONFLICTING"
                               or h["mergeStateStatus"] == "DIRTY")
        pr["maintainer_can_modify"] = h["maintainer_can_modify"]
        pr["is_cross_repository"] = h["is_cross_repository"]
        pr["needs_ci_approval"] = h["needs_ci_approval"]
        pr["head_sha"] = h.get("head_sha") or ""
        pr["comments_count"] = h.get("comments_count") or 0
        if (pr["has_conflicts"] or pr["ci_status"] == "failing"
                or h["threads"]):
            _stamp_repo(pr)
            out.append(pr)
    return out


_BOT_SUFFIX = "[bot]"


_COLLABS_TTL_SEC = 600  # 10 min
_collabs_cache: dict = {"at": 0.0, "slug": "", "logins": set(), "records": []}


def _refresh_collaborators() -> None:
    """Fetch + cache the repo's write-access collaborator list. Stores
    both the lowercase-login set (for membership checks) and a list of
    `{login, avatar_url}` dicts (for UI rendering).
    """
    slug = _repo_slug()
    logins: set[str] = set()
    records: list[dict] = []
    page = 1
    while True:
        try:
            batch = _gh_json([
                "gh", "api",
                f"/repos/{slug}/collaborators",
                "-X", "GET",
                "-f", "per_page=100",
                "-f", f"page={page}",
                "-f", "affiliation=all",
            ])
        except Exception:
            break
        if not isinstance(batch, list) or not batch:
            break
        for c in batch:
            login = (c.get("login") or "").strip()
            perms = c.get("permissions") or {}
            if login and (perms.get("push") or perms.get("admin")
                          or perms.get("maintain")):
                logins.add(login.lower())
                records.append({
                    "login": login,
                    "avatar_url": c.get("avatar_url"),
                })
        if len(batch) < 100:
            break
        page += 1
    _collabs_cache.update({
        "at": time.time(), "slug": slug,
        "logins": logins, "records": records,
    })


def collaborator_logins(force: bool = False) -> set[str]:
    """Return the lowercase logins of anyone in the repo's collaborator
    list who has push or admin rights — i.e., the set of people who
    can legitimately be requested as reviewers. Cached for 10 min to
    avoid hammering the API; the cache is keyed by repo slug so a
    config change invalidates automatically.
    """
    slug = _repo_slug()
    if (not force
            and _collabs_cache["slug"] == slug
            and time.time() - _collabs_cache["at"] < _COLLABS_TTL_SEC):
        return _collabs_cache["logins"]
    _refresh_collaborators()
    return _collabs_cache["logins"]


def collaborator_records(force: bool = False) -> list[dict]:
    """List of `{login, avatar_url}` for each write-access collaborator.
    Same cache as `collaborator_logins()`."""
    slug = _repo_slug()
    if (not force
            and _collabs_cache["slug"] == slug
            and time.time() - _collabs_cache["at"] < _COLLABS_TTL_SEC):
        return list(_collabs_cache["records"])
    _refresh_collaborators()
    return list(_collabs_cache["records"])


def suggest_reviewers(pr_number: int, suggested_limit: int = 12) -> dict:
    """Return candidate reviewers grouped into two buckets:
    - `suggested`: people who've touched this PR's files recently,
      ranked by commit count / recency. Carries `commits`, `files`,
      `last_touched` metadata for UI context.
    - `others`: the rest of the repo's write-access collaborators,
      alpha-sorted. Avatar-only (no PR-specific stats).

    Both groups exclude the PR author, bots, anyone already requested
    as a reviewer, and anyone who has already reviewed. Every
    candidate has `can_request: True` (they're collaborators); the
    field is kept for UI compatibility.
    """
    pr = _gh_json([
        "gh", "pr", "view", str(pr_number),
        "--repo", _repo_slug(),
        "--json", "author,files,reviewRequests,reviews",
    ])
    author_login = ((pr.get("author") or {}).get("login") or "").lower()
    exclude = {author_login} if author_login else set()
    for r in pr.get("reviewRequests") or []:
        login = (r.get("login") or "").lower()
        if login:
            exclude.add(login)
    for rv in pr.get("reviews") or []:
        login = ((rv.get("author") or {}).get("login") or "").lower()
        if login:
            exclude.add(login)

    files = [f.get("path") for f in (pr.get("files") or []) if f.get("path")]
    # Keep the query budget bounded on huge PRs; the most-touched files
    # usually come first in GitHub's listing.
    files = files[:12]

    candidates: dict[str, dict] = {}
    for path in files:
        try:
            commits = _gh_json([
                "gh", "api",
                f"/repos/{_repo_slug()}/commits",
                "-X", "GET",
                "-f", f"path={path}",
                "-f", "per_page=10",
            ])
        except Exception:
            continue
        if not isinstance(commits, list):
            continue
        for c in commits:
            author = c.get("author") or {}
            login = (author.get("login") or "").strip()
            if not login or login.lower() in exclude:
                continue
            if login.endswith(_BOT_SUFFIX) or (author.get("type") or "") == "Bot":
                continue
            commit_date = ((c.get("commit") or {}).get("author") or {}).get("date") or ""
            entry = candidates.setdefault(login, {
                "login": login,
                "avatar_url": author.get("avatar_url"),
                "commits": 0,
                "files": set(),
                "last_touched": "",
            })
            entry["commits"] += 1
            entry["files"].add(path)
            if commit_date > (entry["last_touched"] or ""):
                entry["last_touched"] = commit_date

    ranked = sorted(
        candidates.values(),
        key=lambda e: (e["commits"], e["last_touched"]),
        reverse=True,
    )[:suggested_limit]
    try:
        collab_logins = collaborator_logins()
        collab_records = collaborator_records()
    except Exception:
        collab_logins = set()
        collab_records = []

    # `suggested` keeps the file-toucher ranking plus commit/file stats.
    # Every entry has `can_request` set — only collaborators end up in
    # the UI's "request" column; non-collaborators still appear (for
    # "nudge") but with the request checkbox disabled.
    suggested: list[dict] = []
    suggested_logins: set[str] = set()
    for e in ranked:
        e["files"] = sorted(e["files"])
        e["can_request"] = (not collab_logins) or (e["login"].lower() in collab_logins)
        suggested.append(e)
        suggested_logins.add(e["login"].lower())

    # `others` is everyone else with write access, minus the excluded
    # set (author/already-requested/already-reviewed) and anyone
    # already surfaced in `suggested`.
    others: list[dict] = []
    for rec in collab_records:
        login = (rec.get("login") or "").strip()
        if not login:
            continue
        low = login.lower()
        if low in exclude or low in suggested_logins:
            continue
        others.append({
            "login": login,
            "avatar_url": rec.get("avatar_url"),
            "commits": 0,
            "files": [],
            "last_touched": "",
            "can_request": True,
        })
    others.sort(key=lambda e: e["login"].lower())

    return {"suggested": suggested, "others": others}


_DRAWER_FIELDS = ",".join([
    "number", "title", "body", "url", "author",
    "state", "isDraft", "createdAt", "updatedAt",
    "headRefName", "baseRefName", "mergeable",
    "additions", "deletions", "changedFiles",
    "labels", "milestone",
    "reviewRequests", "reviews", "reviewDecision",
    "comments", "statusCheckRollup",
    "closingIssuesReferences", "assignees",
])


def fetch_pr_for_drawer(pr_number: int) -> dict:
    """Fetch a rich snapshot of a PR for the drawer view. One `gh pr
    view --json` call — no per-field round-trips."""
    return _gh_json([
        "gh", "pr", "view", str(pr_number),
        "--repo", _repo_slug(),
        "--json", _DRAWER_FIELDS,
    ])


_ISSUE_DRAWER_FIELDS = (
    "number,title,url,state,stateReason,author,createdAt,updatedAt,"
    "body,labels,assignees,milestone,comments"
)


def fetch_issue_for_drawer(issue_number: int) -> dict:
    """Issue counterpart to `fetch_pr_for_drawer`. Returns body,
    labels, comments, etc. — everything the issue modal needs in one
    `gh issue view --json` call."""
    return _gh_json([
        "gh", "issue", "view", str(issue_number),
        "--repo", _repo_slug(),
        "--json", _ISSUE_DRAWER_FIELDS,
    ])


def request_reviewers(pr_number: int, logins: list[str]) -> dict:
    """POST /repos/{owner}/{name}/pulls/{N}/requested_reviewers with
    the selected logins. Returns the raw gh API response."""
    if not logins:
        raise ValueError("no reviewers selected")
    cmd = ["gh", "api",
           "--method", "POST",
           "-H", "Accept: application/vnd.github+json",
           f"/repos/{_repo_slug()}/pulls/{pr_number}/requested_reviewers"]
    for login in logins:
        cmd.extend(["-f", f"reviewers[]={login}"])
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"gh api request_reviewers failed: {result.stderr.strip()}"
        )
    try:
        return json.loads(result.stdout) if result.stdout else {}
    except json.JSONDecodeError:
        return {"raw": result.stdout}
