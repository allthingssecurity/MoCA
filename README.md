# MoCA: Mixture of Coding Agents

A frontier model does not need to do every part of a coding task. Reading files,
grepping, applying a plan you have already decided on, running tests: that work is
mechanical, and it is where most of the tokens go.

MoCA pairs a frontier main agent (Claude Opus 4.8) with a cheaper helper from a
different vendor (OpenAI `gpt-5.4-mini`, mounted over MCP through Codex). The main
agent delegates the mechanical work and keeps three things for itself: the plan,
the reading of anything ambiguous, and the review of what comes back.

Two properties separate this from "just use a cheaper model":

**Cross-vendor.** The main agent and the helper come from different labs, with
independent context windows and independent prompt caches. Neither one's context
growth charges the other.

**Persistent, pinned helper.** The helper is one long-lived thread reused via
`codex-reply`, not a fresh agent per delegation. A fresh Codex session re-pays
about 8k to 15k tokens of base instructions every single time. A reused thread
pays that once and then reads from cache. That one fact largely decides whether
the pattern saves anything.

The question is not whether this is cheaper. It obviously can be. The question is
whether it is cheaper without getting worse, and specifically whether the main
agent knows what it must not delegate.

This repo answers that against a benchmark we do not control.

---

## Results

**Not available yet.**

The harness is built and verified end to end. The 19x3 evaluation has not
finished, so there is nothing here worth reading. Nothing will be published here
until it has a valid correctness signal, a blind quality signal, and stated error
bars.

When the run completes, `harness/report.py` generates [`RESULTS.md`](RESULTS.md)
from `data/correctness.json`, `data/quality.json` and `data/cost.json`, with
Wilson intervals on resolve rate and a paired bootstrap on cost and quality
deltas. Any difference whose confidence interval spans zero is reported as **not
significant**, not as a point estimate.

---

## Setup

### Prerequisites

| | Why |
|---|---|
| macOS or Linux | Tested on macOS 26, arm64 |
| Docker, running | SWE-bench grades patches in per-instance containers |
| ~60 GB free disk | Eval images plus bare repo mirrors |
| `claude` CLI, logged in | Arms A and B |
| `codex` CLI, logged in | Arm B (helper) and arm C |
| `uv` or `pip` | Python environment |

Check both CLIs are authenticated before starting. A mid-run auth failure wastes
hours.

```bash
claude --version
codex --version && ls ~/.codex/auth.json
docker info > /dev/null && echo "docker ok"
```

> **Codex model availability depends on your auth.** On a ChatGPT-account login,
> only the `gpt-5.x` coding family is reachable. `gpt-4o` returns
> `400: not supported when using Codex with a ChatGPT account`. If you have an
> OpenAI API key configured instead, cheaper non-coding models become available.
> Edit `harness/arms.py` and `harness/pricing.json` accordingly.

### Install

```bash
git clone https://github.com/allthingssecurity/MoCA && cd MoCA
uv venv --python 3.12 .venv
source .venv/bin/activate
uv pip install swebench datasets

python harness/setup_codex_homes.py
```

`setup_codex_homes.py` creates two isolated Codex homes, each pinned to one model.
It copies your `~/.codex/auth.json` into them so they can authenticate. Those
directories are gitignored, and the script refuses to run if they are not. Do not
change that.

### Why the model has to be pinned this way

`codex mcp-server` ignores `-c model=`. Verified:

```
codex exec       -c model="gpt-5.4-mini"   ->  gpt-5.4-mini   (applied)
codex mcp-server -c model="gpt-5.4-mini"   ->  gpt-5.6-sol    (IGNORED)
```

It falls back to your global `~/.codex/config.toml` default. If that default is a
frontier model, and mine was, then the "cheap helper" arm quietly runs a second
expensive model with no error. An isolated `CODEX_HOME` cannot be ignored, and
`verify_arm()` aborts the run if the wrong model ever appears in the logs.

---

## Running it

```bash
source .venv/bin/activate

# 1. Drop benchmark instances that are broken in THIS environment.
#    Grades the real upstream patches. Anything gold cannot resolve is excluded.
#    Slow: it also builds every Docker eval image. About 40 to 60 minutes, once.
python harness/validate_tasks.py --workers 4

# 2. Run the agents. 3 arms x N tasks. This is the expensive step.
python harness/run_arm.py --arm all --instance all

# 3. Score.
python harness/grade.py --arm all     # correctness, official SWE-bench harness
python harness/judge.py               # quality, blind LLM judge
python harness/cost.py                # cost, shadow-priced, both vendors
python harness/report.py              # writes RESULTS.md

# Or chain 2 and 3:
bash harness/pipeline.sh
```

Artefacts:

