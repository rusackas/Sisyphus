"""FastAPI app serving the kanban UI and action dispatch endpoints."""
import asyncio
import threading
import time

import json

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import config_checks as _config_checks
from . import github, icons as _icons, inbox as _inbox, markdown as md, sessions, worktree
from .actions import (
    CONTINUE_NUDGE,
    approve_drafts,
    approve_plan,
    continue_action,
    dispatch,
)
from .config import (
    PROJECT_ROOT,
    add_queue_block,
    add_repo_block,
    delete_repo_block,
    get_queue_block_yaml,
    load_config,
    new_queue_template,
    replace_queue_block,
    set_default_repo,
    update_queue_definition,
)
from .queues import (
    _mutate,
    _now,
    current_dry_run,
    delete_item,
    find_item,
    get_global_setting,
    get_queue_setting,
    get_queues_config,
    load_state,
    queue_items,
    set_item_drafts,
    set_item_drafts_status,
    set_item_parked_at,
    set_item_plan,
    set_item_plan_status,
    set_item_result,
    set_item_state,
    update_global_setting,
    update_queue_setting,
)
from .runner import refresh_one_item, retriage_item, run_queue

# A 30s fetch tick hammered the GitHub REST API — each queue's fetch
# is ~1 list call + 1 checks call per PR (50 PRs × ~2 calls × N queues
# every 30s = ~N × 12k calls/hr, eating the 5k/hr budget with just a
# couple of queues). 180s still feels responsive for a maintenance
# tool while leaving plenty of headroom for manual Updates and auto-
# unparking of awaiting-update cards.
DEFAULT_AUTO_REFRESH_SECONDS = 180

TEMPLATES_DIR = PROJECT_ROOT / "repobot" / "templates"
STATIC_DIR = PROJECT_ROOT / "repobot" / "static"
SKILLS_DIR = PROJECT_ROOT / ".claude" / "skills"


def _list_triage_skills() -> list[str]:
    """Return the names of every `triage-*` skill bundled in
    `.claude/skills/` (sorted). Used to populate the queue-settings
    triage-skill dropdown so the user can pick a non-default triager
    without typing a skill name by hand."""
    if not SKILLS_DIR.is_dir():
        return []
    out: list[str] = []
    for child in SKILLS_DIR.iterdir():
        if not child.is_dir() or not child.name.startswith("triage-"):
            continue
        if not (child / "SKILL.md").is_file():
            continue
        out.append(child.name)
    return sorted(out)

app = FastAPI(title="repobot")


def _reload_or_redirect(request: Request) -> Response:
    """Success response for settings / config-write endpoints. HTMX
    clients get a 204 + HX-Refresh so the whole page re-renders
    (tabs, header, queue-meta can all be affected by the write);
    non-HTMX clients get the usual 303 back to `/`."""
    if request.headers.get("HX-Request"):
        return Response(status_code=204, headers={"HX-Refresh": "true"})
    return RedirectResponse(url="/", status_code=303)


@app.middleware("http")
async def htmx_swallow_redirects(request: Request, call_next):
    """With hx-boost on the body, every form submit is XHR. A 303
    redirect back to `/` would trigger HTMX to fetch `/` and swap the
    whole page — defeating the point of the migration. Convert those
    same-origin 303s into a 204 No Content when the request is
    HTMX-driven; the client's afterRequest hook nudges the polling
    loop to re-fetch the changed region instead."""
    response = await call_next(request)
    if (request.headers.get("HX-Request")
            and response.status_code == 303):
        return Response(status_code=204)
    return response

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
from datetime import datetime, timezone
from markupsafe import Markup as _Markup

