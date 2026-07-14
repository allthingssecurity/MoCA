#!/usr/bin/env python3
"""Cost accounting across both harnesses.

The whole point of this file is one number that is easy to get wrong:

  For arm B, the helper runs as an MCP *subprocess* of `claude`. Its tokens do
  NOT appear in `claude -p`'s cost. If you report claude's total_cost_usd for arm
  B you will see a large, entirely fictional saving. We recover the helper's
  real usage by joining Codex session logs on `cwd`, which uniquely identifies the
  run that spawned them.

TOKENS are ground truth. Dollars are shadow-priced at list rates and are labelled
as such everywhere, because neither account is billed per token.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HARNESS = Path(__file__).resolve().parent
RUNS = ROOT / "runs"
# Only the experiment's own isolated Codex homes. Deliberately NOT ~/.codex --
# the user's day-to-day Codex work must never be billed to a run.
CODEX_SESSION_DIRS = [
    HARNESS / "codex_home_helper" / "sessions",
    HARNESS / "codex_home_solo" / "sessions",
]
PRICING = json.loads((Path(__file__).parent / "pricing.json").read_text())
RATES = PRICING["usd_per_mtok"]
MULT = PRICING["cache_multipliers"]


def _index_codex_by_cwd() -> dict[str, list[dict]]:
    """Map repo cwd -> the codex sessions launched in it, with final token totals."""
    idx: dict[str, list[dict]] = {}
    for f in [p for d in CODEX_SESSION_DIRS if d.exists()
              for p in d.rglob("rollout-*.jsonl")]:
        cwd, model, totals = None, None, None
        try:
            for line in f.open():
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = d.get("type")
                if t == "session_meta":
                    cwd = d.get("payload", {}).get("cwd")
                elif t == "turn_context":
                    model = d.get("payload", {}).get("model") or model
                elif t == "event_msg" and d.get("payload", {}).get("type") == "token_count":
                    info = d["payload"].get("info") or {}
                    # cumulative -- last one in the file is the session total
                    totals = info.get("total_token_usage") or totals
        except OSError:
            continue
        if cwd and totals:
            idx.setdefault(cwd, []).append(
                {"file": f.name, "model": model, "usage": totals})
    return idx


def _codex_tokens(u: dict) -> dict:
    """Normalise Codex usage to Anthropic's DISJOINT field semantics.

    Codex reports input_tokens INCLUSIVE of cached_input_tokens; Anthropic reports
    inputTokens and cacheReadInputTokens as disjoint. Storing Codex's raw numbers
    next to Anthropic's makes any `input + cache_read` sum double-count the cached
    portion -- which it did, overstating helper tokens until this was fixed.
    """
    cached = u.get("cached_input_tokens", 0)
    return {
        "input": max(u.get("input_tokens", 0) - cached, 0),   # fresh only
        "cache_read": cached,
        "cache_write": 0,                                     # no such concept
        "output": u.get("output_tokens", 0) + u.get("reasoning_output_tokens", 0),
    }


def _price_anthropic(model: str, mu: dict) -> float | None:
    """Recompute from tokens. Used only to cross-check Claude's own costUSD."""
    r = RATES.get(model)
    if not r or r["input"] is None:
        return None
    inp = r["input"] / 1e6
    out = r["output"] / 1e6
    return (
        mu.get("inputTokens", 0) * inp
        + mu.get("outputTokens", 0) * out
        + mu.get("cacheReadInputTokens", 0) * inp * MULT["anthropic_cache_read"]
        + mu.get("cacheCreationInputTokens", 0) * inp * MULT["anthropic_cache_write_1h"]
    )


def _price_openai(model: str, u: dict) -> float | None:
    r = RATES.get(model)
    if not r or r.get("input") is None:
        return None                      # unknown rate -> refuse to guess
    inp = r["input"] / 1e6
    out = r["output"] / 1e6
    # OpenAI publishes an explicit cached-input rate per model; use it rather than
    # assuming a multiplier.
    cached_rate = r.get("cached_input")
    cin = (cached_rate / 1e6) if cached_rate is not None else inp * MULT["openai_cached_input"]

    cached = u.get("cached_input_tokens", 0)
    fresh = max(u.get("input_tokens", 0) - cached, 0)   # input_tokens is inclusive of cached
    # reasoning tokens bill as output on OpenAI models
    outputs = u.get("output_tokens", 0) + u.get("reasoning_output_tokens", 0)
    return fresh * inp + cached * cin + outputs * out


