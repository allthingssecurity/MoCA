#!/usr/bin/env python3
"""Sanity-check the benchmark BEFORE spending 60 agent runs on it.

Run each task's GOLD patch (the real upstream fix, shipped with the dataset)
through the official harness. If the gold patch does not resolve the instance in
*this* environment, the instance is broken here and must be dropped -- keeping it
would penalise all three arms equally for something no agent could ever fix, and
quietly drag every score down.

This is not hypothetical. psf__requests-2317's FAIL_TO_PASS list includes
test_POSTBIN_GET_POST_FILES and test_HTTP_302_ALLOW_REDIRECT_GET, which make live
HTTP calls to httpbin. They 503 in the eval container. The upstream fix itself
cannot pass them.

Output: data/usable.json -- the instances an agent could actually be graded on.

Usage:
    python harness/validate_tasks.py --workers 4
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATASET = "princeton-nlp/SWE-bench_Verified"
RUN_ID = "gold_validation"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    tasks = json.loads((ROOT / "data" / "tasks.json").read_text())

    preds = [{
        "instance_id": t["instance_id"],
        "model_name_or_path": "gold",
        "model_patch": t["patch"],          # the real upstream fix
    } for t in tasks]

    preds_path = ROOT / "data" / "preds" / "gold.json"
    preds_path.parent.mkdir(parents=True, exist_ok=True)
    preds_path.write_text(json.dumps(preds, indent=1))

    print(f"validating {len(preds)} instances with their GOLD patches "
          f"(this also pre-builds every eval image)…\n")

    proc = subprocess.run(
        [sys.executable, "-m", "swebench.harness.run_evaluation",
         "--dataset_name", DATASET,
         "--predictions_path", str(preds_path),
         "--run_id", RUN_ID,
         "--max_workers", str(args.workers),
         "--namespace", "swebench"],
        cwd=ROOT, capture_output=True, text=True,
    )
    (ROOT / "data" / "gold_validation.log").write_text(
        proc.stdout + "\n--- stderr ---\n" + proc.stderr)

    report = ROOT / f"gold.{RUN_ID}.json"
    if not report.exists():
        sys.exit("harness produced no report; see data/gold_validation.log\n"
                 + proc.stdout[-2000:])

    rep = json.loads(report.read_text())
    usable = sorted(rep.get("resolved_ids", []))
    broken = sorted(set(t["instance_id"] for t in tasks) - set(usable))

    by_diff = {t["instance_id"]: t["difficulty"] for t in tasks}
    (ROOT / "data" / "usable.json").write_text(json.dumps({
        "usable": usable,
        "broken": broken,
        "note": "broken = the GOLD upstream patch does not resolve this instance "
                "in this environment (usually live-network tests). No agent could "
                "pass it either, so it is excluded from all arms.",
    }, indent=1))

    print(f"\n  usable: {len(usable)}/{len(tasks)}")
    if broken:
        print(f"  DROPPED (gold patch fails here):")
        for b in broken:
            print(f"    - {b:38} [{by_diff[b]}]")
        print("\n  Re-sample replacements from the same difficulty strata "
              "before running the arms, or accept a smaller n.")
    else:
        print("  every instance is gradeable. proceed.")


if __name__ == "__main__":
    main()