templates.env.globals["icon"] = lambda name, **kw: _Markup(_icons.render(name, **kw))
templates.env.globals["rank_bucket"] = _inbox.rank_bucket
templates.env.globals["BUCKETS"] = _inbox.BUCKETS


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def _time_ago(iso: str | None) -> str:
    """Humanize an ISO timestamp into relative English. Empty on None.
    Granularity deliberately low — "3 weeks ago" is more scannable
    than "19 days ago" when you're looking at a card."""
    dt = _parse_iso(iso)
    if dt is None:
        return ""
    now = datetime.now(timezone.utc)
    delta = now - dt
    secs = delta.total_seconds()
    if secs < 0:
        return "just now"
    if secs < 60:
        return "just now"
    if secs < 3600:
        mins = int(secs // 60)
        return f"{mins} minute{'s' if mins != 1 else ''} ago"
    if secs < 86400:
        hours = int(secs // 3600)
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = int(secs // 86400)
    if days == 1:
        return "yesterday"
    if days < 14:
        return f"{days} days ago"
    if days < 60:
        weeks = days // 7
        return f"{weeks} week{'s' if weeks != 1 else ''} ago"
    if days < 365:
        months = days // 30
        return f"{months} month{'s' if months != 1 else ''} ago"
    years = days // 365
    return f"{years} year{'s' if years != 1 else ''} ago"


def _exact_time(iso: str | None) -> str:
    """Format an ISO timestamp as a tooltip-friendly local-ish string,
    e.g. 'Apr 21, 2026 · 14:30 UTC'. Always UTC to avoid timezone
    mismatches between the user and the server."""
    dt = _parse_iso(iso)
    if dt is None:
        return ""
    return dt.astimezone(timezone.utc).strftime("%b %d, %Y · %H:%M UTC")


templates.env.globals["time_ago"] = _time_ago
templates.env.globals["exact_time"] = _exact_time

# Composite "Unblock" picker — given a card's raw + actions list,
# returns the single best one-click unblock action id (or empty).
# The card template renders an "Unblock → {action}" button at the
# top of the actions row when this returns non-empty.
from .triage import pick_unblock_action as _pick_unblock_action  # noqa: E402
templates.env.globals["pick_unblock_action"] = _pick_unblock_action
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _sweep_stale_session_state() -> None:
    """Sessions live in memory; on startup that's empty. So:
    - Strip session_id pointers (nothing to chat with anymore).
    - Any item with last_result.status == 'running' was interrupted —
      demote back to the queue's initial_state and mark the result
      'interrupted' so the user knows to retry.
    - If `auto_resume_on_boot` is enabled, call continue_action for
      each interrupted item that has an SDK session id we can resume
      from.
    - Prune any worktree on disk whose PR number isn't referenced by
      a live item (e.g. from a previous session the user deleted while
      the server was running, or leftovers from an older run)."""
    auto_resume = bool(get_global_setting("auto_resume_on_boot", False))
    queues_by_id = {q["id"]: q for q in get_queues_config()}
    now = _now()
    live_numbers: set[int] = set()
    resume_candidates: list[tuple[str, object]] = []

    def _m(state):
        for qid, bucket in state.get("queues", {}).items():
            q = queues_by_id.get(qid)
            for item in bucket.get("items", []):
                item.pop("session_id", None)
                item.pop("triage_session_id", None)
                lr = item.get("last_result") or {}
                if lr.get("status") in ("running", "queued", "starting") and q:
                    item["state"] = q["initial_state"]
                    item["state_changed_at"] = now
                    item["last_result"] = {
                        **lr,
                        "status": "interrupted",
                        "message": "Action interrupted by server restart. Retry from the buttons.",
                    }
                    item["last_result_at"] = now
                    sdk_sid = (lr.get("meta") or {}).get("session_id")
                    if auto_resume and sdk_sid and item.get("id") is not None:
                        resume_candidates.append((qid, item["id"]))
                num = item.get("number")
                if isinstance(num, int):
                    live_numbers.add(num)
    _mutate(_m)

    try:
        removed = worktree.prune_orphan_worktrees(live_numbers)
        if removed:
            print(f"[startup] pruned {len(removed)} orphan worktree(s): {removed}")
    except Exception as exc:
        print(f"[startup] worktree prune failed: {exc}")

    if resume_candidates:
        from . import actions as _actions
        resumed = 0
        for qid, item_id in resume_candidates:
            try:
                _actions.continue_action(qid, item_id)
                resumed += 1
            except Exception as exc:
                print(f"[startup] auto-resume failed for {qid}/{item_id}: {exc}")
        print(f"[startup] auto-resumed {resumed}/{len(resume_candidates)} "
              "interrupted action(s)")


_sweep_stale_session_state()


def _backfill_stale_item_raw() -> None:
    """One-shot startup backfill: any item whose `raw` is missing
    `author` or `createdAt` gets a per-PR refetch. This catches items
    that were fetched before LIST_FIELDS expanded and have since
    dropped out of the live `gh pr list` results (closed, merged,
    dismissed-as-reviewer) — they'd never get refreshed by the
    auto-refresh tick because they're not in any fresh fetch.

    Runs in a background thread so startup isn't blocked. Each
    refresh is one `gh pr view` call, scoped to the item's own repo.
    Skipped silently if a refresh fails (the item just stays stale,
    same as today)."""
    from .runner import refresh_one_item
    state = load_state()
    targets: list[tuple[str, object]] = []
    for qid, bucket in (state.get("queues") or {}).items():
        for item in (bucket or {}).get("items") or []:
            raw = item.get("raw") or {}
            if not raw.get("author") or not raw.get("createdAt"):
                if item.get("id") is not None:
                    targets.append((qid, item["id"]))
    if not targets:
        return
    print(f"[startup] backfilling raw on {len(targets)} stale "
          f"item(s) — running in background")

    def _run():
        ok = 0
        for qid, iid in targets:
            try:
                refresh_one_item(qid, iid)
                ok += 1
            except Exception as exc:
                print(f"[backfill] {qid}/{iid}: {exc}")
        print(f"[startup] backfill done: {ok}/{len(targets)}")
    threading.Thread(target=_run, daemon=True,
                     name="raw-backfill").start()


_backfill_stale_item_raw()


def _auto_refresh_interval() -> int:
    """Resolve the auto-refresh interval at boot. Global setting
    (user-editable) wins over config.yaml default so the user can
    tune without restarting. Lower bound of 30s so accidental '1'
    doesn't nuke the GitHub API budget."""
    cfg = load_config()
    cfg_default = int(cfg.get("auto_refresh", {}).get(
        "interval_seconds", DEFAULT_AUTO_REFRESH_SECONDS))
    setting = int(get_global_setting("auto_refresh_seconds", cfg_default))
    return max(30, setting) if setting > 0 else setting  # 0 still means off


# Auto-refresh tick is gated by these. Below the headroom we skip the
# tick rather than spend the last few hundred calls on background polling
# — the user's foreground actions (open drawer, request reviewers, run a
# triage skill) need somewhere to land. GraphQL gets a wider buffer
# because a single triage session can spend ~50 GraphQL calls.
_RATE_LIMIT_REST_HEADROOM = 200
_RATE_LIMIT_GQL_HEADROOM = 500


def _rate_limit_pause_reason() -> str | None:
    """Return a human reason if the auto-refresh tick should skip this
    cycle to preserve API budget. Returns None when there's headroom."""
    snap = github.rate_limit_snapshot() or {}
    core = snap.get("core") or {}
    gql = snap.get("graphql") or {}
    # Default to "lots of room" when the snapshot fails — we don't want
    # an offline rate_limit endpoint to wedge the whole tick.
    rest_remaining = core.get("remaining")
    gql_remaining = gql.get("remaining")
    if (isinstance(rest_remaining, int)
            and rest_remaining < _RATE_LIMIT_REST_HEADROOM):
        return (f"REST budget low: {rest_remaining} remaining "
                f"(< {_RATE_LIMIT_REST_HEADROOM} headroom)")
    if (isinstance(gql_remaining, int)
            and gql_remaining < _RATE_LIMIT_GQL_HEADROOM):
        return (f"GraphQL budget low: {gql_remaining} remaining "
                f"(< {_RATE_LIMIT_GQL_HEADROOM} headroom)")
    return None


def _start_auto_refresh() -> None:
    """One daemon thread per queue. Each wakes every N seconds and calls
    `run_queue` (non-blocking triage fan-out) to keep the hopper full
    and refresh `raw` on every existing item from the same fetch (free
    — the GH API call already happened, this just merges its results).
    Without that merge, items frozen on the board with stale `raw`
    (e.g. from before LIST_FIELDS expanded) never pick up newer fields
    like `author` / `createdAt` until the user manually clicks Update.

    Each tick checks `rate_limit_snapshot()` first — if the REST or
    GraphQL budget is below headroom, the tick is skipped and the
    UI is notified via SSE so the rate-limit pill can show a paused
    state. The next tick retries (rate_limit_snapshot itself is exempt
    from the rate limit, so the gate stays cheap)."""
    interval = _auto_refresh_interval()
    if interval <= 0:
        return
    last_paused: dict[str, bool] = {}
    for q in get_queues_config():
        def loop(qid=q["id"]):
            while True:
                pause_reason = _rate_limit_pause_reason()
                if pause_reason:
                    if not last_paused.get(qid):
                        print(f"[auto-refresh {qid}] paused: {pause_reason}")
                        try:
                            from . import events as _events
                            _events.broadcast("rate-limit-paused",
                                              {"queue_id": qid,
                                               "reason": pause_reason})
                        except Exception:
                            pass
                    last_paused[qid] = True
                else:
                    if last_paused.get(qid):
                        print(f"[auto-refresh {qid}] resumed")
                        try:
                            from . import events as _events
                            _events.broadcast("rate-limit-resumed",
                                              {"queue_id": qid})
                        except Exception:
                            pass
                    last_paused[qid] = False
                    try:
                        run_queue(qid, wait_for_triage=False,
                                  refresh_existing=True)
                    except Exception as exc:
                        print(f"[auto-refresh {qid}] {exc}")
                time.sleep(interval)
        threading.Thread(target=loop, daemon=True, name=f"auto-refresh-{q['id']}").start()


_start_auto_refresh()


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    cfg = load_config()
    state = load_state()
    queues_cfg = get_queues_config()
    pending = False
    for q in queues_cfg:
        items = state.get("queues", {}).get(q["id"], {}).get("items", [])
        for item in items:
            if item.get("state") == q["initial_state"] and not item.get("proposal"):
                pending = True
                break
            if item.get("last_result", {}).get("status") == "running":
                pending = True
                break
        if pending:
            break

    # Fold UI overrides back into the queue dicts so the template can
    # render the currently-effective values without rummaging through
    # two sources of truth.
    for q in queues_cfg:
        q["effective_max_in_flight"] = int(get_queue_setting(
            q["id"], "max_in_flight", q["max_in_flight"]))
        q["effective_worker_slots"] = int(get_queue_setting(
            q["id"], "worker_slots",
            q.get("worker_slots", q["effective_max_in_flight"])))
        q["intake_paused"] = bool(get_queue_setting(
            q["id"], "intake_paused", False))

    stats_data = sessions.stats()
    stats_data["auto_resume_on_boot"] = bool(
        get_global_setting("auto_resume_on_boot", False))
    stats_data["auto_refresh_seconds"] = _auto_refresh_interval()
    # Enrich live-session rows with PR number/title so the popover can
    # render a useful label without another round-trip.
    live_by_item: dict[str, dict] = {}
    for ls in stats_data.get("live", []):
        qid = ls.get("queue_id")
        iid = ls.get("item_id")
        if qid is None or iid is None:
            continue
        for item in state.get("queues", {}).get(qid, {}).get("items", []):
            if item.get("id") == iid:
                ls["number"] = item.get("number")
                ls["title"] = item.get("title")
                break
        live_by_item[f"{qid}:{iid}"] = ls

    # Per-queue count of "needs your attention" items. Ranks 0..3 are
    # the ones that actually want a click (needs_human / interrupted /
    # error / verdict-ready). Used for the tab badges.
    queue_attention_counts: dict[str, int] = {}
    for q in queues_cfg:
        qid = q["id"]
        n = 0
        for item in state.get("queues", {}).get(qid, {}).get("items", []):
            if _inbox.attention_rank(item, q) <= 3:
                n += 1
        queue_attention_counts[qid] = n

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "queues": queues_cfg,
            "state": state,
            "dry_run": current_dry_run(),
            "pending": pending,
            "stats": stats_data,
            "live_by_item": live_by_item,
            "queue_attention_counts": queue_attention_counts,
            "rate_limit": github.rate_limit_snapshot(),
            "repos": github.list_repos(),
            "default_repo_id": (cfg.get("default_repo_id")
                                or github.list_repos()[0]["id"]),
            "queue_repo_ids": {q["id"]: github.queue_repo_id(q)
                               for q in queues_cfg},
            "triage_skills": _list_triage_skills(),
            "config_status": _config_checks.summary(),
        },
    )


@app.post("/queues/{queue_id}/fetch")
def fetch_queue(queue_id: str):
    threading.Thread(
        target=run_queue, args=(queue_id,),
        kwargs={"wait_for_triage": True, "refresh_existing": True}, daemon=True,
    ).start()
    return RedirectResponse(url="/", status_code=303)


@app.post("/queues/{queue_id}/items/{item_id}/actions/{action_id}")
def act(queue_id: str, item_id: int, action_id: str,
        comment_body: str = Form("")):
    extra = {"comment_body": comment_body} if comment_body.strip() else None
    dispatch(queue_id, item_id, action_id, extra_context=extra)
    return RedirectResponse(url="/", status_code=303)


@app.post("/queues/{queue_id}/items/{item_id}/prompt")
def prompt(queue_id: str, item_id: int, instruction: str = Form(...)):
    instruction = instruction.strip()
    if not instruction:
        return RedirectResponse(url="/", status_code=303)
    dispatch(queue_id, item_id, "prompt", extra_context={"instruction": instruction})
    return RedirectResponse(url="/", status_code=303)


@app.post("/queues/{queue_id}/items/{item_id}/continue")
def cont(queue_id: str, item_id: int):
    try:
        continue_action(queue_id, item_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return RedirectResponse(url="/", status_code=303)


@app.post("/queues/{queue_id}/items/{item_id}/resume-live")
async def resume_live(queue_id: str, item_id: int):
    """Nudge a LIVE idle session to finish / emit its final JSON. Unlike
    the `continue` endpoint (which spins up a fresh SDK-resumed process
    for a closed session), this sends the nudge straight into the
    existing idle session's user queue."""
    item = find_item(load_state(), queue_id, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="item not found")
    sid = item.get("session_id") or item.get("triage_session_id")
    if not sid:
        raise HTTPException(status_code=400, detail="no live session on item")
    delivered = await sessions.send_user_message(sid, CONTINUE_NUDGE)
    if not delivered:
        raise HTTPException(status_code=409,
                            detail="session is closed — use the continue button instead")
    return RedirectResponse(url="/", status_code=303)


@app.post("/queues/{queue_id}/items/{item_id}/plan/approve")
async def approve_plan_endpoint(queue_id: str, item_id: int,
                                 plan_json: str = Form(...)):
    """Submit a (possibly edited) plan back to the plan-fix session.

    Two paths:
      - Live session still around (idle, waiting for follow-up):
        send the APPROVED PLAN message directly — phase 2 runs in
        the same session.
      - Live session has closed (idle timeout / aborted): spawn a
        fresh plan-fix session with the approved plan in runtime
        context (`approved_plan` field). The skill's prompt is
        documented to jump straight to phase 2 when that field is
        present, so the user doesn't lose their edits to a closed-
        session error.
    """
    try:
        edited = json.loads(plan_json)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"invalid plan JSON: {exc}")
    if not isinstance(edited, dict):
        raise HTTPException(status_code=400, detail="plan must be a JSON object")
    delivered = await approve_plan(queue_id, item_id, edited)
    if delivered:
        return RedirectResponse(url="/", status_code=303)

    # Live session is gone — spawn a fresh plan-fix session in
    # phase-2-only mode. The dispatcher manages the worktree
    # (re-uses if present), and the skill detects `approved_plan`
    # in runtime context and skips phase 1.
    from .actions import dispatch as _dispatch_action
    try:
        await asyncio.to_thread(
            _dispatch_action,
            queue_id, item_id, "plan-fix",
            {"approved_plan": edited},
        )
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=("plan session was closed and the resume spawn failed: "
                    f"{exc}. Discard and re-run `plan-fix`."),
        )
    set_item_plan(queue_id, item_id, edited)
    set_item_plan_status(queue_id, item_id, "executing")
    set_item_result(queue_id, item_id, {
        "action": "execute-plan",
        "status": "running",
        "message": "Resumed in a fresh session; executing approved plan…",
    })
    return RedirectResponse(url="/", status_code=303)


@app.post("/queues/{queue_id}/items/{item_id}/plan/discard")
def discard_plan(queue_id: str, item_id: int):
    set_item_plan(queue_id, item_id, None)
    set_item_plan_status(queue_id, item_id, "discarded")
    return RedirectResponse(url="/", status_code=303)


@app.post("/queues/{queue_id}/items/{item_id}/drafts/approve")
async def approve_drafts_endpoint(queue_id: str, item_id: int,
                                   drafts_json: str = Form(...)):
    """Submit (possibly edited) per-thread reply drafts. Prefers the
    live address-review-comments session so it can post each reply;
    falls back to direct `gh api` posts when the session has closed
    (e.g. after a reboot) so the click always "just proceeds"."""
    try:
        edited = json.loads(drafts_json)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"invalid drafts JSON: {exc}")
    if not isinstance(edited, dict):
        raise HTTPException(status_code=400, detail="drafts must be a JSON object")
    try:
        await approve_drafts(queue_id, item_id, edited)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return RedirectResponse(url="/", status_code=303)


@app.post("/queues/{queue_id}/items/{item_id}/drafts/discard")
def discard_drafts(queue_id: str, item_id: int):
    set_item_drafts(queue_id, item_id, None)
    set_item_drafts_status(queue_id, item_id, "discarded")
    return RedirectResponse(url="/", status_code=303)


@app.post("/queues/{queue_id}/clear-done")
def clear_done(queue_id: str):
    """Delete every item currently in the queue's done_state column.
    Reuses the per-item delete path so worktree pruning + session abort
    stay correct. Anything still in the upstream feed will come back on
    the next Update."""
    queues_by_id = {q["id"]: q for q in get_queues_config()}
    q = queues_by_id.get(queue_id)
    if q is None:
        raise HTTPException(status_code=404, detail="unknown queue")
    done_state = q.get("done_state", "done")
    state = load_state()
    done_ids = [i["id"] for i in queue_items(state, queue_id)
                if i.get("state") == done_state]
    for iid in done_ids:
        try:
            sessions.abort_sessions_for_item(queue_id, iid)
        except Exception:
            pass
        delete_item(queue_id, iid)
        try:
            worktree.remove_worktree(iid)
        except Exception:
            pass
    return RedirectResponse(url="/", status_code=303)


