"""Ad-hoc task storage — a parallel track to the PR-triage queues.

A "task" is a free-form prompt the user wants Claude to work on
against one of the configured repos. Unlike queue items, tasks are
push-driven (user creates them; nothing pulls them from GitHub) and
live in their own worktree at `workspasisyphus/tasks/task-{id}/` on a
bot-owned branch `sisyphus/task-{id}` branched off the repo's default.

Stored in the same state file as queues, under a top-level `tasks`
key:

  {
    "queues": {...},
    "tasks": {
      "items": [
        {"id": 1, "repo_id": "sisyphus", "prompt": "fix X",
         "task_type": "pr", "status": "in_progress",
         "session_id": "...", "created_at": "...",
         "title": "...",         # optional, skill-assigned
         "last_result": {...},   # skill's final emission
         "pr_url": "...",        # set when the skill opens a PR
         "branch": "sisyphus/task-1"}
      ],
      "next_id": 2
    }
  }

Task statuses: "in_progress" | "stuck" | "done". No "queued" — the
session machinery owns that phase. "stuck" covers needs_human /
error from the skill result.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from .queues import _emit, _mutate, current_dry_run, load_state, _now

TASK_STATUSES = ("in_progress", "stuck", "done")
TASK_TYPES = ("auto", "question", "issue", "pr")
TASK_TYPE_DEFAULT = "auto"


def _tasks_root(state: dict) -> dict:
    """Return the `tasks` sub-document, creating its skeleton on first
    access so callers don't have to guard every read."""
    t = state.setdefault("tasks", {})
    t.setdefault("items", [])
    t.setdefault("next_id", 1)
    return t


def list_tasks() -> list[dict]:
    """Return every task, newest first. The UI sorts into status
    columns on its own."""
    items = _tasks_root(load_state()).get("items") or []
    return sorted(items, key=lambda t: t.get("created_at") or "",
                  reverse=True)


def find_task(task_id: int) -> dict | None:
    for t in list_tasks():
        if t.get("id") == task_id:
            return t
    return None


def create_task(repo_id: str, prompt: str,
                task_type: str = TASK_TYPE_DEFAULT) -> dict:
    """Allocate a new task id and persist a `queued`-shaped record
    without a session yet. Caller spawns the session and then uses
    `update_task(id, session_id=..., status="in_progress")` to
    transition in."""
    if task_type not in TASK_TYPES:
        task_type = TASK_TYPE_DEFAULT
    prompt = (prompt or "").strip()
    if not prompt:
        raise ValueError("prompt is required")
    if not repo_id:
        raise ValueError("repo_id is required")
    created: dict[str, Any] = {}

    def _m(state: dict) -> None:
        t = _tasks_root(state)
        tid = int(t.get("next_id") or 1)
        record = {
            "id": tid,
            "repo_id": repo_id,
            "prompt": prompt,
            "task_type": task_type,
            "status": "in_progress",
            "session_id": None,
            "created_at": _now(),
            "branch": f"sisyphus/task-{tid}",
        }
        t["items"].append(record)
        t["next_id"] = tid + 1
        created.update(record)

    _mutate(_m)
    _emit("tasks-changed", {})
    return created


def update_task(task_id: int, **fields) -> dict | None:
    """Patch the task record with the given fields and return the
    updated record. Writes a `updated_at` stamp automatically."""
    updated: dict[str, Any] = {}

    def _m(state: dict) -> None:
        t = _tasks_root(state)
        for rec in t.get("items") or []:
            if rec.get("id") == task_id:
                rec.update(fields)
                rec["updated_at"] = _now()
                updated.update(rec)
                return

    _mutate(_m)
    _emit("tasks-changed", {})
    return updated or None


def delete_task(task_id: int) -> bool:
    """Remove the task from state. Does NOT abort any running session
    or prune the worktree — callers handle that so order is explicit."""
    removed = {"hit": False}

    def _m(state: dict) -> None:
        t = _tasks_root(state)
        before = len(t.get("items") or [])
        t["items"] = [i for i in (t.get("items") or [])
                      if i.get("id") != task_id]
        removed["hit"] = len(t["items"]) < before

    _mutate(_m)
    _emit("tasks-changed", {})
    return removed["hit"]


# ---------------------------------------------------------------- worktrees

def task_worktree_path(task_id: int) -> Path:
    from .workspace import WORKSPACE_DIR
    return WORKSPACE_DIR / "tasks" / f"task-{task_id}"


def _default_branch(clone_path: Path) -> str:
    """Resolve the remote's default branch name. Tries `origin/HEAD`
    first (set by git clone), falls back to main → master probing."""
    try:
        out = subprocess.run(
            ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
            cwd=str(clone_path), capture_output=True, text=True, check=True,
        )
        ref = out.stdout.strip()  # e.g. refs/remotes/origin/main
        if ref.startswith("refs/remotes/origin/"):
            return ref[len("refs/remotes/origin/"):]
    except subprocess.CalledProcessError:
        pass
    for cand in ("main", "master"):
        r = subprocess.run(
            ["git", "rev-parse", "--verify", f"origin/{cand}"],
            cwd=str(clone_path), capture_output=True, text=True,
        )
        if r.returncode == 0:
            return cand
    return "main"


