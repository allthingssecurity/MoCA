#!/usr/bin/env python3
"""Correctness, graded by the OFFICIAL SWE-bench harness. We author no tests.

Runs each arm's patches through swebench's Docker evaluation: a run is RESOLVED
only if every FAIL_TO_PASS test now passes AND every PASS_TO_PASS test still
passes. That second half matters -- it is what catches a patch that "fixes" the
bug by breaking something else, which is exactly the failure mode a cheap
delegated model is most likely to produce.

We deliberately do not hand-roll any of this. The point of using an external
benchmark is that neither we nor the agents get to define what "correct" means.

Usage:
    python harness/grade.py --arm all
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from arms import ARMS

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "runs"
PREDS = ROOT / "data" / "preds"
DATASET = "princeton-nlp/SWE-bench_Verified"


def build_predictions(arm: str) -> Path | None:
    """Convert our patch.diff files into the harness's predictions format."""
    preds = []
    for ledger_path in sorted((RUNS / arm).glob("*/ledger.json")):
        d = ledger_path.parent / "patch.diff"
        patch = d.read_text() if d.exists() else ""
        preds.append({
            "instance_id": ledger_path.parent.name,
            "model_name_or_path": arm,
            # An empty patch is a legitimate prediction: it means the agent failed
            # to produce anything. It will simply be graded unresolved.
            "model_patch": patch,
        })
    if not preds:
        return None
    PREDS.mkdir(parents=True, exist_ok=True)
    out = PREDS / f"{arm}.json"
    out.write_text(json.dumps(preds, indent=1))
    return out


def grade(arm: str, workers: int) -> dict:
    preds_path = build_predictions(arm)
    if not preds_path:
        return {"arm": arm, "error": "no runs"}

    run_id = f"sidekick_{arm}"
    cmd = [
        sys.executable, "-m", "swebench.harness.run_evaluation",
        "--dataset_name", DATASET,
        "--predictions_path", str(preds_path),
        "--run_id", run_id,
        "--max_workers", str(workers),
        # Native arm64 eval images exist -- no x86 emulation penalty.
        "--namespace", "swebench",
    ]
    print(f"  grading {arm} …", flush=True)
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    (RUNS / arm / "grade.log").write_text(proc.stdout + "\n--- stderr ---\n" + proc.stderr)

    # The harness writes <model_name_or_path>.<run_id>.json in cwd.
    report_path = ROOT / f"{arm}.{run_id}.json"
    if not report_path.exists():
        return {"arm": arm, "error": "harness produced no report; see grade.log",
                "tail": proc.stdout[-1500:]}

    rep = json.loads(report_path.read_text())
    resolved = set(rep.get("resolved_ids", []))
    return {
        "arm": arm,
        "total": rep.get("total_instances"),
        "submitted": rep.get("submitted_instances"),
        "resolved_ids": sorted(resolved),
        "unresolved_ids": sorted(rep.get("unresolved_ids", [])),
        "error_ids": sorted(rep.get("error_ids", [])),
        "empty_patch_ids": sorted(rep.get("empty_patch_ids", [])),
        "resolved": len(resolved),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", default="all")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    arms = list(ARMS) if args.arm == "all" else [args.arm]
    results = [grade(a, args.workers) for a in arms]
    (ROOT / "data" / "correctness.json").write_text(json.dumps(results, indent=1))

    print()
    print(f"{'arm':26} {'resolved':>10} {'submitted':>10} {'empty':>6} {'errors':>7}")
    print("-" * 64)
    for r in results:
        if r.get("error"):
            print(f"{r['arm']:26} ERROR: {r['error']}")
            continue
        n = r["submitted"] or 0
        pct = f"{r['resolved']}/{n}" + (f" ({100*r['resolved']/n:.0f}%)" if n else "")
        print(f"{r['arm']:26} {pct:>10} {n:>10} "
              f"{len(r['empty_patch_ids']):>6} {len(r['error_ids']):>7}")

    print("\nresolved = ALL fail_to_pass now pass AND ALL pass_to_pass still pass.")
    print("Graded by the official SWE-bench harness; we authored none of these tests.")


if __name__ == "__main__":
    main()