@app.post("/queues/{queue_id}/items/{item_id}/refresh")
def refresh_item(queue_id: str, item_id: int):
    """Per-card refresh: refetch this PR from GitHub, re-triage if the
    PR has been touched since last triage, otherwise leave alone."""
    try:
        refresh_one_item(queue_id, item_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return RedirectResponse(url="/", status_code=303)


@app.post("/queues/{queue_id}/items/{item_id}/retriage")
def retriage(queue_id: str, item_id: int):
    """Force a fresh triage on this item: clears the existing verdict
    and spawns a new triage session. Used when the human disagrees with
    the triage or the old session got stuck."""
    try:
        retriage_item(queue_id, item_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return RedirectResponse(url="/", status_code=303)


def _ctx_for_queue(request: Request, queue_id: str) -> dict:
    """Build the Jinja context needed to render _queue_body.html for one
    queue. Mirrors the shape the index page prepares so the partial
    renders identically whether it's embedded in the full page or
    returned as an HTMX fragment."""
    cfg = load_config()
    queues_cfg = get_queues_config()
    # Per-queue overrides (matches index endpoint).
    for q in queues_cfg:
        q["effective_max_in_flight"] = int(
            get_queue_setting(q["id"], "max_in_flight", q.get("max_in_flight", 0)))
        q["effective_worker_slots"] = int(
            get_queue_setting(q["id"], "worker_slots", q.get("max_in_flight", 0)))
        q["intake_paused"] = bool(get_queue_setting(
            q["id"], "intake_paused", False))
    queue = next((q for q in queues_cfg if q["id"] == queue_id), None)
    if queue is None:
        raise HTTPException(status_code=404, detail="unknown queue")
    state = load_state()
    stats_data = sessions.stats()
    stats_data["auto_resume_on_boot"] = bool(
        get_global_setting("auto_resume_on_boot", False))
    stats_data["auto_refresh_seconds"] = _auto_refresh_interval()
    live_by_item: dict[str, dict] = {}
    for ls in stats_data.get("live", []):
        qid = ls.get("queue_id"); iid = ls.get("item_id")
        if qid is None or iid is None:
            continue
        for item in state.get("queues", {}).get(qid, {}).get("items", []):
            if item.get("id") == iid:
                ls["number"] = item.get("number")
                ls["title"] = item.get("title")
                break
        live_by_item[f"{qid}:{iid}"] = ls
    q_items = state.get("queues", {}).get(queue_id, {}).get("items", [])
    done_state = queue.get("done_state") or "done"
    awaiting_state = queue.get("awaiting_state")
    return {
        "request": request,
        "queue": queue,
        "state": state,
        "stats": stats_data,
        "live_by_item": live_by_item,
        "dry_run": current_dry_run(),
        "q_items": q_items,
        "done_state": done_state,
        "awaiting_state": awaiting_state,
    }


@app.get("/queues/{queue_id}/body", response_class=HTMLResponse)
def queue_body(request: Request, queue_id: str):
    """Return the state-columns fragment for one queue. Polled by
    HTMX every few seconds; morph-swap preserves DOM identity so open
    <details>, focused inputs, and scroll position survive."""
    ctx = _ctx_for_queue(request, queue_id)
    return templates.TemplateResponse(request, "_queue_body.html", ctx)


@app.get("/queues/{queue_id}/meta", response_class=HTMLResponse)
def queue_meta(request: Request, queue_id: str):
    """Header counts for one queue (occupancy + triaging spinner).
    Lives in its own fragment so the SSE handler can refresh it on
    `queue-changed` without touching the body — the queue-meta sits
    in the header chrome above the SSE-targeted body div, so a body-
    only swap leaves these counts stale."""
    ctx = _ctx_for_queue(request, queue_id)
    queue = ctx["queue"]
    q_items = ctx["q_items"]
    done_state = ctx["done_state"]
    awaiting_state = ctx["awaiting_state"]
    initial_state = queue.get("initial_state")
    eff_max = queue.get("effective_max_in_flight") or queue.get("max_in_flight") or 0
    non_done = sum(1 for i in q_items
                   if i.get("state") != done_state
                   and i.get("state") != awaiting_state)
    triaging = sum(1 for i in q_items
                   if i.get("state") == initial_state
                   and not i.get("proposal"))
    return templates.TemplateResponse(
        request, "_queue_meta_counts.html",
        {"request": request, "non_done": non_done,
         "eff_max": eff_max, "triaging": triaging},
    )


@app.get("/fragments/header-readout", response_class=HTMLResponse)
def header_readout(request: Request):
    """Return just the header stats readout. Polled by HTMX."""
    stats_data = sessions.stats()
    stats_data["auto_resume_on_boot"] = bool(
        get_global_setting("auto_resume_on_boot", False))
    stats_data["auto_refresh_seconds"] = _auto_refresh_interval()
    tt = stats_data.get("tokens_24h") or {}
    ttl = (tt.get("input_tokens", 0) + tt.get("output_tokens", 0)
           + tt.get("cache_creation_input_tokens", 0)
           + tt.get("cache_read_input_tokens", 0))
    return templates.TemplateResponse(
        request, "_header_readout.html",
        {"request": request, "s": stats_data, "ttl": ttl,
         "rate_limit": github.rate_limit_snapshot()},
    )


@app.get("/events")
async def events_stream(request: Request):
    """Server-Sent Events stream. The client opens this once on
    page load (`new EventSource('/events')`) and keeps it open;
    every mutation that should refresh some part of the UI calls
    `events.broadcast(...)`, which fans the event out to every
    connected subscriber over this channel.

    Replaces the every-3s queue/tasks/header polling — the page
    only refetches a body when the server says something actually
    changed. A 30s fallback poll on each container backstops the
    rare case where the SSE link drops without `EventSource`'s
    auto-reconnect kicking in.

    Keepalive comments fire every 15s so reverse proxies / browser
    timeouts don't kill an idle connection.
    """
    import asyncio as _asyncio
    from . import events as _events

    async def gen():
        sub_q = _events.subscribe()
        try:
            # Initial hello so the client knows the channel is up
            # (handy for diagnostics, also primes any handler that
            # wants to flush on first event).
            yield "event: hello\ndata: {}\n\n"
            while True:
                msg = await _asyncio.to_thread(_events._blocking_get,
                                               sub_q, 15.0)
                if msg is None:
                    # No event in the last 15s — emit a comment to
                    # keep the connection alive across proxies.
                    yield ": keepalive\n\n"
                    continue
                yield (f"event: {msg['event']}\n"
                       f"data: {json.dumps(msg['data'])}\n\n")
        finally:
            _events.unsubscribe(sub_q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            # Disable nginx-style proxy buffering when present;
            # streaming responses need to flush immediately.
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache",
        },
    )


@app.get("/queues/{queue_id}/items/{item_id}/drawer", response_class=HTMLResponse)
def drawer(request: Request, queue_id: str, item_id: int):
    """Render a snapshot (title, body, comments, history) as an HTML
    fragment for the in-app modal. Branches on item.kind so issue
    items get the simpler issue modal (no CI, no merge state, no
    review threads); PR items keep the full PR modal."""
    item = find_item(load_state(), queue_id, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="item not found")
    number = item.get("number")
    if not number:
        raise HTTPException(status_code=400, detail="item has no number")
    # Resolve the repo for this item: stamped on `raw.repo` at fetch
    # time; fall back to the queue's configured repo.
    qcfg = {q["id"]: q for q in get_queues_config()}.get(queue_id, {})
    slug = github.item_repo_slug(item) or github.queue_repo_slug(qcfg)
    kind = ((item.get("raw") or {}).get("kind") or "pr").lower()
    try:
        with github.repo_scope(slug):
            if kind == "issue":
                obj = github.fetch_issue_for_drawer(int(number))
            else:
                obj = github.fetch_pr_for_drawer(int(number))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    owner, name = slug.split("/", 1)
    body_html = md.render(obj.get("body"), owner=owner, name=name)
    comments = []
    for c in obj.get("comments") or []:
        author_login = (c.get("author") or {}).get("login") or "unknown"
        comments.append({
            "author": author_login,
            "createdAt": c.get("createdAt"),
            "html": md.render(c.get("body"), owner=owner, name=name),
        })
    # Audit history — same pattern for both kinds.
    history: list[dict] = []
    try:
        from . import db as _db
        for r in _db.actions_for_item(queue_id, item_id, limit=40):
            history.append({"kind": "action", "ts": r["ts"], **r})
        for r in _db.transitions_for_item(queue_id, item_id, limit=40):
            history.append({"kind": "transition", "ts": r["ts"], **r})
    except Exception as exc:
        print(f"[drawer] history fetch failed: {exc}")
    history.sort(key=lambda r: r.get("ts") or "", reverse=True)
    template = "issue_modal.html" if kind == "issue" else "pr_modal.html"
    ctx_key = "issue" if kind == "issue" else "pr"
    return templates.TemplateResponse(
        request, template,
        {ctx_key: obj, "body_html": body_html, "comments": comments,
         "queue_id": queue_id, "item_id": item_id,
         "history": history},
    )


@app.get("/queues/{queue_id}/bulk-approve-candidates", response_class=JSONResponse)
def bulk_approve_candidates(queue_id: str):
    """Return the list of items in this queue that are eligible for
    bulk approve-merge: their `actions` list contains 'approve-merge',
    they have no live action session, and the state isn't already
    `done`. Each candidate carries a default approval_comment pulled
    from triage_notes (skill-drafted) or a templated fallback.

    Powers the bulk-approve modal — the user sees every clean PR at
    once with each comment editable, and ships the lot in one
    confirmation."""
    state = load_state()
    bucket = state.get("queues", {}).get(queue_id) or {}
    items = bucket.get("items") or []
    qcfg = {q["id"]: q for q in get_queues_config()}.get(queue_id, {})
    done_state = qcfg.get("done_state", "done")
    live_action_ids: set = set()
    with sessions._SESSIONS_LOCK:
        for s in sessions.SESSIONS.values():
            if (s.kind == "action" and s.queue_id == queue_id
                    and s.item_id is not None
                    and s.status in ("queued", "starting", "running", "idle")):
                live_action_ids.add(s.item_id)

    out = []
    for item in items:
        if "approve-merge" not in (item.get("actions") or []):
            continue
        if item.get("state") == done_state:
            continue
        if item.get("id") in live_action_ids:
            continue
        notes = item.get("triage_notes") or {}
        default_comment = (
            notes.get("approval_comment")
            or (item.get("assessment") or {}).get("approval_comment")
            or "Dependabot version bump — CI green, mergeStateStatus CLEAN, no open threads."
        )
        author_login = ((item.get("raw") or {}).get("author") or {}).get("login")
        out.append({
            "id": item.get("id"),
            "number": item.get("number"),
            "title": item.get("title") or "",
            "url": item.get("url") or "",
            "author_login": author_login,
            "default_comment": default_comment,
        })
    return JSONResponse({"candidates": out})


@app.post("/queues/{queue_id}/bulk-approve-merge")
async def bulk_approve_merge(queue_id: str,
                              item_id: list[int] = Form(default=[]),
                              comment_body: list[str] = Form(default=[])):
    """Sequentially dispatch approve-merge for each (item_id, comment)
    pair from the bulk-approve modal. Each fires through the standard
    dispatch pipeline — same comment-edit flow, same skill, same
    re-verify-mergeStateStatus guard. Returns a summary of how many
    fired vs. errored.

    The comment-edit UI lives in the bulk modal; user has already
    reviewed each comment before submitting. We pass each as
    `extra_context.comment_body` so the underlying skill posts it
    verbatim — same path as a single-card approve.
    """
    if not item_id:
        raise HTTPException(status_code=400, detail="no items selected")
    if len(item_id) != len(comment_body):
        raise HTTPException(
            status_code=400,
            detail="item_id and comment_body lists must have the same length",
        )
    fired: list[int] = []
    errors: list[str] = []
    from .actions import dispatch as _dispatch_action
    for iid, body in zip(item_id, comment_body):
        try:
            extra = {"comment_body": (body or "").strip()} if (body or "").strip() else None
            await asyncio.to_thread(
                _dispatch_action, queue_id, iid, "approve-merge", extra)
            fired.append(iid)
        except Exception as exc:
            errors.append(f"#{iid}: {exc}")
    return JSONResponse({
        "fired": fired,
        "errors": errors,
        "count": len(fired),
    })


@app.post("/queues/{queue_id}/items/{item_id}/create-pr-from-attempt")
def create_pr_from_attempt(queue_id: str, item_id: int,
                            comment_body: str = Form(...)):
    """Run `gh pr create` for an attempt-fix-issue phase-2 confirmation.

    The skill committed + pushed in phase 1 and emitted a drafted
    title + body. The card surfaces a 'Review & create PR' button
    that opens the comment-edit modal pre-filled with `title\\n\\n\\
    body`. On confirm the modal POSTs the (possibly edited) blob
    here. We split first line as the title; the rest is the body.
    """
    item = find_item(load_state(), queue_id, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="item not found")
    last_result = item.get("last_result") or {}
    if last_result.get("status") != "pr_ready":
        raise HTTPException(
            status_code=400,
            detail="No PR-ready draft on this item. Re-run attempt-fix-issue.",
        )
    head_branch = last_result.get("head_branch")
    if not head_branch:
        raise HTTPException(
            status_code=400,
            detail="Missing head_branch on the draft — can't create PR.",
        )
    blob = (comment_body or "").strip()
    if "\n\n" in blob:
        title, body = blob.split("\n\n", 1)
    else:
        # No blank line between title and body — split on first newline.
        title, _, body = blob.partition("\n")
    title = title.strip()
    body = body.strip()
    if not title:
        raise HTTPException(status_code=400, detail="PR title is empty")

    qcfg = {q["id"]: q for q in get_queues_config()}.get(queue_id, {})
    slug = github.item_repo_slug(item) or github.queue_repo_slug(qcfg)
    if current_dry_run():
        set_item_result(queue_id, item_id, {
            "action": "create-pr",
            "status": "skipped_dry_run",
            "message": f"dry_run — would gh pr create on {slug}, head {head_branch}",
        })
        return RedirectResponse(url="/", status_code=303)

    import subprocess
    cmd = ["gh", "pr", "create",
           "--repo", slug,
           "--head", head_branch,
           "--title", title,
           "--body", body]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        set_item_result(queue_id, item_id, {
            "action": "create-pr",
            "status": "error",
            "message": f"gh pr create failed: {(proc.stderr or '').strip()}",
        })
        raise HTTPException(status_code=502,
                            detail=f"gh pr create failed: {proc.stderr.strip()}")
    pr_url = (proc.stdout or "").strip()
    qcfg2 = {q["id"]: q for q in get_queues_config()}.get(queue_id, {})
    awaiting_state = qcfg2.get("awaiting_state", "awaiting update")
    set_item_state(queue_id, item_id, awaiting_state)
    set_item_parked_at(queue_id, item_id, _now())
    set_item_result(queue_id, item_id, {
        "action": "create-pr",
        "status": "completed",
        "message": f"opened {pr_url}",
        "pr_url": pr_url,
    })
    return RedirectResponse(url="/", status_code=303)


@app.post("/queues/{queue_id}/items/{item_id}/track-pr")
def track_pr(queue_id: str, item_id: int,
             pr_number: int = Form(...),
             pr_url: str = Form(default=""),
             pr_title: str = Form(default="")):
    """Park an issue card with a 'tracked by PR #N' note and move it
    to the queue's done state. Used when a linked PR is doing the
    work — the issue triage card no longer needs attention; the PR
    is the live surface from here on.

    No GitHub side-effects. Purely a local "archive this issue card,
    leave a breadcrumb pointing at the PR" so the issue queue stays
    focused on issues that still need triage decisions.
    """
    item = find_item(load_state(), queue_id, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="item not found")
    qcfg = {q["id"]: q for q in get_queues_config()}.get(queue_id, {})
    done_state = qcfg.get("done_state", "done")
    pr_label = f"PR #{pr_number}"
    if pr_title:
        pr_label = f"{pr_label} ({pr_title})"
    set_item_state(queue_id, item_id, done_state,
                   reason=f"tracked by {pr_label}")
    set_item_parked_at(queue_id, item_id, _now())
    set_item_result(queue_id, item_id, {
        "action": "track-pr",
        "status": "completed",
        "message": f"Tracking {pr_label} — issue card archived from triage.",
        "pr_url": pr_url or None,
        "pr_number": pr_number,
    })
    return RedirectResponse(url="/", status_code=303)


@app.get("/queues/{queue_id}/items/{item_id}/bot-thread-candidates")
def bot_thread_candidates(queue_id: str, item_id: int):
    """Return the unresolved review threads on this PR with each
    thread classified by `triage.classify_bot_thread`. Powers the
    resolve-bot-threads modal — the user sees what would be
    resolved (with bot author + classification) before submitting.

    Pulls thread data from `item.raw.unresolved_threads` (already
    populated by the GraphQL hydrate on fetch); doesn't re-fetch.
    Threads without a classification of `boilerplate` or
    `ambiguous` are surfaced too but pre-unchecked in the UI —
    the user can opt in but the default is "don't auto-resolve."
    """
    from .triage import classify_bot_thread, is_bot_login
    item = find_item(load_state(), queue_id, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="item not found")
    raw = item.get("raw") or {}
    threads = raw.get("unresolved_threads") or []
    out = []
    for t in threads:
        classification = classify_bot_thread(t)
        out.append({
            "id": t.get("id"),
            "first_author": t.get("first_author") or "",
            "first_body": t.get("first_body") or "",
            "path": t.get("path") or "",
            "line": t.get("line"),
            "is_bot": is_bot_login(t.get("first_author") or ""),
            "classification": classification,
            # Default-checked when boilerplate (safe to auto-resolve)
            # or ambiguous-bot (likely safe but user should glance).
            # Substantive bot threads and human threads default
            # unchecked — user has to opt in to override.
            "default_checked": classification in ("boilerplate", "ambiguous"),
        })
    return JSONResponse({"threads": out})


@app.post("/queues/{queue_id}/items/{item_id}/resolve-bot-threads")
def submit_resolve_bot_threads(queue_id: str, item_id: int,
                                thread_ids: list[str] = Form(default=[])):
    """Resolve the selected review threads via the GraphQL
    resolveReviewThread mutation. Records a result on the item with
    counts (resolved / errored). Doesn't transition state — the card
    stays where it is and the next refresh tick re-computes the
    action menu (now without the bot threads blocking approve-merge).
    """
    item = find_item(load_state(), queue_id, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="item not found")
    pr_number = item.get("number")
    if not pr_number:
        raise HTTPException(status_code=400, detail="item has no PR number")
    selected = [t.strip() for t in thread_ids if t and t.strip()]
    if not selected:
        raise HTTPException(status_code=400, detail="no threads selected")
    if current_dry_run():
        set_item_result(queue_id, item_id, {
            "action": "resolve-bot-threads",
            "status": "skipped_dry_run",
            "message": f"dry_run — would resolve {len(selected)} threads",
        })
        return RedirectResponse(url="/", status_code=303)
    qcfg = {q["id"]: q for q in get_queues_config()}.get(queue_id, {})
    slug = github.item_repo_slug(item) or github.queue_repo_slug(qcfg)
    resolved: list[str] = []
    errors: list[str] = []
    with github.repo_scope(slug):
        for tid in selected:
            try:
                github.resolve_review_thread(tid)
                resolved.append(tid)
            except Exception as exc:
                errors.append(f"{tid[:8]}…: {exc}")
    parts = [f"resolved {len(resolved)} thread{'s' if len(resolved) != 1 else ''}"]
    if errors:
        parts.append(f"{len(errors)} failed")
    set_item_result(queue_id, item_id, {
        "action": "resolve-bot-threads",
        "status": "completed" if not errors else "completed_with_errors",
        "message": "; ".join(parts) + (
            f"  ({'; '.join(errors)})" if errors else ""),
    })
    return RedirectResponse(url="/", status_code=303)


@app.get("/queues/{queue_id}/items/{item_id}/reviewer-candidates")
def reviewer_candidates(queue_id: str, item_id: int):
    """Return ranked candidate reviewers for the modal. Mechanical
    computation — no Claude session involved."""
    item = find_item(load_state(), queue_id, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="item not found")
    pr_number = item.get("number")
    if not pr_number:
        raise HTTPException(status_code=400, detail="item has no PR number")
    qcfg = {q["id"]: q for q in get_queues_config()}.get(queue_id, {})
    slug = github.item_repo_slug(item) or github.queue_repo_slug(qcfg)
    try:
        with github.repo_scope(slug):
            grouped = github.suggest_reviewers(int(pr_number))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    # Keep the flat `candidates` key around for any caller that hasn't
    # migrated yet — it's the concatenation of both groups in display
    # order (suggested first, then others).
    flat = list(grouped.get("suggested") or []) + list(grouped.get("others") or [])
    return JSONResponse({
        "candidates": flat,
        "suggested": grouped.get("suggested") or [],
        "others": grouped.get("others") or [],
    })


@app.post("/queues/{queue_id}/items/{item_id}/request-reviewers")
def submit_request_reviewers(queue_id: str, item_id: int,
                             reviewers: list[str] = Form(default=[]),
                             nudge: list[str] = Form(default=[]),
                             comment_body: str = Form(default="")):
    """Called by the reviewer-picker modal. Two independent slots of
    action per candidate:
      - `reviewers` — logins to request as formal reviewers (only
        valid for repo collaborators; handled via GH's
        requested_reviewers API).
      - `nudge` + `comment_body` — logins to @-mention in a freeform
        comment on the PR. Body was edited in the second modal before
        it arrived here.
    At least one must be non-empty. Both may be non-empty; if so, we
    request review first then post the nudge comment.
    """
    item = find_item(load_state(), queue_id, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="item not found")
    pr_number = item.get("number")
    if not pr_number:
        raise HTTPException(status_code=400, detail="item has no PR number")
    to_request = [r.strip() for r in reviewers if r and r.strip()]
    to_nudge = [r.strip() for r in nudge if r and r.strip()]
    comment = (comment_body or "").strip()
    if not to_request and not to_nudge:
        raise HTTPException(status_code=400,
                            detail="no reviewers or nudges selected")
    if to_nudge and not comment:
        raise HTTPException(status_code=400,
                            detail="nudge selected but no comment body")

    cfg = load_config()
    dry_run = current_dry_run()
    qcfg = {q["id"]: q for q in get_queues_config()}.get(queue_id, {})
    awaiting_state = qcfg.get("awaiting_state", "awaiting update")

    actions_taken: list[str] = []
    errors: list[str] = []

    if dry_run:
        if to_request:
            actions_taken.append(
                "would request review from "
                + ", ".join("@" + r for r in to_request))
        if to_nudge:
            actions_taken.append(
                f"would post nudge comment ({len(comment)} chars)")
        set_item_result(queue_id, item_id, {
            "action": "request-reviewers",
            "status": "skipped_dry_run",
            "message": "dry_run — " + "; ".join(actions_taken),
            "reviewers": to_request,
            "nudged": to_nudge,
        })
        return RedirectResponse(url="/", status_code=303)

    qcfg_lookup = {q["id"]: q for q in get_queues_config()}.get(queue_id, {})
    slug = github.item_repo_slug(item) or github.queue_repo_slug(qcfg_lookup)
    if to_request:
        try:
            with github.repo_scope(slug):
                github.request_reviewers(int(pr_number), to_request)
            actions_taken.append(
                "requested review from "
                + ", ".join("@" + r for r in to_request))
        except Exception as exc:
            errors.append(f"request_reviewers: {exc}")

    if to_nudge and comment:
        try:
            with github.repo_scope(slug):
                github.post_pr_comment(int(pr_number), comment)
            actions_taken.append(
                "posted nudge comment (@"
                + ", @".join(to_nudge) + ")")
        except Exception as exc:
            errors.append(f"post_pr_comment: {exc}")

    if errors and not actions_taken:
        set_item_result(queue_id, item_id, {
            "action": "request-reviewers",
            "status": "error",
            "message": "; ".join(errors),
        })
        raise HTTPException(status_code=502, detail="; ".join(errors))

    set_item_state(queue_id, item_id, awaiting_state)
    set_item_parked_at(queue_id, item_id, _now())
    set_item_result(queue_id, item_id, {
        "action": "request-reviewers",
        "status": "completed" if not errors else "completed_with_errors",
        "message": "; ".join(actions_taken + (["(errors: " + "; ".join(errors) + ")"] if errors else [])),
        "reviewers": to_request,
        "nudged": to_nudge,
    })
    return RedirectResponse(url="/", status_code=303)


@app.post("/queues/{queue_id}/items/{item_id}/delete")
def delete(queue_id: str, item_id: int):
    try:
        sessions.abort_sessions_for_item(queue_id, item_id)
    except Exception:
        pass
    delete_item(queue_id, item_id)
    try:
        worktree.remove_worktree(item_id)
    except Exception:
        pass
    return RedirectResponse(url="/", status_code=303)


@app.get("/stats")
def stats():
    return JSONResponse(sessions.stats())


@app.get("/sessions/{session_id}")
def session_snapshot(session_id: str):
    snap = sessions.get_snapshot(session_id)
    if snap is None:
        raise HTTPException(status_code=404, detail="session not found")
    return JSONResponse(snap)


@app.post("/sessions/{session_id}/send")
async def session_send(session_id: str, text: str = Form(...)):
    text = text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="empty message")
    delivered = await sessions.send_user_message(session_id, text)
    if not delivered:
        raise HTTPException(status_code=409, detail="session is closed")
    return JSONResponse({"ok": True})