def ensure_task_worktree(task_id: int, repo_id: str) -> Path:
    """Create (or refresh) a worktree for a task at
    `workspasisyphus/tasks/task-{id}`, checked out on `sisyphus/task-{id}`
    branched off the repo's default. Ensures the base clone exists
    first via `workspace.ensure_repo`.
    """
    from . import github
    from .workspace import WORKSPACE_DIR, ensure_repo
    repo = github.repo_by_id(repo_id)
    if not repo:
        raise ValueError(f"Unknown repo id: {repo_id}")
    ensure_repo(repo["owner"], repo["name"])
    clone_path = WORKSPACE_DIR / repo["name"]
    wt_path = task_worktree_path(task_id)
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    branch = f"sisyphus/task-{task_id}"
    default_branch = _default_branch(clone_path)

    def _git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=str(clone_path),
                       capture_output=True, text=True, check=True)

    _git("fetch", "origin", default_branch)
    if (wt_path / ".git").exists():
        subprocess.run(
            ["git", "reset", "--hard", f"origin/{default_branch}"],
            cwd=str(wt_path), capture_output=True, text=True, check=True,
        )
        subprocess.run(
            ["git", "checkout", "-B", branch, f"origin/{default_branch}"],
            cwd=str(wt_path), capture_output=True, text=True, check=True,
        )
        return wt_path
    _git("worktree", "add", "--force", "-B", branch,
         str(wt_path), f"origin/{default_branch}")
    return wt_path


def remove_task_worktree(task_id: int) -> None:
    """Best-effort worktree removal. Swallowed exceptions because the
    caller path (delete-task) shouldn't fail if the worktree is half-
    missing or on another host."""
    wt = task_worktree_path(task_id)
    if not wt.exists():
        return
    # Find the repo clone to run `git worktree remove` from. Walk up
    # one level from the worktree's configured repo if needed; easiest
    # is to iterate known repos.
    from . import github
    from .workspace import WORKSPACE_DIR
    for r in github.list_repos():
        clone = WORKSPACE_DIR / r["name"]
        if not clone.exists():
            continue
        try:
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(wt)],
                cwd=str(clone), capture_output=True, text=True, check=True,
            )
            return
        except subprocess.CalledProcessError:
            continue
    # Fallback: just rm the directory if git wouldn't.
    import shutil
    shutil.rmtree(wt, ignore_errors=True)


# ------------------------------------------------------------- session spawn

def dispatch_task(task_id: int) -> str:
    """Spawn the `do-task` session for a task. Records the session id
    on the task record immediately; wires callbacks to update status
    and result as the skill progresses.

    Returns the session id. Caller is expected to have created the
    task record via `create_task()` first (otherwise the
    task-by-id lookup inside the callbacks will miss).
    """
    from . import sessions, github
    from .config import load_config

    task = find_task(task_id)
    if task is None:
        raise LookupError(f"Task {task_id} not found")

    repo = github.repo_by_id(task["repo_id"])
    if not repo:
        raise ValueError(f"Unknown repo_id: {task['repo_id']}")

    wt_path = ensure_task_worktree(task_id, task["repo_id"])

    cfg = load_config()
    dry_run = current_dry_run()
    context = {
        "task": {
            "id": task["id"],
            "repo_id": task["repo_id"],
            "prompt": task["prompt"],
            "task_type": task.get("task_type", "auto"),
        },
        "repo": {
            "id": repo["id"],
            "owner": repo["owner"],
            "name": repo["name"],
            "slug": repo["slug"],
        },
        "branch": task.get("branch") or f"sisyphus/task-{task_id}",
        "dry_run": dry_run,
    }

    def _on_started(_s):
        update_task(task_id, status="in_progress")

    def _on_first_turn(s):
        """Skill's first (typically only) turn finished. Fold its
        emitted JSON result onto the task record and move the task
        to done/stuck based on status."""
        result = dict(s.final_result or {})
        status = result.get("status", "completed")
        new_status = "done" if status in {"completed", "skipped_dry_run"} \
            else "stuck"
        updates: dict[str, Any] = {
            "status": new_status,
            "last_result": result,
        }
        if result.get("title"):
            updates["title"] = result["title"]
        if result.get("pr_url"):
            updates["pr_url"] = result["pr_url"]
        if result.get("commit_sha"):
            updates["commit_sha"] = result["commit_sha"]
        update_task(task_id, **updates)

    session_id = sessions.start_session(
        "do-task", context, str(wt_path),
        kind="task",
        queue_id=None,
        item_id=task_id,
        action_id="do-task",
        on_started=_on_started,
        on_first_turn_complete=_on_first_turn,
    )
    update_task(task_id, session_id=session_id, status="in_progress")
    return session_id
