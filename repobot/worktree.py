"""Per-PR git worktree management.

Worktrees live under workspace/worktrees/pr-{number} and share .git with
the main clone at workspace/{repo_name}. Actions that need to modify the
PR's branch (rebase, lockfile regen) run inside one of these.
"""
import subprocess
from pathlib import Path

from .config import PROJECT_ROOT, load_config

WORKSPACE_DIR = PROJECT_ROOT / "workspace"
WORKTREES_DIR = WORKSPACE_DIR / "worktrees"


def repo_path() -> Path:
    """Path to the main clone of the default repo. Task-level and
    cross-repo flows pass their own paths; this is the "ambient"
    default for single-repo call sites."""
    from .github import default_repo_slug
    _, name = default_repo_slug().split("/", 1)
    return WORKSPACE_DIR / name


def _git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else str(repo_path()),
        capture_output=True,
        text=True,
        check=True,
    )


def worktree_path_for(pr_number: int) -> Path:
    return WORKTREES_DIR / f"pr-{pr_number}"


def ensure_worktree(pr_number: int, head_ref: str,
                    repo_slug: str | None = None) -> Path:
    """Create (or reuse) a worktree for the PR.

    Fetches the PR's head via GitHub's `refs/pull/{N}/head` ref instead
    of the branch name — that ref is created for every PR (fork or
    in-repo) and is reachable from `origin`, so cross-fork PRs work the
    same as maintainer PRs. The worktree checks out on a bot-owned
    local branch `ce/pr-{N}`; this namespace never collides with user
    branches or other worktrees the user might have set up manually
    (a branch can only live in one worktree at a time). Pushes still
    target the upstream head ref via `git push origin HEAD:{head_ref}`
    — the local branch name is a sandbox detail.

    `repo_slug` (e.g. `apache/superset`) pins which clone to operate
    in. Cross-repo queues MUST pass it so the fetch hits the right
    origin — without it we fall back to the global default and try
    to fetch `refs/pull/{N}/head` against the wrong repo (which
    silently creates a useless local ref or errors out, depending on
    whether the wrong-repo PR ref exists).

    `head_ref` is kept as a parameter for API compatibility / future use.
    """
    del head_ref  # unused; kept in signature for API stability
    target = worktree_path_for(pr_number)
    pr_ref = f"refs/ce-pr/{pr_number}"
    local_branch = f"ce/pr-{pr_number}"

    # Resolve the clone to operate in: explicit repo_slug if provided,
    # otherwise the global default. Ensure the clone exists (lazy
    # init) before any git commands run against it.
    if repo_slug and "/" in repo_slug:
        from .workspace import WORKSPACE_DIR, ensure_repo
        owner, name = repo_slug.split("/", 1)
        ensure_repo(owner, name)
        clone_path = WORKSPACE_DIR / name
    else:
        clone_path = repo_path()

    _git("fetch", "origin",
         f"+refs/pull/{pr_number}/head:{pr_ref}", cwd=clone_path)

    if (target / ".git").exists():
        _git("reset", "--hard", pr_ref, cwd=target)
        _git("checkout", "-B", local_branch, pr_ref, cwd=target)
        return target

    WORKTREES_DIR.mkdir(parents=True, exist_ok=True)
    _git("worktree", "add", "--force", "-B", local_branch,
         str(target), pr_ref, cwd=clone_path)
    return target


def issue_worktree_path_for(issue_number: int) -> Path:
    """Issues live under a separate `issues/` subtree to keep them
    visually distinct from PR worktrees in the workspace dir, and
    because the branch namespace differs (`sisyphus/issue-{N}` vs.
    `sisyphus/pr-{N}` — bot-owned, never collides with user branches)."""
    return WORKSPACE_DIR / "issues" / f"issue-{issue_number}"


def ensure_issue_worktree(issue_number: int, repo_slug: str | None = None
                          ) -> Path:
    """Create (or refresh) a worktree for an issue-driven fix.

    Branched off the repo's default branch via `sisyphus/issue-{N}`. Unlike
    PR worktrees (which checkout `refs/pull/{N}/head`), issue
    worktrees start from a clean default-branch tip — the skill is
    going to make the fix from scratch and push to a new head.

    `repo_slug` pins which clone to operate in. Required for cross-
    repo issue queues; falls back to the global default when not
    provided. Lazy-clones via `workspace.ensure_repo`.
    """
    if repo_slug and "/" in repo_slug:
        from .workspace import WORKSPACE_DIR as _WS, ensure_repo
        owner, name = repo_slug.split("/", 1)
        ensure_repo(owner, name)
        clone_path = _WS / name
    else:
        clone_path = repo_path()

    # Resolve the default branch from the clone's HEAD ref. Done
    # once, lightweight.
    head_result = subprocess.run(
        ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
        cwd=str(clone_path), capture_output=True, text=True,
    )
    if head_result.returncode == 0 and head_result.stdout.strip():
        default_branch = head_result.stdout.strip().split("/", 1)[1]
    else:
        # Fallback — most bundled repos are master- or main-rooted.
        default_branch = "main"

    target = issue_worktree_path_for(issue_number)
    branch = f"sisyphus/issue-{issue_number}"
    target.parent.mkdir(parents=True, exist_ok=True)

    _git("fetch", "origin", default_branch, cwd=clone_path)
    base_ref = f"origin/{default_branch}"

    if (target / ".git").exists():
        _git("reset", "--hard", base_ref, cwd=target)
        _git("checkout", "-B", branch, base_ref, cwd=target)
        return target

    _git("worktree", "add", "--force", "-B", branch,
         str(target), base_ref, cwd=clone_path)
    return target


def remove_worktree(pr_number: int) -> None:
    target = worktree_path_for(pr_number)
    if not target.exists():
        return
    try:
        _git("worktree", "remove", "--force", str(target))
    except subprocess.CalledProcessError:
        pass


def remove_issue_worktree(issue_number: int) -> None:
    target = issue_worktree_path_for(issue_number)
    if not target.exists():
        return
    try:
        _git("worktree", "remove", "--force", str(target))
    except subprocess.CalledProcessError:
        pass


def existing_worktree_numbers() -> list[int]:
    """Scan workspace/worktrees/pr-*/ and return the PR numbers on disk."""
    if not WORKTREES_DIR.exists():
        return []
    nums = []
    for child in WORKTREES_DIR.iterdir():
        if child.is_dir() and child.name.startswith("pr-"):
            try:
                nums.append(int(child.name[3:]))
            except ValueError:
                continue
    return nums


def prune_orphan_worktrees(live_pr_numbers: set[int]) -> list[int]:
    """Remove any worktree whose PR number isn't in the live set.
    Returns the list of PR numbers removed."""
    removed = []
    for n in existing_worktree_numbers():
        if n not in live_pr_numbers:
            remove_worktree(n)
            removed.append(n)
    # Also run `git worktree prune` to drop stale admin entries for
    # anything that was already removed from disk.
    try:
        _git("worktree", "prune")
    except subprocess.CalledProcessError:
        pass
    return removed