@app.post("/sessions/{session_id}/resume")
def session_resume(session_id: str):
    new_id = sessions.resume_session(session_id)
    if new_id is None:
        raise HTTPException(status_code=409, detail="session is not resumable")
    return JSONResponse({"session_id": new_id})


@app.post("/settings/global")
def update_global(request: Request,
                  max_concurrent: int = Form(...),
                  auto_resume_on_boot: str = Form(""),
                  auto_refresh_seconds: int = Form(180),
                  dry_run: str = Form("")):
    """Bump (or trim) the global session cap. Applies live via semaphore
    resize — new/queued sessions pick up the new cap immediately; existing
    in-flight sessions finish their current turn at the old cap.
    Also persists `auto_resume_on_boot` — when true, the startup sweep
    resumes any interrupted action that left an SDK session id behind.
    `dry_run` is a runtime override on top of `actions.dry_run` in
    config.yaml — flipping it from the UI doesn't require a restart."""
    if max_concurrent < 1 or max_concurrent > 64:
        raise HTTPException(status_code=400,
                            detail="max_concurrent must be between 1 and 64")
    update_global_setting("max_concurrent", int(max_concurrent))
    update_global_setting(
        "auto_resume_on_boot",
        auto_resume_on_boot.lower() in ("1", "true", "on", "yes"),
    )
    update_global_setting(
        "dry_run",
        dry_run.lower() in ("1", "true", "on", "yes"),
    )
    # 0 disables auto-refresh entirely; any other value clamped to
    # >= 30s inside _auto_refresh_interval(). The change takes effect
    # after restart (the refresh threads are started once at boot).
    refresh = int(auto_refresh_seconds)
    if refresh < 0 or refresh > 3600:
        raise HTTPException(status_code=400,
                            detail="auto_refresh_seconds must be 0–3600")
    update_global_setting("auto_refresh_seconds", refresh)
    try:
        sessions.resize_semaphore(int(max_concurrent))
    except Exception as exc:
        print(f"[settings] semaphore resize failed: {exc}")
    return _reload_or_redirect(request)


