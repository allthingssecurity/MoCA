# MoCA — Mixture of Coding Agents

**A frontier model does not need to do every part of a coding task.** Reading
files, grepping, applying a plan you have already decided on, running tests — that
work is mechanical, and it is where most of the tokens go.

MoCA pairs a **frontier main agent** (Claude Opus 4.8) with a **cheaper sidekick
from a different vendor** (OpenAI `gpt-5.4-mini`, mounted over MCP through Codex).
The main agent delegates the mechanical work and keeps three things for itself:
the plan, the interpretation of anything ambiguous, and the review of what comes
back.

Two properties distinguish this from "just use a cheaper model":

- **Cross-vendor.** Main agent and sidekick come from different labs, with
  independent context windows and independent prompt caches. Neither one's context
  growth charges the other.
- **Persistent, pinned sidekick.** The sidekick is one long-lived thread reused via
  `codex-reply`, not a fresh agent per delegation. A *fresh* Codex session re-pays
  ~8–15k tokens of base instructions **every single time**; a reused thread pays it
  once, then reads from cache. That one fact largely decides whether the pattern
  saves anything at all.

The interesting question is not whether this is cheaper — it obviously can be. It
is whether it is cheaper **without quietly getting worse**, and specifically
whether the main agent knows what it must *not* delegate.

This repo answers that against a benchmark we do not control.

---

## Results

**Not yet available.**

The harness is built and verified end-to-end. The 20×3 evaluation has not been
run, so there is nothing here worth reading yet — and nothing will be published
here until it has a **valid correctness signal, a blind quality signal, and stated
error bars**.

When the run completes, `harness/report.py` generates [`RESULTS.md`](RESULTS.md)
from `data/correctness.json`, `data/quality.json` and `data/cost.json`, with Wilson
intervals on resolve rate and a paired bootstrap on cost and quality deltas. Any
difference whose confidence interval spans zero will be reported as **not
significant** rather than quoted as a point estimate.

---

## Setup

### Prerequisites

| | Why |
|---|---|
| macOS or Linux | Tested on macOS 26 / arm64 |
| **Docker**, running | SWE-bench grades patches in per-instance containers |
| ~60 GB free disk | Eval images + bare repo mirrors |
| **`claude`** CLI, logged in | Arms A and B |
| **`codex`** CLI, logged in | Arm B (sidekick) and arm C |
| `uv` (or `pip`) | Python env |

Check both CLIs are authenticated before you start — a mid-run auth failure wastes
hours:

```bash
claude --version
codex --version && ls ~/.codex/auth.json
docker info > /dev/null && echo "docker ok"
```

> **Codex model availability depends on your auth.** On a **ChatGPT-account**
> login, only the `gpt-5.x` coding family is reachable — `gpt-4o` returns
> `400: not supported when using Codex with a ChatGPT account`. If you have an
> OpenAI **API key** configured instead, cheaper non-coding models become
> available; edit `harness/arms.py` and `harness/pricing.json` accordingly.

### Install

```bash
git clone <this repo> && cd moca
uv venv --python 3.12 .venv
source .venv/bin/activate
uv pip install swebench datasets

python harness/setup_codex_homes.py
```

`setup_codex_homes.py` creates two isolated Codex homes, each pinned to one model.
**It copies your `~/.codex/auth.json` into them** so they can authenticate. Those
directories are gitignored and the script refuses to run if they aren't — do not
change that.

### Why the model must be pinned this way

`codex mcp-server` **silently ignores `-c model=`.** Verified:

```
codex exec       -c model="gpt-5.4-mini"   ->  gpt-5.4-mini   (applied)
codex mcp-server -c model="gpt-5.4-mini"   ->  gpt-5.6-sol    (IGNORED)
```

It falls back to your global `~/.codex/config.toml` default. If that default is a
frontier model — mine was — the "cheap sidekick" arm quietly runs a *second
expensive model*, with no error. An isolated `CODEX_HOME` cannot be ignored, and
`verify_arm()` aborts the run if the wrong model ever appears in the logs.

---

## Running it

```bash
source .venv/bin/activate

# 1. Drop benchmark instances that are broken in THIS environment.
#    (Grades the real upstream patches; anything gold cannot resolve is excluded.)
#    Slow: it also builds every Docker eval image. ~40-60 min, once.
python harness/validate_tasks.py --workers 4

# 2. Run the agents. 3 arms x N tasks. This is the expensive step.
python harness/run_arm.py --arm all --instance all
#    …or pilot first:
python harness/run_arm.py --arm all --instance all --limit 3

# 3. Score.
python harness/grade.py --arm all     # correctness, official SWE-bench harness
python harness/judge.py               # quality, blind LLM judge
python harness/cost.py                # shadow-priced cost, both vendors
```

