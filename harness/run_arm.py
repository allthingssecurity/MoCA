#!/usr/bin/env python3
"""Run one (task, arm) pair and emit a candidate patch + a usage ledger.

Integrity guards, because both of these would silently inflate the numbers:

  1. HISTORY LEAKAGE. Checking out base_commit leaves the real fix commit sitting
     in the object store on another branch -- `git log --all` finds it. So after
     checkout we destroy .git and re-init. The future genuinely does not exist.

  2. WEB LEAKAGE. Every SWE-bench solution is a public GitHub PR. An agent with
     web access can just look it up. Claude arms run with WebSearch/WebFetch
     denied; Codex runs network-sandboxed.

Usage:
    python harness/run_arm.py --arm A_opus_solo --instance psf__requests-2317
    python harness/run_arm.py --arm all --instance all
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from arms import ARMS, BANNED_CLAUDE_TOOLS, COMMON_TASK_PROMPT, DELEGATION_POLICY

ROOT = Path(__file__).resolve().parent.parent
HARNESS = Path(__file__).resolve().parent
TASKS = json.loads((ROOT / "data" / "tasks.json").read_text())
MIRRORS = ROOT / "data" / "mirrors"
RUNS = ROOT / "runs"
HELPER_HOME = HARNESS / "codex_home_helper"
SOLO_HOME = HARNESS / "codex_home_solo"
TIMEOUT_S = 45 * 60


def verify_arm(arm: dict, outdir: Path, ledger: dict) -> None:
    """Refuse to record a run whose Codex model is not the one we pinned.

    This exists because it already happened once: mcp-server ignored `-c model=`
    and quietly ran the helper on a frontier model. A wrong-model run that looks
    successful is worse than a crash, so we crash.
    """
    expected = arm["helper"] or (arm["model"] if arm["engine"] == "codex" else None)
    if not expected:
        return
    home = HELPER_HOME if arm["helper"] else SOLO_HOME
    repo_cwd = str(outdir / "repo")

    seen = set()
    for f in (home / "sessions").rglob("rollout-*.jsonl"):
        cwd = None
        for line in f.open():
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("type") == "session_meta":
                cwd = d["payload"].get("cwd")
            elif d.get("type") == "turn_context" and cwd == repo_cwd:
                seen.add(d["payload"].get("model"))
    bad = seen - {expected}
    if bad:
        raise SystemExit(
            f"ABORT: {ledger['arm']}/{ledger['instance_id']} ran Codex on {sorted(bad)}, "
            f"expected {expected!r}. The model pin is broken -- results would be invalid."
        )
    ledger["codex_models_seen"] = sorted(seen)


def sh(cmd, cwd=None, timeout=None, env=None, check=True):
    return subprocess.run(
        cmd, cwd=cwd, timeout=timeout, check=check,
        capture_output=True, text=True,
        env={**os.environ, **(env or {})},
    )


def ensure_mirror(repo: str) -> Path:
    """One bare mirror per repo, reused across arms and instances."""
    dest = MIRRORS / (repo.replace("/", "__") + ".git")
    if dest.exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"    cloning mirror {repo} (once)…", flush=True)
    sh(["git", "clone", "--bare", "--quiet", f"https://github.com/{repo}.git", str(dest)],
       timeout=30 * 60)
    return dest


def materialise_repo(task: dict, workdir: Path) -> Path:
    """Repo at base_commit, with all history destroyed so the fix is unreachable."""
    mirror = ensure_mirror(task["repo"])
    repo = workdir / "repo"
    if repo.exists():
        shutil.rmtree(repo)
    sh(["git", "clone", "--quiet", "--no-checkout", str(mirror), str(repo)])
    sh(["git", "checkout", "--quiet", "--detach", task["base_commit"]], cwd=repo)

    # GUARD 1: nuke history. Without this, `git log --all` reveals the real fix.
    shutil.rmtree(repo / ".git")
    sh(["git", "init", "--quiet", "-b", "base"], cwd=repo)
    sh(["git", "add", "-A"], cwd=repo)
    sh(["git", "-c", "user.email=e@e", "-c", "user.name=eval",
        "commit", "--quiet", "-m", "base"], cwd=repo)
    return repo


def run_claude(task, arm, repo: Path, outdir: Path) -> dict:
    prompt = COMMON_TASK_PROMPT.format(problem_statement=task["problem_statement"])
    cmd = [
        "claude", "-p", prompt,
        "--output-format", "json",
        "--model", arm["model"],
        "--dangerously-skip-permissions",
        "--disallowedTools", *BANNED_CLAUDE_TOOLS,   # GUARD 2
    ]

    if arm["helper"]:
        # Mount Codex as an MCP server scoped to this repo. --strict-mcp-config so
        # no ambient MCP servers from the user's global config leak into the arm.
        #
        # The model is pinned via CODEX_HOME, NOT via `-c model=`: mcp-server
        # silently ignores that flag and falls back to the user's global default
        # (see setup_codex_homes.py). verify_arm() below catches it if it ever
        # regresses -- a silent frontier-model fallback must not reach the results.
        mcp = {
            "mcpServers": {
                "codex": {
                    "command": "codex",
                    "args": ["mcp-server"],
                    "env": {"CODEX_HOME": str(HELPER_HOME)},
                }
            }
        }
        mcp_path = outdir / "mcp.json"
        mcp_path.write_text(json.dumps(mcp, indent=1))
        cmd += ["--mcp-config", str(mcp_path), "--strict-mcp-config",
                "--append-system-prompt", DELEGATION_POLICY]

    t0 = time.time()
    proc = subprocess.run(cmd, cwd=repo, capture_output=True, text=True,
                          timeout=TIMEOUT_S, stdin=subprocess.DEVNULL)
    wall = time.time() - t0
    (outdir / "agent.stdout").write_text(proc.stdout)
    (outdir / "agent.stderr").write_text(proc.stderr)

    try:
        res = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"engine": "claude", "ok": False, "wall_s": wall,
                "error": "unparseable claude output", "raw": proc.stdout[-2000:]}

    return {
        "engine": "claude",
        "ok": not res.get("is_error", False),
        "wall_s": wall,
        "num_turns": res.get("num_turns"),
        "session_id": res.get("session_id"),
        # Claude Code shadow-prices itself. We keep it, but tokens stay primary.
        "claude_cost_usd": res.get("total_cost_usd"),
        "claude_model_usage": res.get("modelUsage"),
        "stop_reason": res.get("stop_reason"),
    }


def run_codex(task, arm, repo: Path, outdir: Path) -> dict:
    prompt = COMMON_TASK_PROMPT.format(problem_statement=task["problem_statement"])
    cmd = [
        "codex", "exec", prompt,
        "-m", arm["model"],
        "--json",
        "--sandbox", "workspace-write",   # GUARD 2: no network egress
        "--skip-git-repo-check",
    ]
    t0 = time.time()
    proc = subprocess.run(cmd, cwd=repo, capture_output=True, text=True,
                          timeout=TIMEOUT_S, stdin=subprocess.DEVNULL,
                          env={**os.environ, "CODEX_HOME": str(SOLO_HOME)})
    wall = time.time() - t0
    (outdir / "agent.stdout").write_text(proc.stdout)
    (outdir / "agent.stderr").write_text(proc.stderr)

    usage, thread_id = None, None
    for line in proc.stdout.splitlines():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("type") == "thread.started":
            thread_id = d.get("thread_id") or d.get("threadId")
        if d.get("type") == "turn.completed" and d.get("usage"):
            usage = d["usage"]   # last one wins = cumulative final turn

    return {
        "engine": "codex",
        "ok": usage is not None,
        "wall_s": wall,
        "thread_id": thread_id,
        "codex_usage": usage,          # tokens only -- Codex reports no $
        "codex_model": arm["model"],
    }


def run_one(task: dict, arm_name: str) -> dict:
    arm = ARMS[arm_name]
    outdir = RUNS / arm_name / task["instance_id"]
    outdir.mkdir(parents=True, exist_ok=True)

    repo = materialise_repo(task, outdir)
    runner = run_claude if arm["engine"] == "claude" else run_codex
    try:
        ledger = runner(task, arm, repo, outdir)
    except subprocess.TimeoutExpired:
        ledger = {"engine": arm["engine"], "ok": False, "error": "timeout",
                  "wall_s": TIMEOUT_S}

    # The candidate patch is whatever the agent left in the working tree.
    # Exclude tests: the prompt forbids touching them, and this enforces it.
    diff = sh(["git", "diff", "--", ".", ":(exclude)*test*", ":(exclude)*tests*"],
              cwd=repo, check=False).stdout
    (outdir / "patch.diff").write_text(diff)

    ledger.update({
        "instance_id": task["instance_id"],
        "arm": arm_name,
        "repo": task["repo"],
        "difficulty": task["difficulty"],
        "patch_bytes": len(diff),
        "empty_patch": not diff.strip(),
    })

    # Crash rather than record a run that used the wrong Codex model.
    verify_arm(arm, outdir, ledger)

    (outdir / "ledger.json").write_text(json.dumps(ledger, indent=1))

    # Free the checkout; the patch and ledger are what we keep.
    shutil.rmtree(repo, ignore_errors=True)
    return ledger


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", required=True, help="arm name, or 'all'")
    ap.add_argument("--instance", required=True, help="instance_id, or 'all'")
    ap.add_argument("--limit", type=int, default=None, help="cap task count (pilot)")
    args = ap.parse_args()

    arm_names = list(ARMS) if args.arm == "all" else [args.arm]
    tasks = TASKS if args.instance == "all" else [
        t for t in TASKS if t["instance_id"] == args.instance]

    # Only run instances the GOLD patch can actually resolve here. Running an arm
    # on a broken instance burns hours and produces a guaranteed failure that has
    # nothing to do with the agent.
    usable_path = ROOT / "data" / "usable.json"
    if usable_path.exists():
        usable = set(json.loads(usable_path.read_text())["usable"])
        skipped = [t["instance_id"] for t in tasks if t["instance_id"] not in usable]
        tasks = [t for t in tasks if t["instance_id"] in usable]
        for s in skipped:
            print(f"  skip {s}: gold patch cannot resolve it here", flush=True)
    else:
        print("  WARNING: no data/usable.json -- run validate_tasks.py first, or you "
              "will grade arms on instances no agent can pass.", flush=True)

    if args.limit:
        tasks = tasks[: args.limit]
    if not tasks:
        sys.exit(f"no runnable task matching {args.instance!r}")

    for task in tasks:
        for arm_name in arm_names:
            print(f"[{arm_name}] {task['instance_id']} ({task['difficulty']})", flush=True)
            led = run_one(task, arm_name)
            flag = "EMPTY PATCH" if led["empty_patch"] else f"{led['patch_bytes']}B"
            print(f"    -> ok={led['ok']} {flag} {led.get('wall_s', 0):.0f}s", flush=True)


if __name__ == "__main__":
    main()