@app.get("/queues/{queue_id}/definition")
def queue_definition(queue_id: str):
    """Return one queue's current configuration (from config.yaml),
    for the edit-query form."""
    queues = get_queues_config()
    q = next((q for q in queues if q.get("id") == queue_id), None)
    if q is None:
        raise HTTPException(status_code=404, detail="unknown queue")
    return JSONResponse({
        "id": q.get("id"),
        "title": q.get("title"),
        "max_in_flight": q.get("max_in_flight"),
        "query": q.get("query") or {},
    })


@app.post("/queues/{queue_id}/definition")
def update_queue_definition_endpoint(
    request: Request,
    queue_id: str,
    title: str = Form(""),
    repo_id: str = Form(""),
    q_author: str = Form(""),
    q_state: str = Form("open"),
    q_review_requested: str = Form(""),
    q_labels: str = Form(""),
    q_assignee: str = Form(""),
    q_milestone: str = Form(""),
    q_search: str = Form(""),
    h_ci_status: str = Form(""),
    h_merge_state: str = Form(""),
    h_review_threads: str = Form(""),
    f_attention_only: str = Form(""),
    f_non_draft: str = Form(""),
    triage_skill: str = Form(""),
    states_json: str = Form(""),
    multi_bucket: str = Form(""),
):
    """Rewrite one queue's query in config.yaml, preserving comments
    and structure. Labels are a comma-separated string in the form;
    stored as a list. The state machine arrives as `states_json` (a
    list of `{name, original_name, is_initial, is_done, is_awaiting}`)
    so we can detect renames vs adds vs deletes. Renames migrate
    every affected item in SQL; deletes refuse if items still occupy
    the column.

    On a query change, clear this queue's items so the next fetch
    tick repopulates against the new query — otherwise stale cards
    from the old query linger on the board. Pure state-machine
    edits (no query change) keep their items intact (the renamer
    already migrated them)."""
    queues_cfg = get_queues_config()
    q_existing = next((q for q in queues_cfg
                       if q.get("id") == queue_id), None)
    if q_existing is None:
        raise HTTPException(status_code=404, detail="unknown queue")

    labels_list = [l.strip() for l in q_labels.split(",") if l.strip()]
    query_updates: dict = {
        "author": q_author.strip() or None,
        "state": q_state.strip() or None,
        "review_requested": q_review_requested.strip() or None,
        "assignee": q_assignee.strip() or None,
        "milestone": q_milestone.strip() or None,
        "labels": labels_list or None,
        "search": q_search.strip() or None,
    }
    # Reject underspecified queries — `state: open` alone matches every
    # open PR in the repo, which is almost never what the user means.
    if not github.query_has_discriminator(query_updates):
        raise HTTPException(
            status_code=400,
            detail=("Query is too broad. Add at least one filter: an "
                    "author, review-requested login, assignee, "
                    "milestone, label, or a Search query string. "
                    "`state: open` by itself matches every open PR in "
                    "the repo."),
        )
    # Prune None so we don't write empty keys into yaml.
    query_updates = {k: v for k, v in query_updates.items() if v is not None}

    def _on(v: str) -> bool:
        return (v or "").lower() in ("on", "true", "1", "yes")
    hydrate_updates: dict = {
        "ci_status": _on(h_ci_status) or None,
        "merge_state": _on(h_merge_state) or None,
        "review_threads": _on(h_review_threads) or None,
    }
    hydrate_updates = {k: v for k, v in hydrate_updates.items() if v}
    filter_updates: dict = {
        "attention_only": _on(f_attention_only) or None,
        "non_draft": _on(f_non_draft) or None,
    }
    filter_updates = {k: v for k, v in filter_updates.items() if v}

    updates: dict = {"query": query_updates}
    if title.strip():
        updates["title"] = title.strip()
    # `null` deletes the key from yaml so toggles can be cleared by
    # leaving every checkbox in the form unchecked.
    updates["hydrate"] = hydrate_updates or None
    updates["filter"] = filter_updates or None
    # Triage skill override. Empty string falls back to the bundled
    # `triage-generic-pr` (or whatever the queue's built-in triager
    # picks) — passing None to update_queue_definition deletes the key.
    updates["triage_skill"] = triage_skill.strip() or None
    # Per-queue repo. Required — every queue must explicitly target a
    # registered repo (no implicit "use default" fallback). The form
    # marks the select required, so empty here means a programmatic
    # POST that's missing the field.
    rid = (repo_id or "").strip()
    if not rid:
        raise HTTPException(
            status_code=400,
            detail="Pick a repo. Every queue needs an explicit target.",
        )
    if not github.repo_by_id(rid):
        raise HTTPException(
            status_code=400,
            detail=f"unknown repo: {rid}. Add it to the repos "
                   f"registry first.",
        )
    updates["repo"] = rid

    # State machine: parse the JSON the form serialized from the row
    # editor. Each entry carries its original_name so we can
    # distinguish a rename from a delete + add. update_queue_definition
    # does the validation (unique names, role assignments must be in
    # `states`, deleted columns can't have items, etc.).
    if states_json.strip():
        try:
            entries = json.loads(states_json)
            if not isinstance(entries, list):
                raise ValueError("states_json must be a JSON array")
        except (json.JSONDecodeError, ValueError) as exc:
            raise HTTPException(
                status_code=400, detail=f"states_json: {exc}")
        new_states: list[str] = []
        renames: dict[str, str] = {}
        initial_list: list[str] = []
        done_state = None
        awaiting_state = None
        seen: set[str] = set()
        for e in entries:
            if not isinstance(e, dict):
                raise HTTPException(
                    status_code=400,
                    detail="each states_json entry must be an object")
            name = (e.get("name") or "").strip()
            if not name:
                raise HTTPException(
                    status_code=400, detail="state name is required")
            if name in seen:
                raise HTTPException(
                    status_code=400,
                    detail=f"duplicate state name: {name!r}")
            seen.add(name)
            new_states.append(name)
            orig = (e.get("original_name") or "").strip()
            if orig and orig != name:
                renames[orig] = name
            if e.get("is_initial"):
                initial_list.append(name)
            if e.get("is_done"):
                done_state = name
            if e.get("is_awaiting"):
                awaiting_state = name
        if not initial_list:
            raise HTTPException(
                status_code=400,
                detail="at least one state must be marked initial")
        if not (multi_bucket or "").lower() in ("on", "true", "1", "yes"):
            # Single-bucket queue — keep only the first initial.
            initial_list = initial_list[:1]
        updates["states"] = new_states
        updates["_state_renames"] = renames
        updates["initial_state"] = initial_list[0]
        if len(initial_list) > 1:
            updates["initial_states"] = initial_list
        else:
            # Drop the multi-bucket field when collapsing back to one.
            updates["initial_states"] = None
        updates["done_state"] = done_state
        updates["awaiting_state"] = awaiting_state

    # Did the query actually change? If not, skip the post-save wipe
    # so pure state-machine edits keep their items (already migrated
    # by the rename helper). The structured fields are compared at
    # the same shape we'd serialize them.
    old_query = q_existing.get("query") or {}
    new_query_for_compare = {k: v for k, v in query_updates.items()
                             if v not in (None, "", [])}
    old_query_for_compare = {k: v for k, v in old_query.items()
                             if v not in (None, "", [])}
    query_changed = (new_query_for_compare != old_query_for_compare)

    try:
        update_queue_definition(queue_id, updates)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if query_changed:
        # Wipe this queue's items so the next fetch repopulates from
        # the new query. Without this, cards matching the OLD query
        # stick around until the user manually deletes or closes them.
        def _clear(state):
            bucket = state.get("queues", {}).get(queue_id)
            if bucket:
                bucket["items"] = []
        _mutate(_clear)

    # Kick an immediate fetch in a background thread so the user sees
    # new cards show up without waiting for the auto-refresh tick.
    threading.Thread(
        target=run_queue, args=(queue_id,),
        kwargs={"wait_for_triage": False},
        daemon=True,
    ).start()

    return _reload_or_redirect(request)