def account_run(ledger_path: Path, codex_idx: dict) -> dict:
    led = json.loads(ledger_path.read_text())
    repo_cwd = str(ledger_path.parent / "repo")

    tokens: dict[str, dict] = {}
    usd, unpriced = 0.0, []

    # --- Claude side (arms A and B) -------------------------------------------
    for model, mu in (led.get("claude_model_usage") or {}).items():
        tokens[model] = {
            "input": mu.get("inputTokens", 0),
            "output": mu.get("outputTokens", 0),
            "cache_read": mu.get("cacheReadInputTokens", 0),
            "cache_write": mu.get("cacheCreationInputTokens", 0),
        }
        # Prefer the vendor's own number; recompute only as a sanity check.
        usd += mu.get("costUSD") or (_price_anthropic(model, mu) or 0.0)

    # --- Codex side, arm C: usage came straight off the exec stream ------------
    if led.get("codex_usage"):
        m = led.get("codex_model", "unknown")
        u = led["codex_usage"]
        tokens[m] = _codex_tokens(u)
        p = _price_openai(m, u)
        usd += p if p is not None else 0.0
        if p is None:
            unpriced.append(m)

    # --- Codex side, arm B: THE BIT THAT IS EASY TO MISS -----------------------
    # Helper tokens are invisible to claude -p. Recover them via cwd join.
    helper_sessions = codex_idx.get(repo_cwd, [])
    for s in helper_sessions:
        m = s["model"] or "gpt-5.4-mini"
        u = s["usage"]
        agg = tokens.setdefault(
            m, {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0})
        for k, v in _codex_tokens(u).items():
            agg[k] += v
        p = _price_openai(m, u)
        usd += p if p is not None else 0.0
        if p is None:
            unpriced.append(m)

    return {
        "arm": led["arm"],
        "instance_id": led["instance_id"],
        "difficulty": led["difficulty"],
        "ok": led["ok"],
        "empty_patch": led["empty_patch"],
        "wall_s": round(led.get("wall_s", 0), 1),
        "tokens_by_model": tokens,
        "helper_sessions": len(helper_sessions),
        "shadow_usd": round(usd, 4),
        "unpriced_models": sorted(set(unpriced)),
        "claude_reported_usd": led.get("claude_cost_usd"),
    }


def main():
    codex_idx = _index_codex_by_cwd()
    rows = [account_run(p, codex_idx) for p in sorted(RUNS.rglob("ledger.json"))]
    if not rows:
        sys.exit("no runs yet")

    (ROOT / "data" / "cost.json").write_text(json.dumps(rows, indent=1))

    unpriced = sorted({m for r in rows for m in r["unpriced_models"]})

    print(f"{'arm':24} {'instance':32} {'sk':>3} {'tokens(in/out)':>18} {'shadow $':>9}")
    print("-" * 92)
    for r in rows:
        tin = sum(v["input"] + v["cache_read"] + v["cache_write"]
                  for v in r["tokens_by_model"].values())
        tout = sum(v["output"] for v in r["tokens_by_model"].values())
        print(f"{r['arm']:24} {r['instance_id']:32} {r['helper_sessions']:>3} "
              f"{tin:>8,}/{tout:<8,} {r['shadow_usd']:>9.4f}")

    print()
    by_arm: dict[str, list[dict]] = {}
    for r in rows:
        by_arm.setdefault(r["arm"], []).append(r)
    print(f"{'arm':24} {'n':>2} {'mean shadow $':>14} {'mean wall':>10}")
    print("-" * 56)
    for arm, rs in sorted(by_arm.items()):
        n = len(rs)
        print(f"{arm:24} {n:>2} {sum(x['shadow_usd'] for x in rs)/n:>14.4f} "
              f"{sum(x['wall_s'] for x in rs)/n:>9.0f}s")

    if unpriced:
        print()
        print("!! INCOMPLETE COST MODEL. No list price supplied for: "
              + ", ".join(unpriced))
        print("!! Those models contributed TOKENS but $0.00 to the totals above,")
        print("!! so any arm that used them is understated. Fill in")
        print("!! harness/pricing.json before quoting a cost delta.")


if __name__ == "__main__":
    main()
