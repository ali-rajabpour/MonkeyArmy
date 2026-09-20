"""§7.6: multi-task batch manifests — dependency-ordered waves (Kahn's
algorithm), a conservative overlap check between tasks the caller intends to
run in parallel, and sequential integration on finish.
"""

from __future__ import annotations

import json
import secrets
import string
import time
from pathlib import Path
from typing import Any

from config import Defaults
from persistence import repo_state_dir, slug_for

_BASE36 = string.digits + string.ascii_lowercase


def _to_base36(n: int) -> str:
    if n == 0:
        return "0"
    out = []
    while n:
        n, r = divmod(n, 36)
        out.append(_BASE36[r])
    return "".join(reversed(out))


def new_batch_id() -> str:
    ts = _to_base36(int(time.time() * 1000))
    rand = "".join(secrets.choice(_BASE36) for _ in range(4))
    return f"b_{ts}_{rand}"


def manifest_path(repo: str, batch_id: str) -> Path:
    return repo_state_dir(repo) / "batches" / f"{batch_id}.json"


def save_manifest(manifest: dict[str, Any]) -> None:
    path = manifest_path(manifest["repo"], manifest["batchId"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def load_manifest(repo: str, batch_id: str) -> dict[str, Any] | None:
    try:
        return json.loads(manifest_path(repo, batch_id).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


# ── Dependency ordering (Kahn's algorithm) ───────────────────────────────

def _topo_waves(tasks: list[dict[str, Any]]) -> list[list[str]]:
    keys = [t["key"] for t in tasks]
    deps = {t["key"]: list(t.get("dependsOn") or []) for t in tasks}
    indegree = {k: 0 for k in keys}
    children: dict[str, list[str]] = {k: [] for k in keys}
    for k, ds in deps.items():
        for d in ds:
            children[d].append(k)
            indegree[k] += 1

    waves: list[list[str]] = []
    remaining = set(keys)
    while remaining:
        wave = sorted(k for k in remaining if indegree[k] == 0)
        if not wave:
            raise ValueError("cycle detected in dependsOn graph")
        waves.append(wave)
        for k in wave:
            remaining.discard(k)
            for c in children[k]:
                indegree[c] -= 1
    return waves


def _depends_on_chain(key: str, deps: dict[str, list[str]]) -> set[str]:
    """Every key `key` depends on, directly or transitively."""
    chain: set[str] = set()
    stack = list(deps.get(key, []))
    while stack:
        d = stack.pop()
        if d in chain:
            continue
        chain.add(d)
        stack.extend(deps.get(d, []))
    return chain


def _glob_prefix(pattern: str) -> str:
    for i, ch in enumerate(pattern):
        if ch in "*?[":
            return pattern[:i]
    return pattern  # no wildcard — the whole literal path


def _files_overlap(files_a: list[str], files_b: list[str]) -> bool:
    """Conservative, string-only disjointness check (no real glob-vs-glob
    intersection — that's a much bigger problem than a batch validator
    needs to solve). Two files/globs are flagged as overlapping when either
    is an exact string match, or one's literal prefix (the part before the
    first `*`/`?`/`[`) is a prefix of the other's — e.g. `src/**/*.py` (prefix
    `src/`) vs `src/utils.py` (prefix is the whole string) overlaps, but
    `src/a/*.py` vs `src/b/*.py` does not.

    Known false positives this trades for zero false negatives: two exact,
    unrelated filenames where one string is a prefix of the other (e.g.
    `app.py` vs `app.py.bak`) are flagged as overlapping even though they
    never collide. An unrestricted task (empty `allowedFiles`) is always
    treated as overlapping with everything, since nothing can prove it
    disjoint from any other task's files.
    """
    if not files_a or not files_b:
        return True
    for a in files_a:
        pa = _glob_prefix(a)
        for b in files_b:
            pb = _glob_prefix(b)
            if a == b or pa.startswith(pb) or pb.startswith(pa):
                return True
    return False


def validate(tasks: list[dict[str, Any]]) -> list[list[str]]:
    """Raises ValueError (message meant for the supervisor) on the first
    problem: duplicate keys, an unknown dependsOn target, a cycle, or two
    tasks with no dependency relation between them whose allowedFiles
    overlap. Returns the topological waves on success."""
    if not tasks:
        raise ValueError("tasks_json must be a non-empty list")
    keys = [t["key"] for t in tasks]
    if len(keys) != len(set(keys)):
        dupes = sorted({k for k in keys if keys.count(k) > 1})
        raise ValueError(f"duplicate task keys: {', '.join(dupes)}")

    key_set = set(keys)
    for t in tasks:
        for dep in t.get("dependsOn") or []:
            if dep not in key_set:
                raise ValueError(f"task {t['key']!r} depends on unknown key {dep!r}")

    waves = _topo_waves(tasks)  # raises ValueError on a cycle

    deps = {t["key"]: list(t.get("dependsOn") or []) for t in tasks}
    by_key = {t["key"]: t for t in tasks}
    for i, a_key in enumerate(keys):
        a_chain = _depends_on_chain(a_key, deps)
        for b_key in keys[i + 1:]:
            if b_key in a_chain or a_key in _depends_on_chain(b_key, deps):
                continue  # related by a dependency path — sequential, not parallel
            a_files = by_key[a_key].get("allowedFiles") or []
            b_files = by_key[b_key].get("allowedFiles") or []
            if _files_overlap(a_files, b_files):
                if not a_files or not b_files:
                    empty_key = a_key if not a_files else b_key
                    raise ValueError(
                        f"task {empty_key!r} has no allowedFiles (unrestricted) and cannot be "
                        f"parallelized; add allowedFiles or a dependsOn edge"
                    )
                raise ValueError(
                    f"tasks {a_key!r} and {b_key!r} are parallelizable (no dependency relation) "
                    f"but their allowedFiles overlap — add a dependsOn edge or narrow allowedFiles"
                )
    return waves


# ── Manifest lifecycle (§6.11) ───────────────────────────────────────────

def create(
    repo: str, goal: str, tasks: list[dict[str, Any]],
    verify_command: str | None = None, mode: str | None = None,
) -> dict[str, Any]:
    waves = validate(tasks)
    repo_abs = str(Path(repo).resolve())
    batch_id = new_batch_id()
    manifest = {
        "batchId": batch_id, "repo": repo_abs, "slug": slug_for(repo_abs),
        "goal": goal, "createdAt": time.time(), "finishedAt": None,
        "verifyCommand": verify_command, "integrateMode": mode,
        "tasks": [
            {
                "key": t["key"], "title": t.get("title", t["key"]),
                "dependsOn": list(t.get("dependsOn") or []),
                "allowedFiles": list(t.get("allowedFiles") or []),
                "taskId": None,
            }
            for t in tasks
        ],
    }
    save_manifest(manifest)
    return {"batch_id": batch_id, "order": waves}


def find_manifest(batch_id: str) -> tuple[str, dict[str, Any]] | None:
    """Search every repo the server has ever seen for `batch_id`'s manifest.
    Lets `batch(action='status'|'finish', ...)` work without repo_path — the
    skill's own §2.7 call doesn't pass one, and a batch_id is already
    globally unique (new_batch_id), so there's nothing repo_path adds here
    except which repo to look in first.
    """
    from persistence import all_repos

    for repo in all_repos():
        manifest = load_manifest(repo, batch_id)
        if manifest is not None:
            return repo, manifest
    return None


def link(repo: str, batch_id: str, key: str, task_id: str) -> None:
    manifest = load_manifest(repo, batch_id)
    if manifest is None:
        raise KeyError(f"unknown batch_id {batch_id!r}")
    for t in manifest["tasks"]:
        if t["key"] == key:
            t["taskId"] = task_id
            save_manifest(manifest)
            return
    raise KeyError(f"unknown task key {key!r} in batch {batch_id!r}")


def status(repo: str, batch_id: str) -> dict[str, Any]:
    from jobs import get_job_with_fallback

    manifest = load_manifest(repo, batch_id)
    if manifest is None:
        raise KeyError(f"unknown batch_id {batch_id!r}")

    rows: list[dict[str, Any]] = []
    total_cost = 0.0
    for t in manifest["tasks"]:
        row: dict[str, Any] = {"key": t["key"], "title": t["title"], "task_id": t.get("taskId")}
        job = get_job_with_fallback(t["taskId"]) if t.get("taskId") else None
        if job:
            row["status"] = job.get("status")
            row["attempt"] = job.get("attempt", 1)
            row["cost_usd"] = job.get("costUsd")
            row["review"] = job.get("review")
            total_cost += job.get("costUsd") or 0
        rows.append(row)
    return {
        "batch_id": batch_id, "goal": manifest["goal"], "finished_at": manifest.get("finishedAt"),
        "tasks": rows, "total_cost_usd": round(total_cost, 4),
    }


def finish(repo: str, batch_id: str, verify_command: str | None, mode: str | None, defaults: Defaults) -> dict[str, Any]:
    import git_ops
    import verify as verify_mod
    from jobs import get_job_with_fallback

    manifest = load_manifest(repo, batch_id)
    if manifest is None:
        raise KeyError(f"unknown batch_id {batch_id!r}")
    repo_abs = manifest["repo"]

    # 1. Blockers, checked BEFORE integrating anything.
    ordered_keys = [k for wave in _topo_waves(manifest["tasks"]) for k in wave]
    jobs_by_key: dict[str, dict[str, Any] | None] = {}
    blockers: list[str] = []
    for t in manifest["tasks"]:
        key, task_id = t["key"], t.get("taskId")
        if not task_id:
            blockers.append(f"{key}: never dispatched (no task_id)")
            continue
        job = get_job_with_fallback(task_id)
        jobs_by_key[key] = job
        if not job:
            blockers.append(f"{key}: task_id {task_id} not found")
            continue
        st = job.get("status")
        if st == "integrated":
            continue
        if st != "succeeded":
            blockers.append(f"{key}: status is {st!r}, not succeeded")
        elif (job.get("review") or {}).get("verdict") != "approve":
            blockers.append(f"{key}: not yet approved (review_task)")
    if blockers:
        return {"batch_id": batch_id, "finished": False, "blockers": blockers}

    # 2. Integrate in topological order, stopping (without rolling back
    #    anything already integrated) at the first refusal.
    integrate_mode = mode or manifest.get("integrateMode") or defaults.integrate_mode
    integrated: list[dict[str, Any]] = []
    for key in ordered_keys:
        job = jobs_by_key.get(key)
        if job is None or job.get("status") == "integrated":
            integrated.append({"key": key, "task_id": job.get("taskId") if job else None,
                                "integrated": True, "skipped": True})
            continue
        outcome = git_ops.integrate(job, repo_abs, None, integrate_mode, False)
        integrated.append({"key": key, **outcome})
        if not outcome.get("integrated"):
            return {
                "batch_id": batch_id, "finished": False, "integrated": integrated,
                "stopped_at": key, "reason": outcome.get("reason"), "suggestion": outcome.get("suggestion"),
            }

    # 3. Final verify — a failure is reported, not rolled back (the commits
    #    are the user's to revert).
    final_verify = None
    vc = verify_command or manifest.get("verifyCommand")
    if vc:
        final_verify = verify_mod.run_command(vc, repo_abs, defaults.verify_timeout_s)

    # 4. End-state assertion + report (§5.4).
    task_ids = [t.get("taskId") for t in manifest["tasks"] if t.get("taskId")]
    end_state = git_ops.assert_end_state(repo_abs, task_ids)

    def jf(key: str, name: str, default: Any = None) -> Any:
        job = jobs_by_key.get(key) or {}
        return job.get(name, default)

    task_rows = [
        {
            "key": t["key"], "title": t["title"], "task_id": t.get("taskId"),
            "status": jf(t["key"], "status", "integrated"), "attempts": jf(t["key"], "attempt", 1),
            "cost_usd": jf(t["key"], "costUsd"), "added": (jf(t["key"], "diffstat") or {}).get("added"),
            "removed": (jf(t["key"], "diffstat") or {}).get("removed"),
            "integrated_sha": jf(t["key"], "integratedSha"),
        }
        for t in manifest["tasks"]
    ]
    report = {
        "tasks": task_rows,
        "workerCostUsd": round(sum(jf(t["key"], "costUsd") or 0 for t in manifest["tasks"]), 4),
        "workerTokens": sum(jf(t["key"], "totalTokens") or 0 for t in manifest["tasks"]),
        "priced": any(jf(t["key"], "priced") for t in manifest["tasks"]),
        "linesAdded": sum((jf(t["key"], "diffstat") or {}).get("added") or 0 for t in manifest["tasks"]),
        "linesRemoved": sum((jf(t["key"], "diffstat") or {}).get("removed") or 0 for t in manifest["tasks"]),
        "finalVerify": final_verify,
        "endState": end_state,
    }
    manifest["finishedAt"] = time.time()
    manifest["report"] = report
    save_manifest(manifest)
    return {"batch_id": batch_id, "finished": True, "integrated": integrated, "report": report}
