#!/usr/bin/env python3
"""Blind quality judging: the "would you actually merge this?" axis.

Tests measure correctness. They do NOT measure whether a diff is a hack, whether
it papers over the symptom instead of the cause, or whether it drags in unrelated
churn. That gap is exactly where delegation is expected to fail: a cheap model
handed a judgment call will happily produce something that goes green and that no
maintainer would merge. If we only counted tests, we would miss the single thing
MoCA most needs to be checked for.

Blinding, because a judge that knows which arm it is grading will find what it
expects to find:
  - arm labels are stripped and replaced with neutral ids (system_1, system_2, …)
  - the label->arm mapping is randomised per instance with a fixed seed
  - all candidates for one instance are judged in a SINGLE call, so the judge
    ranks them against each other rather than drifting between separate calls
  - the gold patch is NOT shown; we are grading craft, not similarity-to-gold
    (a diff that differs from upstream can be perfectly mergeable)

Usage:
    python harness/judge.py            # judge every instance with >=2 candidates
"""
from __future__ import annotations

import json
import random
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from arms import ARMS

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "runs"
TASKS = {t["instance_id"]: t for t in json.loads((ROOT / "data" / "tasks.json").read_text())}
JUDGE_MODEL = "opus"        # judging is the one place we do not want to economise
SEED = 20260714

RUBRIC = """\
You are reviewing candidate patches for a real bug in an open-source repository.
Each was produced by a different automated system. You do not know which.

Score each candidate 1-10 on each axis. Be a demanding reviewer: 10 means you \
would merge it as-is, 5 means you would request changes, 1 means you would reject it.

  root_cause  Does it fix the actual cause, or paper over the symptom? A patch \
that special-cases the reported input to make it work scores low.
  scope       Is the change minimal and targeted? Penalise unrelated refactors, \
churn, dead code, and gratuitous reformatting.
  fit         Does it match the surrounding code's conventions and idioms, or \
does it read like it was written by someone unfamiliar with the codebase?
  robustness  Does it handle the adjacent cases a maintainer would immediately \
ask about, without over-engineering for cases that cannot occur?

Then give overall = your honest "would I merge this" score, 1-10. overall is NOT \
an average -- a patch can score well on every axis and still be unmergeable, or \
be slightly rough and still obviously right.

An EMPTY patch means the system produced nothing. Score it 1 across the board.

Return ONLY a JSON object, no prose:
{"scores": {"<id>": {"root_cause": n, "scope": n, "fit": n, "robustness": n, \
"overall": n, "one_line_verdict": "..."}}, "best": "<id>", \
"why_best": "one sentence"}
"""


def candidates(instance_id: str) -> dict[str, str]:
    out = {}
    for arm in ARMS:
        p = RUNS / arm / instance_id / "patch.diff"
        if p.exists():
            out[arm] = p.read_text()
    return out


def judge_instance(instance_id: str, cands: dict[str, str]) -> dict | None:
    task = TASKS[instance_id]

    # Randomised, seeded blinding: label order carries no information about arm.
    arms = sorted(cands)
    rng = random.Random(f"{SEED}:{instance_id}")
    rng.shuffle(arms)
    label_of = {arm: f"system_{i+1}" for i, arm in enumerate(arms)}
    arm_of = {v: k for k, v in label_of.items()}

    blocks = []
    for arm in arms:
        diff = cands[arm].strip() or "(EMPTY -- this system produced no patch)"
        blocks.append(f"<candidate id=\"{label_of[arm]}\">\n{diff}\n</candidate>")

    prompt = (
        f"{RUBRIC}\n\n<problem_statement>\n{task['problem_statement']}\n"
        f"</problem_statement>\n\n" + "\n\n".join(blocks)
    )

    proc = subprocess.run(
        ["claude", "-p", prompt, "--model", JUDGE_MODEL,
         "--output-format", "json",
         # The judge reads diffs only. It gets no tools -- it must not go looking
         # up the upstream fix and grade against that.
         "--disallowedTools", "WebSearch", "WebFetch", "Read", "Bash", "Glob", "Grep"],
        capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=900,
    )
    try:
        text = json.loads(proc.stdout)["result"]
    except (json.JSONDecodeError, KeyError):
        print(f"    !! judge call failed for {instance_id}")
        return None

    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        print(f"    !! judge returned no JSON for {instance_id}")
        return None
    try:
        verdict = json.loads(m.group(0))
    except json.JSONDecodeError:
        print(f"    !! judge JSON unparseable for {instance_id}")
        return None

    # Unblind only now, after the judgement is fixed.
    scores = {arm_of[lab]: sc for lab, sc in verdict.get("scores", {}).items()
              if lab in arm_of}
    return {
        "instance_id": instance_id,
        "difficulty": task["difficulty"],
        "scores_by_arm": scores,
        "best_arm": arm_of.get(verdict.get("best", "")),
        "why_best": verdict.get("why_best"),
        "blinding": label_of,
    }


def main():
    ids = sorted({p.parent.name for arm in ARMS
                  for p in (RUNS / arm).glob("*/patch.diff")} if RUNS.exists() else [])
    results = []
    for iid in ids:
        c = candidates(iid)
        if len(c) < 2:
            print(f"  skip {iid}: only {len(c)} candidate(s)")
            continue
        print(f"  judging {iid} ({len(c)} candidates, blind)…", flush=True)
        r = judge_instance(iid, c)
        if r:
            results.append(r)

    if not results:
        sys.exit("nothing judged")
    (ROOT / "data" / "quality.json").write_text(json.dumps(results, indent=1))

    # Mean overall per arm.
    agg: dict[str, list[int]] = {}
    for r in results:
        for arm, sc in r["scores_by_arm"].items():
            agg.setdefault(arm, []).append(sc.get("overall", 0))

    print()
    print(f"{'arm':26} {'n':>2} {'mean overall (1-10)':>20}")
    print("-" * 52)
    for arm, xs in sorted(agg.items()):
        print(f"{arm:26} {len(xs):>2} {sum(xs)/len(xs):>20.2f}")
    print("\nJudge was blind to arm identity and had no tools "
          "(could not look up the upstream fix).")


if __name__ == "__main__":
    main()
