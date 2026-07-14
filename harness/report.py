#!/usr/bin/env python3
"""Generate RESULTS.md from the artefacts. Error bars are mandatory, not optional.

n=20 is a small sample and agent runs are high-variance. A bare "MoCA was 27%
cheaper" from 20 tasks is not a finding, it is an anecdote with a decimal point.
So:

  - resolve rate    -> Wilson score interval (correct for small-n proportions;
                       normal approximation is badly wrong near 0% and 100%)
  - cost / quality  -> paired bootstrap over instances, because the arms are run
                       on the SAME tasks. Pairing removes between-task variance,
                       which is by far the largest noise source here.

If an interval spans zero, we say so plainly instead of quoting the point estimate.
"""
from __future__ import annotations

import json
import math
import random
import statistics as stats
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
BOOTSTRAP = 10_000
SEED = 20260714


def load(name):
    p = DATA / name
    return json.loads(p.read_text()) if p.exists() else None


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for a proportion. Handles k=0 and k=n sanely."""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z**2 / n
    centre = (p + z**2 / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z**2 / (4 * n**2)) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


def paired_bootstrap(pairs: list[tuple[float, float]], z: float = 95) -> tuple:
    """95% CI on the mean paired difference (b - a). Pairs share the same task."""
    if not pairs:
        return (0.0, 0.0, 0.0)
    rng = random.Random(SEED)
    diffs = [b - a for a, b in pairs]
    point = stats.fmean(diffs)
    n = len(diffs)
    means = []
    for _ in range(BOOTSTRAP):
        means.append(stats.fmean([diffs[rng.randrange(n)] for _ in range(n)]))
    means.sort()
    lo = means[int(0.025 * BOOTSTRAP)]
    hi = means[int(0.975 * BOOTSTRAP)]
    return (point, lo, hi)


def main() -> None:
    correctness = load("correctness.json") or []
    quality = load("quality.json") or []
    cost = load("cost.json") or []
    usable = load("usable.json") or {}

    if not cost:
        raise SystemExit("no runs yet -- nothing to report")

    arms = sorted({r["arm"] for r in cost})
    baseline = "A_opus_solo"

    cost_by = {a: {r["instance_id"]: r["shadow_usd"] for r in cost if r["arm"] == a}
               for a in arms}
    wall_by = {a: {r["instance_id"]: r["wall_s"] for r in cost if r["arm"] == a}
               for a in arms}
    qual_by = {a: {q["instance_id"]: q["scores_by_arm"][a]["overall"]
                   for q in quality if a in q["scores_by_arm"]} for a in arms}
    res_by = {c["arm"]: set(c.get("resolved_ids", [])) for c in correctness
              if not c.get("error")}
    sub_by = {c["arm"]: c.get("submitted", 0) for c in correctness
              if not c.get("error")}

    L = []
    L.append("# MoCA — Results\n")
    L.append(f"Generated from `data/*.json`. Seed `{SEED}`. "
             f"Bootstrap resamples: {BOOTSTRAP:,}.\n")

    n_us = len(usable.get("usable", []))
    n_br = len(usable.get("broken", []))
    if usable:
        L.append(f"**Instances:** {n_us} usable, {n_br} dropped by gold validation "
                 f"(the real upstream patch could not resolve them in this "
                 f"environment, so no agent could either).\n")
        if n_br:
            L.append("Dropped: " + ", ".join(f"`{b}`" for b in usable["broken"]) + "\n")

    # ---- correctness -------------------------------------------------------
    if res_by:
        L.append("\n## Correctness (official SWE-bench harness)\n")
        L.append("Resolved = every `FAIL_TO_PASS` test passes **and** every "
                 "`PASS_TO_PASS` test still passes. We authored none of these tests.\n")
        L.append("| Arm | Resolved | Rate | 95% CI (Wilson) |")
        L.append("|---|---|---|---|")
        for a in arms:
            k, n = len(res_by.get(a, ())), sub_by.get(a, 0)
            if not n:
                continue
            lo, hi = wilson(k, n)
            L.append(f"| `{a}` | {k}/{n} | {k/n:.0%} | {lo:.0%} – {hi:.0%} |")
        L.append("\n> With n of this size the intervals overlap heavily. Treat any "
                 "correctness difference between arms as **unresolved** unless the "
                 "intervals are disjoint.\n")

    # ---- cost --------------------------------------------------------------
    L.append("\n## Cost (shadow-priced — see README; neither account is billed per token)\n")
    L.append("| Arm | Mean $/task | vs baseline | 95% CI on the difference |")
    L.append("|---|---|---|---|")
    for a in arms:
        xs = list(cost_by[a].values())
        if not xs:
            continue
        mean = stats.fmean(xs)
        if a == baseline:
            L.append(f"| `{a}` | ${mean:.4f} | — (baseline) | — |")
            continue
        shared = sorted(set(cost_by[baseline]) & set(cost_by[a]))
        pairs = [(cost_by[baseline][i], cost_by[a][i]) for i in shared]
        pt, lo, hi = paired_bootstrap(pairs)
        base_mean = stats.fmean([p[0] for p in pairs]) or 1
        verdict = ("**not significant** (CI spans $0)" if lo < 0 < hi
                   else f"**{pt/base_mean:+.0%}**")
        L.append(f"| `{a}` | ${mean:.4f} | {verdict} | ${lo:+.4f} – ${hi:+.4f} |")
    L.append(f"\n> Paired over the {len(set(cost_by[baseline]))} instances every arm "
             "attempted. Pairing removes between-task variance, which dominates here.\n")

    # ---- quality -----------------------------------------------------------
    if any(qual_by.values()):
        L.append("\n## Quality (blind LLM judge, 1–10 'would I merge this')\n")
        L.append("Arm labels stripped and shuffled per instance; judge had no tools "
                 "and could not look up the upstream fix.\n")
        L.append("| Arm | Mean overall | vs baseline | 95% CI on the difference |")
        L.append("|---|---|---|---|")
        for a in arms:
            xs = list(qual_by[a].values())
            if not xs:
                continue
            mean = stats.fmean(xs)
            if a == baseline:
                L.append(f"| `{a}` | {mean:.2f} | — (baseline) | — |")
                continue
            shared = sorted(set(qual_by[baseline]) & set(qual_by[a]))
            pairs = [(qual_by[baseline][i], qual_by[a][i]) for i in shared]
            pt, lo, hi = paired_bootstrap(pairs)
            verdict = ("**not significant** (CI spans 0)" if lo < 0 < hi
                       else f"**{pt:+.2f} pts**")
            L.append(f"| `{a}` | {mean:.2f} | {verdict} | {lo:+.2f} – {hi:+.2f} |")
        L.append("\n> This is the axis that matters most. Cheap delegation that "
                 "passes tests but drops quality is not a saving, it is a deferred "
                 "cost paid in code review.\n")

    # ---- latency -----------------------------------------------------------
    L.append("\n## Latency\n")
    L.append("| Arm | Mean wall-clock |")
    L.append("|---|---|")
    for a in arms:
        xs = list(wall_by[a].values())
        if xs:
            L.append(f"| `{a}` | {stats.fmean(xs):.0f}s |")
    L.append("\n> Delegation adds round-trips. If MoCA is cheaper but materially "
             "slower, that is a trade, not a free win — state it.\n")

    # ---- cost by difficulty: the routing question --------------------------
    diff_of = {r["instance_id"]: r["difficulty"] for r in cost}
    if "B_opus_codex_sidekick" in cost_by:
        L.append("\n## Does the router know what NOT to delegate?\n")
        L.append("The whole thesis. Delegation should win on mechanical work and "
                 "should back off on judgment-heavy work.\n")
        L.append("| Difficulty | n | Δ cost vs solo | Δ quality vs solo |")
        L.append("|---|---|---|---|")
        for d in ["<15 min fix", "15 min - 1 hour", "1-4 hours"]:
            ids = [i for i in cost_by[baseline] if diff_of.get(i) == d
                   and i in cost_by["B_opus_codex_sidekick"]]
            if not ids:
                continue
            cp = [(cost_by[baseline][i], cost_by["B_opus_codex_sidekick"][i]) for i in ids]
            c_pt, c_lo, c_hi = paired_bootstrap(cp)
            base = stats.fmean([p[0] for p in cp]) or 1
            qids = [i for i in ids if i in qual_by.get(baseline, {})
                    and i in qual_by.get("B_opus_codex_sidekick", {})]
            if qids:
                qp = [(qual_by[baseline][i], qual_by["B_opus_codex_sidekick"][i])
                      for i in qids]
                q_pt, q_lo, q_hi = paired_bootstrap(qp)
                qtxt = f"{q_pt:+.2f} ({q_lo:+.2f}–{q_hi:+.2f})"
            else:
                qtxt = "—"
            L.append(f"| {d} | {len(ids)} | {c_pt/base:+.0%} "
                     f"(${c_lo:+.3f}–${c_hi:+.3f}) | {qtxt} |")
        L.append("\n> If quality holds on the easy tiers and collapses on `1-4 hours`, "
                 "the pattern works but the router does not — it is delegating "
                 "judgment it should have kept.\n")

    (ROOT / "RESULTS.md").write_text("\n".join(L) + "\n")
    print("wrote RESULTS.md")
    print("\n".join(L[:40]))


if __name__ == "__main__":
    main()