Artefacts land in:

| Path | What |
|---|---|
| `data/tasks.json` | The 20 selected instances (seeded, reproducible) |
| `data/usable.json` | Which survived gold validation, and why the rest didn't |
| `runs/<arm>/<instance>/patch.diff` | The candidate patch |
| `runs/<arm>/<instance>/ledger.json` | Per-run usage, timing, models actually used |
| `data/correctness.json` | Resolved / unresolved per arm |
| `data/quality.json` | Blind judge scores per arm |
| `data/cost.json` | Token + shadow-cost breakdown per run |

---

## Method

### The three arms

| Arm | What runs | What it controls for |
|---|---|---|
| `A_opus_solo` | Claude Code, Opus 4.8 | The baseline you pay for today |
| `B_opus_codex_sidekick` | Opus 4.8 + `gpt-5.4-mini` sidekick | MoCA |
| `C_codex_solo` | Codex, `gpt-5.5` | **The arm that keeps us honest** |

Arm C is not decoration. Without it, "the sidekick was cheaper and just as good"
is indistinguishable from "this task never needed a frontier model at all."

### Task selection

20 instances from **SWE-bench Verified**, seed `20260714`, stratified by the
dataset's own human difficulty labels:

- 8 × `<15 min fix` — mechanical. The sidekick's best case.
- 9 × `15 min – 1 hour` — medium.
- 3 × `1–4 hours` — judgment. The sidekick's **worst** case.

Capped at 5 tasks per repo (a naive random sample came out 75% Django).

An all-hard task set would be the *wrong* test. Delegation is expected to fail on
judgment-heavy work — that is the known failure mode, not a surprise. What is
actually under test is whether the main agent **routes correctly**, which requires
both kinds of task to route between.

### What "cost" means — read this before quoting a number

**Neither account is billed per token.** Claude Code runs on a subscription
(`service_tier: standard`, 1-hour cache). Codex runs on a ChatGPT plan
(`plan_type: prolite`), metered as *percent of a weekly window*. **There is no
invoice to read.**

Every dollar figure here is **shadow-priced**: tokens from the logs × published
API list rate (`harness/pricing.json`, verified against `developers.openai.com`,
not aggregator blogs). Tokens are the ground truth; dollars are a derived
comparison unit, labelled as such throughout.

### Metrics

- **Correctness** — the official SWE-bench harness. Resolved = every `FAIL_TO_PASS`
  test now passes **and** every `PASS_TO_PASS` test still passes. We author none of
  these tests. The second half is what catches a patch that "fixes" the bug by
  breaking something else.
- **Quality** — a blind LLM judge over the diffs, scoring root cause / scope / fit
  / robustness / would-I-merge. Arm labels are stripped and shuffled per instance;
  the judge has **no tools** and cannot look up the upstream fix. This catches the
  diff that goes green and that no maintainer would merge — the failure a cheap
  delegated model is most likely to produce.
- **Cost** — shadow-priced tokens across both vendors, sidekick included.

---

## Guards, and why each exists

Every one of these was added because the naive version was **actively wrong**, and
**every one made MoCA look better than it is.** That is not a coincidence — it is
why they abort the run instead of logging a warning.

| Guard | The bug it prevents |
|---|---|
| **Model pin** via isolated `CODEX_HOME` | `mcp-server` ignores `-c model=` and silently runs a frontier model as the "cheap" sidekick |
| **`cwd`-join on Codex session logs** | The sidekick is an MCP *subprocess*; its tokens never reach `claude -p`'s `total_cost_usd`. Reporting that gives an imaginary saving |
| **Disjoint token normalisation** | Codex's `input_tokens` *includes* cached tokens; Anthropic's are disjoint. Naive summing double-counts |
| **`.git` destroyed post-checkout** | `git log --all` reaches the real fix commit sitting on another branch |
| **Web tools denied / network off** | Every SWE-bench solution is a public GitHub PR. An agent with search measures retrieval, not skill |
| **Gold-patch validation** | Some instances can't be resolved *by the real upstream patch* here (`psf__requests-2317` needs live httpbin calls that 503). Keeping them penalises all arms for something no agent could fix |

---

## Limitations

- **n = 20 is small.** Agent runs are high-variance; expect wide confidence
  intervals. They will be reported, not buried.
- **Shadow pricing is a model, not money.** See above.
- **One sidekick model, one main model.** The tiered-router idea (route *per task*
  across several sidekick tiers) is not yet tested — MoCA currently pins one
  sidekick for the whole run.
- **Python-only.** SWE-bench Verified is entirely Python repos. Nothing here says
  anything about MoCA on Go, TypeScript, or Rust.