@app.post("/queues/compose")
def queue_compose(
    prompt: str = Form(...),
    current_yaml: str = Form(""),
):
    """Run the compose-queue skill on a natural-language prompt,
    return the generated YAML for the user to review. Does NOT
    write to config.yaml — that's still the explicit Save click.
    """
    if not prompt or not prompt.strip():
        raise HTTPException(status_code=400, detail="empty prompt")
    queues_cfg = get_queues_config()
    existing_ids = [q.get("id") for q in queues_cfg if q.get("id")]
    context = {
        "prompt": prompt.strip(),
        "current_yaml": current_yaml or "",
        "existing_ids": existing_ids,
    }
    try:
        session_id, result = sessions.run_session_blocking(
            "compose-queue", context,
            cwd=str(PROJECT_ROOT),
            kind="compose",
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"compose failed: {exc}")
    status = result.get("status")
    yaml_out = result.get("yaml")
    message = result.get("message") or ""
    if status == "error" or not yaml_out:
        raise HTTPException(
            status_code=400,
            detail=message or "compose skill returned no YAML"
        )
    # Also parse the YAML to a form-shaped dict so the client can
    # populate the structured form directly. The skill keeps emitting
    # YAML (its native output); we translate here. Failure is
    # graceful — the caller still gets the YAML string.
    fields = None
    try:
        from ruamel.yaml import YAML as _YAML
        parsed = _YAML(typ="safe").load(yaml_out)
        if isinstance(parsed, dict):
            fields = _yaml_to_form_fields(parsed)
    except Exception as exc:
        print(f"[compose] YAML→form parse failed: {exc}")
    return JSONResponse({
        "yaml": yaml_out,
        "fields": fields,
        "message": message,
        "session_id": session_id,
    })


def _yaml_to_form_fields(parsed: dict) -> dict:
    """Translate a parsed queue YAML dict into the form-shape JSON
    the client populates. Mirrors the structured fields the form
    knows about; fields outside that set are ignored (the form can't
    render them anyway). Used by the compose-queue endpoint to
    populate the new-queue / edit-queue form after AI generation."""
    q = (parsed.get("query") or {}) if isinstance(parsed.get("query"), dict) else {}
    repo_val = parsed.get("repo")
    if isinstance(repo_val, dict):
        owner = repo_val.get("owner") or ""
        name = repo_val.get("name") or ""
        repo_id_or_slug = f"{owner}/{name}" if owner and name else ""
    else:
        repo_id_or_slug = repo_val or ""
    hydrate = parsed.get("hydrate") or {}
    pfilter = parsed.get("filter") or {}
    states = list(parsed.get("states") or [])
    initial_states = list(parsed.get("initial_states") or [])
    initial_state = parsed.get("initial_state") or (
        initial_states[0] if initial_states else None)
    done_state = parsed.get("done_state")
    awaiting_state = parsed.get("awaiting_state")
    state_rows = []
    for s in states:
        state_rows.append({
            "name": s,
            "original_name": "",
            "is_initial": (s in initial_states or s == initial_state),
            "is_done": (s == done_state),
            "is_awaiting": (s == awaiting_state),
        })
    return {
        "id": parsed.get("id") or "",
        "title": parsed.get("title") or "",
        "max_in_flight": parsed.get("max_in_flight") or 10,
        "repo_id": repo_id_or_slug,
        "query": {
            "author": q.get("author") or "",
            "state": q.get("state") or "open",
            "review_requested": q.get("review_requested") or "",
            "assignee": q.get("assignee") or "",
            "milestone": q.get("milestone") or "",
            "labels": q.get("labels") or [],
            "search": q.get("search") or "",
        },
        "hydrate": {
            "ci_status": bool(hydrate.get("ci_status")),
            "merge_state": bool(hydrate.get("merge_state")),
            "review_threads": bool(hydrate.get("review_threads")),
        },
        "filter": {
            "attention_only": bool(pfilter.get("attention_only")),
            "non_draft": bool(pfilter.get("non_draft")),
        },
        "triage_skill": parsed.get("triage_skill") or "",
        "states": state_rows,
        "multi_bucket": len(initial_states) > 1,
    }


@app.get("/queues/new/template")
def queue_new_template():
    """Return a YAML template for a brand-new queue, used to seed the
    new-queue modal's Raw YAML editor."""
    return JSONResponse({"yaml": new_queue_template()})


def _post_add_queue(parsed: dict):
    """Shared body for the two new-queue endpoints. Writes to
    config.yaml, pre-creates the queue's state bucket, and kicks a
    fetch so the new queue shows up populated within a poll tick."""
    try:
        added = add_queue_block(parsed)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    qid = added["id"]

    # Pre-create the state bucket so downstream set_item_* calls have
    # something to insert into.
    def _ensure(state):
        state.setdefault("queues", {}).setdefault(
            qid, {"items": [], "created_at": _now()})
    _mutate(_ensure)

    threading.Thread(
        target=run_queue, args=(qid,),
        kwargs={"wait_for_triage": False},
        daemon=True,
    ).start()
    return qid


@app.post("/queues/new/form")
def queue_new_form(
    id: str = Form(...),
    title: str = Form(...),
    max_in_flight: int = Form(10),
    repo_id: str = Form(""),
    q_author: str = Form(""),
    q_state: str = Form("open"),
    q_review_requested: str = Form(""),
    q_labels: str = Form(""),
    q_assignee: str = Form(""),
    q_milestone: str = Form(""),
    q_search: str = Form(""),
    h_ci_status: str = Form(""),
    h_merge_state: str = Form(""),
    h_review_threads: str = Form(""),
    f_attention_only: str = Form(""),
    f_non_draft: str = Form(""),
    triage_skill: str = Form(""),
    states_json: str = Form(""),
    multi_bucket: str = Form(""),
):
    """Create a new queue from the structured form. Defaults to the
    standard state machine (in triage / in progress / awaiting update
    / done). The form's state-machine section can override every
    column name + role; pass it as `states_json` (same shape as the
    edit-queue handler)."""
    query: dict = {}
    if q_author.strip(): query["author"] = q_author.strip()
    if q_state.strip(): query["state"] = q_state.strip()
    if q_review_requested.strip():
        query["review_requested"] = q_review_requested.strip()
    if q_assignee.strip(): query["assignee"] = q_assignee.strip()
    if q_milestone.strip(): query["milestone"] = q_milestone.strip()
    labels = [l.strip() for l in q_labels.split(",") if l.strip()]
    if labels: query["labels"] = labels
    if q_search.strip(): query["search"] = q_search.strip()

    # Reject underspecified queries (see edit-queue handler for the
    # full rationale — `state: open` alone matches every open PR).
    if not github.query_has_discriminator(query):
        raise HTTPException(
            status_code=400,
            detail=("Query is too broad. Add at least one filter: an "
                    "author, review-requested login, assignee, "
                    "milestone, label, or a Search query string."),
        )

    def _on(v: str) -> bool:
        return (v or "").lower() in ("on", "true", "1", "yes")
    hydrate: dict = {}
    if _on(h_ci_status): hydrate["ci_status"] = True
    if _on(h_merge_state): hydrate["merge_state"] = True
    if _on(h_review_threads): hydrate["review_threads"] = True
    post_filter: dict = {}
    if _on(f_attention_only): post_filter["attention_only"] = True
    if _on(f_non_draft): post_filter["non_draft"] = True

    # State machine: same parsing shape as the edit handler. When
    # the form didn't render the section (e.g., raw POST bypasses),
    # fall back to the standard four-state machine.
    state_machine = _parse_states_json(states_json, multi_bucket)
    parsed = {
        "id": id.strip(),
        "title": title.strip(),
        "max_in_flight": int(max_in_flight),
        "query": query,
    }
    parsed.update(state_machine)
    if hydrate: parsed["hydrate"] = hydrate
    if post_filter: parsed["filter"] = post_filter
    if triage_skill.strip():
        parsed["triage_skill"] = triage_skill.strip()
    rid = (repo_id or "").strip()
    if not rid:
        raise HTTPException(
            status_code=400,
            detail="Pick a repo. Every queue needs an explicit target.",
        )
    if not github.repo_by_id(rid):
        raise HTTPException(
            status_code=400,
            detail=f"unknown repo: {rid}. Add it to the repos "
                   f"registry first.",
        )
    parsed["repo"] = rid
    _post_add_queue(parsed)
    return RedirectResponse(url="/", status_code=303)


def _parse_states_json(states_json: str, multi_bucket: str) -> dict:
    """Translate the form's `states_json` payload into the queue
    schema's state-machine fields. Returns a dict with `states`,
    `initial_state`, `initial_states` (multi-bucket only),
    `done_state`, `awaiting_state`. Falls back to the standard
    4-state machine when no payload is given.
    """
    if not (states_json or "").strip():
        return {
            "initial_state": "in triage",
            "done_state": "done",
            "awaiting_state": "awaiting update",
            "states": ["in triage", "in progress",
                       "awaiting update", "done"],
        }
    try:
        entries = json.loads(states_json)
        if not isinstance(entries, list):
            raise ValueError("states_json must be a JSON array")
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(
            status_code=400, detail=f"states_json: {exc}")
    new_states: list[str] = []
    initial_list: list[str] = []
    done_state = None
    awaiting_state = None
    seen: set[str] = set()
    for e in entries:
        if not isinstance(e, dict):
            raise HTTPException(
                status_code=400,
                detail="each states_json entry must be an object")
        name = (e.get("name") or "").strip()
        if not name:
            raise HTTPException(
                status_code=400, detail="state name is required")
        if name in seen:
            raise HTTPException(
                status_code=400,
                detail=f"duplicate state name: {name!r}")
        seen.add(name)
        new_states.append(name)
        if e.get("is_initial"):
            initial_list.append(name)
        if e.get("is_done"):
            done_state = name
        if e.get("is_awaiting"):
            awaiting_state = name
    if not new_states:
        raise HTTPException(
            status_code=400,
            detail="states must have at least one entry")
    if not initial_list:
        raise HTTPException(
            status_code=400,
            detail="at least one state must be marked initial")
    is_multi = (multi_bucket or "").lower() in ("on", "true", "1", "yes")
    out: dict = {
        "states": new_states,
        "initial_state": initial_list[0],
    }
    if is_multi and len(initial_list) > 1:
        out["initial_states"] = initial_list
    if done_state:
        out["done_state"] = done_state
    if awaiting_state:
        out["awaiting_state"] = awaiting_state
    return out