| Path | What |
|---|---|
| `data/tasks.json` | The selected instances (seeded, reproducible) |
| `data/usable.json` | Which survived gold validation, and why the rest did not |
| `runs/<arm>/<instance>/patch.diff` | The candidate patch |
| `runs/<arm>/<instance>/ledger.json` | Per-run usage, timing, models actually used |
| `data/correctness.json` | Resolved and unresolved, per arm |
| `data/quality.json` | Blind judge scores, per arm |
| `data/cost.json` | Token and shadow-cost breakdown, per run |

---

## Method

### The three arms

| Arm | What runs | What it controls for |
|---|---|---|
| `A_opus_solo` | Claude Code, Opus 4.8 | The baseline you pay for today |
| `B_opus_codex_helper` | Opus 4.8 plus `gpt-5.4-mini` helper | MoCA |
| `C_codex_solo` | Codex, `gpt-5.5` | Checks the result is not an artefact |

Arm C is not decoration. Without it, "the helper was cheaper and just as good" is
indistinguishable from "this task never needed a frontier model at all."

### Task selection

20 instances from SWE-bench Verified, seed `20260714`, stratified by the dataset's
own human difficulty labels:

- 8 x `<15 min fix`, mechanical. The helper's best case.
- 9 x `15 min - 1 hour`, medium.
- 3 x `1-4 hours`, judgment. The helper's worst case.

Capped at 5 tasks per repo. A plain random sample came out 75% Django.

Gold validation then dropped `psf__requests-2317`, leaving **n = 19**.

An all-hard task set would be the wrong test. Delegation is expected to fail on
judgment-heavy work. That is the known failure mode, not a discovery. What is
actually under test is whether the main agent routes correctly, and that needs
both kinds of task to route between. A set of only hard problems measures "can a
cheap model do hard work" (no). A set of only easy problems measures nothing.

### What "cost" means here

**Neither account is billed per token.** Claude Code runs on a subscription
(`service_tier: standard`, 1-hour cache). Codex runs on a ChatGPT plan
(`plan_type: prolite`), metered as percent of a weekly window. There is no invoice
to read.

Every dollar figure here is therefore shadow-priced: tokens from the logs times
published API list rate (`harness/pricing.json`, taken from
`developers.openai.com`, not from aggregator blogs). Tokens are the ground truth.
Dollars are a derived comparison unit, and are labelled that way throughout.

### Metrics

**Correctness.** The official SWE-bench harness. Resolved means every
`FAIL_TO_PASS` test now passes and every `PASS_TO_PASS` test still passes. We
author none of these tests. The second half is what catches a patch that "fixes"
the bug by breaking something else.

**Quality.** A blind LLM judge over the diffs, scoring root cause, scope, fit,
robustness, and would-I-merge. Arm labels are stripped and shuffled per instance.
The judge has no tools and cannot look up the upstream fix. This catches the diff
that goes green and that no maintainer would merge, which is the failure a cheap
delegated model is most likely to produce.

**Cost.** Shadow-priced tokens across both vendors, helper included.

---

## Guards, and why each exists

Each of these was added because the naive version was wrong, and **each one made
MoCA look better than it is.** That is not a coincidence. It is why they abort the
run instead of logging a warning.

| Guard | The bug it prevents |
|---|---|
| Model pin via isolated `CODEX_HOME` | `mcp-server` ignores `-c model=` and silently runs a frontier model as the "cheap" helper |
| `cwd` join on Codex session logs | The helper is an MCP subprocess. Its tokens never reach `claude -p`'s `total_cost_usd`. Reporting that number gives an imaginary saving |
| Disjoint token normalisation | Codex's `input_tokens` includes cached tokens. Anthropic's are disjoint. Naive summing double-counts |
| `.git` destroyed after checkout | `git log --all` otherwise reaches the real fix commit sitting on another branch |
| Web tools denied, network off | Every SWE-bench solution is a public GitHub PR. An agent with search measures retrieval, not skill |
| Gold-patch validation | Some instances cannot be resolved by the real upstream patch here. `psf__requests-2317` needs live httpbin calls that 503 in the container. Keeping it penalises all arms for something no agent could fix |

---

## Limitations

**n = 19 is small.** Agent runs are high variance. Expect wide confidence
intervals. They are reported, not buried.

**Shadow pricing is a model, not money.** See above.

**One helper model, one main model.** The tiered-router idea, routing per task
across several helper tiers, is not tested here. MoCA currently pins one helper
for the whole run.

**Python only.** SWE-bench Verified is entirely Python repos. Nothing here says
anything about MoCA on Go, TypeScript, or Rust.

**Grading runs x86_64 under emulation.** `swebench` 4.1.0 hardcodes the arch and
exposes no flag. Native arm64 images exist and would be faster. We do not use
them, because x86_64 is the arch every published SWE-bench number is produced on,
and arch differences show up exactly where this benchmark lives: float precision
in sympy, numpy and matplotlib.