@app.post("/queues/new/raw")
def queue_new_raw(yaml_text: str = Form(...)):
    """Create a new queue from raw YAML. Full control over state
    machine, query, labels, etc."""
    from ruamel.yaml import YAML
    y = YAML()
    try:
        parsed = y.load(yaml_text)
    except Exception as exc:
        raise HTTPException(status_code=400,
                            detail=f"YAML parse error: {exc}")
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=400,
                            detail="YAML must describe a single queue mapping")
    _post_add_queue(parsed)
    return RedirectResponse(url="/", status_code=303)


@app.get("/queues/{queue_id}/definition/raw")
def queue_definition_raw(queue_id: str):
    """Return the queue's config.yaml block as a YAML string. Used
    by the settings modal's YAML tab to load the editor with the
    current content."""
    try:
        return JSONResponse({"yaml": get_queue_block_yaml(queue_id)})
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown queue")


@app.post("/queues/{queue_id}/definition/raw")
def update_queue_definition_raw(request: Request,
                                queue_id: str,
                                yaml_text: str = Form(...)):
    """Replace the queue's entire block in config.yaml from a raw
    YAML string. Validation: must parse, must be a mapping, must
    include the required keys, and the id must match the URL param
    (renaming via YAML save isn't allowed because the state file is
    keyed by id)."""
    try:
        replace_queue_block(queue_id, yaml_text)
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown queue")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Same post-save behavior as the structured form: clear items
    # and kick a fresh fetch so the new definition takes effect.
    def _clear(state):
        bucket = state.get("queues", {}).get(queue_id)
        if bucket:
            bucket["items"] = []
    _mutate(_clear)
    threading.Thread(
        target=run_queue, args=(queue_id,),
        kwargs={"wait_for_triage": False},
        daemon=True,
    ).start()
    return _reload_or_redirect(request)


@app.post("/queues/{queue_id}/settings")
def update_queue_settings(request: Request,
                          queue_id: str,
                          max_in_flight: int = Form(...),
                          worker_slots: int = Form(...),
                          intake_paused: str = Form("")):
    """Per-queue settings. `worker_slots` is clamped to the global cap so
    a deprioritized queue can't accidentally monopolize all sessions."""
    queues_by_id = {q["id"]: q for q in get_queues_config()}
    if queue_id not in queues_by_id:
        raise HTTPException(status_code=404, detail="unknown queue")
    cfg = load_config()
    cfg_cap = int((cfg.get("sessions") or {}).get("max_concurrent", 8))
    global_cap = int(get_global_setting("max_concurrent", cfg_cap))
    if max_in_flight < 0 or max_in_flight > 500:
        raise HTTPException(status_code=400,
                            detail="max_in_flight out of range")
    worker_slots = max(1, min(int(worker_slots), global_cap))
    update_queue_setting(queue_id, "max_in_flight", int(max_in_flight))
    update_queue_setting(queue_id, "worker_slots", int(worker_slots))
    update_queue_setting(queue_id, "intake_paused",
                         intake_paused.lower() in ("1", "true", "on", "yes"))
    return _reload_or_redirect(request)


# ============================================================= tasks board
# Ad-hoc tasks: user submits a prompt + repo, we spawn a `do-task`
# session in a fresh worktree. Parallel track to the PR-triage queues.

from . import tasks as _tasks  # noqa: E402


# ============================================================ repos registry
# CRUD over the top-level `repos:` list in config.yaml. Driven by the
# global settings popover; queue forms reference entries by id.

@app.post("/repos/new")
def repo_new(request: Request,
             id: str = Form(...),
             owner: str = Form(...),
             name: str = Form(...),
             display_name: str = Form("")):
    """Append a new entry to the repos registry."""
    try:
        add_repo_block({
            "id": id, "owner": owner, "name": name,
            "display_name": display_name,
        })
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _reload_or_redirect(request)


@app.post("/repos/{repo_id}/delete")
def repo_delete(request: Request, repo_id: str):
    """Remove a repo from the registry. Refuses if it's the default
    or referenced by any queue (with an actionable message)."""
    try:
        delete_repo_block(repo_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown repo: {repo_id}")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _reload_or_redirect(request)


@app.post("/repos/{repo_id}/set-default")
def repo_set_default(request: Request, repo_id: str):
    """Set the top-level default_repo_id."""
    try:
        set_default_repo(repo_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown repo: {repo_id}")
    return _reload_or_redirect(request)


@app.post("/tasks/new")
def new_task(request: Request,
             repo_id: str = Form(...),
             prompt: str = Form(...),
             task_type: str = Form(default=_tasks.TASK_TYPE_DEFAULT)):
    """Create a task and spawn the `do-task` session. Returns to the
    main page — the task appears in the In Progress column as soon
    as the session acks startup."""
    prompt = (prompt or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is required")
    if not github.repo_by_id(repo_id):
        raise HTTPException(status_code=400, detail=f"unknown repo: {repo_id}")
    if task_type not in _tasks.TASK_TYPES:
        task_type = _tasks.TASK_TYPE_DEFAULT
    try:
        task = _tasks.create_task(repo_id=repo_id, prompt=prompt,
                                  task_type=task_type)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    # Dispatch in a background thread so the POST returns quickly —
    # worktree setup can shell out to `git fetch` which is slow.
    def _dispatch():
        try:
            _tasks.dispatch_task(task["id"])
        except Exception as exc:
            _tasks.update_task(task["id"], status="stuck",
                               last_result={
                                   "status": "error",
                                   "message": f"spawn failed: {exc}",
                               })
    threading.Thread(target=_dispatch, daemon=True).start()
    return _reload_or_redirect(request)


@app.post("/queues/{queue_id}/items/{item_id}/spawn-pr-task")
def spawn_pr_task(request: Request, queue_id: str, item_id: int,
                  pr_number: int = Form(...),
                  pr_title: str = Form(default="")):
    """Spawn an Ad Hoc Task pre-targeted at a PR linked from an
    issue card. Used as the cross-context "go act on this linked PR"
    affordance — clicking the `+ task` chip on an issue's linked-PR
    list lands you on the Tasks board with a session ready to triage
    and act on that PR.

    The pre-filled prompt primes the do-task skill toward "triage
    first, recommend, wait for OK before mutating" so the user's
    review-and-confirm flow happens in the session transcript
    instead of dispatching a side-effect immediately.
    """
    item = find_item(load_state(), queue_id, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="item not found")
    qcfg = {q["id"]: q for q in get_queues_config()}.get(queue_id, {})
    repo_id = github.queue_repo_id(qcfg)
    if not repo_id:
        raise HTTPException(status_code=400,
                            detail="couldn't resolve repo_id for queue")
    issue_number = item.get("number")
    title_clean = (pr_title or "").strip()
    title_part = f' — "{title_clean}"' if title_clean else ""
    # Smart prompt: triage-first conversational, no surprise mutations.
    # The do-task skill takes it from here. Comment bodies for any
    # mutation get drafted in the session for OK before posting.
    prompt = (
        f"Triage and act on PR #{pr_number}{title_part} (linked from "
        f"issue #{issue_number}).\n\n"
        f"Step 1 — read the PR's current state via `gh pr view "
        f"{pr_number} --repo {repo_id}/...`: CI status / "
        f"mergeStateStatus / reviewDecision / unresolved review "
        f"threads / recent comments. Skim the diff if it's small.\n\n"
        f"Step 2 — recommend ONE next action and explain why. "
        f"Options: approve-merge, rebase, fix-precommit, attempt-fix, "
        f"nudge-author, await-update, close-with-thanks, prompt. "
        f"Note constraints (self-authored? branch protection? "
        f"author is a bot?).\n\n"
        f"Step 3 — WAIT for my OK before mutating. When I confirm, "
        f"draft any comment body for me to review in your reply, "
        f"then post + execute the action via gh CLI.\n\n"
        f"For approve-merge specifically: skip self-approval (GitHub "
        f"blocks it) — go straight to `gh pr merge --squash --auto`. "
        f"For close: comment-then-close, never silent on a non-self "
        f"non-bot PR."
    )
    try:
        task = _tasks.create_task(repo_id=repo_id, prompt=prompt,
                                  task_type="pr")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    def _dispatch():
        try:
            _tasks.dispatch_task(task["id"])
        except Exception as exc:
            _tasks.update_task(task["id"], status="stuck",
                               last_result={
                                   "status": "error",
                                   "message": f"spawn failed: {exc}",
                               })
    threading.Thread(target=_dispatch, daemon=True).start()
    return _reload_or_redirect(request)


# Self-improvement feedback hook. Per-card "(?)" button posts here
# with the user's free-text prompt; we bundle the card's full
# context (raw, triage_notes, proposal, actions, last_result,
# history) and spawn an Ad Hoc Task targeting the Sisyphus repo
# itself so a Claude Code session can investigate and open a PR
# with proposed skill / triage-logic changes. The user reviews and
# merges the PR like any other.
_SISYPHUS_FEEDBACK_REPO_ID = "sisyphus"
_SISYPHUS_CARD_DUMP_BUDGET = 30000  # ~30KB JSON cap, leaves room for prompt


@app.post("/queues/{queue_id}/items/{item_id}/spawn-feedback-task")
def spawn_feedback_task(request: Request, queue_id: str, item_id: int,
                         user_prompt: str = Form(...)):
    """Spawn an Ad Hoc Task on the Sisyphus repo with the user's
    feedback about this card's triage. Includes the full card
    context as a JSON dump in the prompt, plus the user's free-text
    description of what's wrong / what they want changed.

    Result: the Claude Code session investigates the relevant skills
    + triage code and opens a PR on the Sisyphus repo with proposed
    fixes. Standard PR review/merge from there.
    """
    user_prompt = (user_prompt or "").strip()
    if not user_prompt:
        raise HTTPException(status_code=400, detail="prompt is required")
    if not github.repo_by_id(_SISYPHUS_FEEDBACK_REPO_ID):
        raise HTTPException(
            status_code=400,
            detail=f"feedback target repo `{_SISYPHUS_FEEDBACK_REPO_ID}` "
                   f"not in repos: registry — add it to config.yaml.",
        )
    item = find_item(load_state(), queue_id, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="item not found")

    # Card dump: everything a Claude session would need to reason
    # about why this card looks the way it does. JSON-serialised and
    # capped at the budget so the prompt stays manageable.
    qcfg = {q["id"]: q for q in get_queues_config()}.get(queue_id, {})
    src_repo_id = github.queue_repo_id(qcfg) or "(unknown)"
    card_dump = {
        "queue_id": queue_id,
        "queue_label": qcfg.get("label") or queue_id,
        "queue_kind": qcfg.get("kind") or "pr",
        "source_repo_id": src_repo_id,
        "item": {
            "id": item.get("id"),
            "number": item.get("number"),
            "title": item.get("title"),
            "url": item.get("url"),
            "state": item.get("state"),
            "actions": item.get("actions") or [],
            "proposal": item.get("proposal"),
            "triage_source": item.get("triage_source"),
            "triage_notes": item.get("triage_notes") or {},
            "last_result": item.get("last_result"),
            "raw": item.get("raw") or {},
            "history": (item.get("history") or [])[-10:],
        },
    }
    dump = json.dumps(card_dump, indent=2, default=str)
    truncated = False
    if len(dump) > _SISYPHUS_CARD_DUMP_BUDGET:
        dump = dump[:_SISYPHUS_CARD_DUMP_BUDGET]
        truncated = True

    item_label = f"#{item.get('number')}" if item.get("number") else f"item {item_id}"
    title_part = f' — "{item.get("title")}"' if item.get("title") else ""
    truncation_note = "\n\n(card dump truncated to fit budget)" if truncated else ""
    prompt = (
        f"User feedback on Sisyphus's triage of "
        f"{src_repo_id} {item_label}{title_part} (queue: {queue_id}).\n\n"
        f"### USER PROMPT\n\n"
        f"{user_prompt}\n\n"
        f"### CARD CONTEXT\n\n"
        f"```json\n{dump}\n```"
        f"{truncation_note}"
        f"\n\n"
        f"### YOUR JOB\n\n"
        f"Investigate the relevant Sisyphus code that produced the "
        f"behavior the user is describing. The most common targets "
        f"are:\n"
        f"- `.claude/skills/*/SKILL.md` (triage skill prompts — most "
        f"narrative bugs live here)\n"
        f"- `repobot/triage.py` (mechanical action menu — most "
        f"action-availability bugs live here)\n"
        f"- `repobot/runner.py` (refresh / retriage flow)\n"
        f"- `repobot/actions.py` (action registry, dispatch)\n"
        f"- `repobot/templates/_card.html`, `static/style.css` (UI)\n\n"
        f"Read the user's prompt, correlate with the card context, "
        f"identify root cause, propose a fix. Keep the change "
        f"minimal and aligned with existing patterns. Open a PR on "
        f"the Sisyphus repo with a clear commit message explaining "
        f"the bug and the fix; the user will review/merge like any "
        f"other PR. Skip side-effects on the source repo "
        f"({src_repo_id}); this task is about Sisyphus itself.\n\n"
        f"BEFORE you start: read CLAUDE.md and MEMORY.md at the repo "
        f"root. CLAUDE.md has the architectural invariants; MEMORY.md "
        f"has learned-pattern facts from prior incidents — your bug "
        f"may already be flagged there, and if so the fix is to align "
        f"with the documented learning, not relitigate it.\n\n"
        f"AFTER you've drafted a fix: judge whether the underlying "
        f"lesson is durable (i.e., generalizes beyond the one card "
        f"that triggered the report) or a one-off. If durable, "
        f"include a one-line entry addition to MEMORY.md in the same "
        f"PR — under the appropriate section, with a brief Why "
        f"naming the symptom (\"PR #N — ...\"). If a one-off, skip "
        f"the MEMORY.md change."
    )

    try:
        task = _tasks.create_task(repo_id=_SISYPHUS_FEEDBACK_REPO_ID,
                                  prompt=prompt, task_type="pr")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    def _dispatch():
        try:
            _tasks.dispatch_task(task["id"])
        except Exception as exc:
            _tasks.update_task(task["id"], status="stuck",
                               last_result={
                                   "status": "error",
                                   "message": f"spawn failed: {exc}",
                               })
    threading.Thread(target=_dispatch, daemon=True).start()
    return _reload_or_redirect(request)


# Self-update: pull latest Sisyphus main and re-exec the server so
# the user can merge a PR via the tool and immediately consume the
# new code. `git pull --ff-only` so local edits abort the update
# instead of being silently overwritten.
@app.get("/admin/config-status", response_class=JSONResponse)
def admin_config_status():
    """Run every registered config check and return a summary. Used
    by the header "config needed" banner — both for the initial
    page render (via Jinja context) and for the modal's "Recheck"
    button (via fetch). Cheap; safe to call as often as you like."""
    return JSONResponse(_config_checks.summary())


@app.post("/admin/self-update")
def admin_self_update(request: Request):
    """Run `git pull --ff-only` against origin/main on the Sisyphus
    repo the server is running from, then re-exec the current
    process via os.execv so the new code is loaded. SSE clients
    reconnect automatically.

    Aborts (without restart) if the working tree is dirty or the
    pull is non-ff — the user investigates manually rather than us
    losing state."""
    import os
    import sys
    import subprocess
    from pathlib import Path

    # The Sisyphus repo is the parent of the repobot package directory.
    repo_root = Path(__file__).resolve().parent.parent
    if not (repo_root / ".git").exists():
        raise HTTPException(
            status_code=500,
            detail=f"{repo_root} is not a git repo — can't self-update.",
        )

    def _run(*cmd: str) -> tuple[int, str, str]:
        proc = subprocess.run(cmd, cwd=str(repo_root),
                              capture_output=True, text=True)
        return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()

    rc, out, err = _run("git", "status", "--porcelain")
    if rc != 0:
        raise HTTPException(status_code=500,
                            detail=f"git status failed: {err}")
    if out:
        raise HTTPException(
            status_code=409,
            detail=f"working tree dirty — refusing to self-update.\n{out}",
        )

    _run("git", "fetch", "origin", "main")
    rc, out, err = _run("git", "pull", "--ff-only", "origin", "main")
    if rc != 0:
        raise HTTPException(
            status_code=409,
            detail=f"git pull --ff-only failed (non-ff or conflict?):\n{err or out}",
        )

    head_rc, head_sha, _ = _run("git", "rev-parse", "--short", "HEAD")
    summary = f"pulled to {head_sha if head_rc == 0 else '?'}: {out}"
    print(f"[self-update] {summary} — re-execing server")

    # Re-exec the same Python with the same argv. New process loads
    # the freshly-pulled code; the old process is replaced. Schedule
    # the exec slightly after the response goes out so the client
    # gets the redirect rather than a connection-reset.
    def _restart():
        import time
        time.sleep(0.5)
        try:
            os.execv(sys.executable, [sys.executable] + sys.argv)
        except Exception as exc:
            print(f"[self-update] os.execv failed: {exc}")

    threading.Thread(target=_restart, daemon=True).start()
    return _reload_or_redirect(request)


@app.post("/tasks/{task_id}/delete")
def delete_task_endpoint(task_id: int):
    """Remove a task record, abort any in-flight session, and prune
    the worktree. Mirror of the per-queue-item delete path."""
    try:
        sessions.abort_sessions_for_item(None, task_id)
    except Exception:
        pass
    _tasks.remove_task_worktree(task_id)
    _tasks.delete_task(task_id)
    return RedirectResponse(url="/", status_code=303)


@app.post("/tasks/{task_id}/retry")
def retry_task_endpoint(request: Request, task_id: int):
    """Re-dispatch a task with its original prompt + repo + type.
    Aborts any live session, resets the worktree to a clean state on
    the same `ce/task-{N}` branch, and spawns a fresh `do-task`
    session. Useful for stuck tasks where the skill bailed and the
    user wants another go without re-typing the prompt."""
    task = _tasks.find_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    try:
        sessions.abort_sessions_for_item(None, task_id)
    except Exception:
        pass
    _tasks.update_task(task_id, status="in_progress",
                       session_id=None, last_result=None)

    def _dispatch():
        try:
            _tasks.dispatch_task(task_id)
        except Exception as exc:
            _tasks.update_task(task_id, status="stuck", last_result={
                "status": "error",
                "message": f"retry failed: {exc}",
            })
    threading.Thread(target=_dispatch, daemon=True).start()
    return _reload_or_redirect(request)


@app.post("/tasks/clear-done")
def clear_done_tasks(request: Request):
    """Bulk-delete every task in the Done column. Aborts sessions
    (none should be live for a done task, but be defensive) and
    prunes their worktrees. Mirror of the per-queue clear-done."""
    for t in _tasks.list_tasks():
        if t.get("status") != "done":
            continue
        tid = t.get("id")
        if tid is None:
            continue
        try:
            sessions.abort_sessions_for_item(None, tid)
        except Exception:
            pass
        _tasks.remove_task_worktree(tid)
        _tasks.delete_task(tid)
    return _reload_or_redirect(request)


@app.post("/tasks/{task_id}/create-pr")
def create_pr_from_task(request: Request, task_id: int):
    """Open a PR from a task's worktree branch. Used when a task
    produced commits but the skill didn't already open the PR
    (e.g. a question-mode task that incidentally fixed something).
    Pushes the branch + runs `gh pr create`; stamps pr_url on the
    task so subsequent renders link out instead of offering the
    button again."""
    task = _tasks.find_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    if task.get("pr_url"):
        return _reload_or_redirect(request)
    repo = github.repo_by_id(task.get("repo_id"))
    if not repo:
        raise HTTPException(status_code=400, detail="task has no valid repo")
    wt_path = _tasks.task_worktree_path(task_id)
    if not (wt_path / ".git").exists():
        raise HTTPException(status_code=400,
                            detail="worktree is gone — can't open a PR for it")
    branch = task.get("branch") or f"sisyphus/task-{task_id}"
    title = task.get("title") or task["prompt"][:60]
    body_lines = [
        f"_Opened from Sisyphus task-{task_id}._",
        "",
        "**Original prompt:**",
        "",
        f"> {task['prompt']}",
    ]
    body = "\n".join(body_lines)
    import subprocess as _sp
    try:
        _sp.run(
            ["git", "push", "--set-upstream", "origin",
             f"HEAD:{branch}"],
            cwd=str(wt_path), capture_output=True, text=True, check=True,
        )
        result = _sp.run(
            ["gh", "pr", "create",
             "--repo", repo["slug"],
             "--title", title,
             "--body", body,
             "--head", branch],
            cwd=str(wt_path), capture_output=True, text=True, check=True,
        )
        url = (result.stdout or "").strip().splitlines()[-1]
    except _sp.CalledProcessError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"PR creation failed: {exc.stderr or exc.stdout}",
        )
    last = dict(task.get("last_result") or {})
    last["pr_url"] = url
    _tasks.update_task(task_id, pr_url=url, last_result=last)
    return _reload_or_redirect(request)


@app.post("/tasks/{task_id}/open-issue")
def open_issue_endpoint(request: Request, task_id: int):
    """Open the drafted issue on GitHub. Uses the issue_title /
    issue_body emitted by the do-task skill on completion. Stamps
    the resulting issue URL onto the task record so the card can
    link out to it."""
    task = _tasks.find_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    last = task.get("last_result") or {}
    title = last.get("issue_title") or task.get("title")
    body = last.get("issue_body") or ""
    if not title:
        raise HTTPException(status_code=400,
                            detail="task has no drafted issue title")
    repo = github.repo_by_id(task.get("repo_id"))
    if not repo:
        raise HTTPException(status_code=400, detail="task has no valid repo")
    import subprocess as _sp
    try:
        with github.repo_scope(repo["slug"]):
            result = _sp.run(
                ["gh", "issue", "create",
                 "--repo", repo["slug"],
                 "--title", title,
                 "--body", body or "_(no body)_"],
                capture_output=True, text=True, check=True,
            )
        url = (result.stdout or "").strip().splitlines()[-1]
    except _sp.CalledProcessError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"gh issue create failed: {exc.stderr or exc.stdout}",
        )
    new_result = dict(last)
    new_result["issue_url"] = url
    _tasks.update_task(task_id, issue_url=url, last_result=new_result)
    return _reload_or_redirect(request)


@app.get("/fragments/tasks/body", response_class=HTMLResponse)
def tasks_body(request: Request):
    """HTML fragment for the tasks board — creation form + status
    columns. Polled by HTMX like the queue-body fragments."""
    items = _tasks.list_tasks()
    by_status: dict[str, list[dict]] = {s: [] for s in _tasks.TASK_STATUSES}
    for t in items:
        by_status.setdefault(t.get("status", "in_progress"), []).append(t)
    repos = github.list_repos()
    return templates.TemplateResponse(
        request, "_tasks_board.html",
        {
            "request": request,
            "tasks_by_status": by_status,
            "repos": repos,
            "task_types": _tasks.TASK_TYPES,
            "default_task_type": _tasks.TASK_TYPE_DEFAULT,
        },
    )
