"""The three arms under test.

The comparison only means something if the arms differ in exactly one thing:
who does the work. Everything else -- prompt, repo state, tool access, turn
budget, leakage guards -- is held constant across arms.

Arm C is not decoration. Without it, a helper win is indistinguishable from
"this task never needed a frontier model in the first place".
"""

HELPER_MODEL = "gpt-5.4-mini"   # verified reachable on this ChatGPT-account auth
CODEX_SOLO_MODEL = "gpt-5.5"      # gpt-4o is NOT reachable: 400 on ChatGPT-account auth

# The delegation policy. This IS the mechanism under test: the main agent should
# take minimal actions, read only what it must, and keep the plan / ambiguity /
# review for itself. If the main agent just does the work, arm B collapses into
# arm A and MoCA measures nothing.
DELEGATION_POLICY = """\
You have a helper: a cheaper coding agent reachable via the `codex` MCP tool \
(and `codex-reply` to continue an existing helper thread by threadId).

Operate as a delegator, not an implementer. Concretely:

- DELEGATE by default. Bulk file reading, searching, mechanical edits, running \
tests, and applying a plan you have already decided on all go to the helper.
- DO NOT read files yourself unless you genuinely cannot decide without seeing \
the exact text. Ask the helper to read and report back.
- KEEP for yourself: the plan, the interpretation of anything ambiguous in the \
problem statement, and the final review of the helper's diff.
- REUSE the thread. After the first `codex` call you get a threadId. Use \
`codex-reply` with that threadId for every follow-up so the helper keeps its \
context and its cache stays warm. Spawning a fresh helper per step re-pays its \
full base-instruction overhead every time.
- ESCALATE when it is not working. If the helper is going in circles or the \
task turns out to hinge on subtle judgment, take the work back and do it yourself. \
Delegating judgment is how this pattern fails.

The helper shares your working directory. It can edit files directly.
"""

# Held constant across every arm.
COMMON_TASK_PROMPT = """\
You are fixing a real bug in this repository.

<problem_statement>
{problem_statement}
</problem_statement>

Rules:
- Modify the source code so the described issue is fixed.
- Do NOT write new tests and do NOT modify existing tests. The change is graded \
by a hidden test suite you cannot see.
- Do not try to look up the upstream fix. Solve it from the code in front of you.
- Keep the change minimal and in keeping with the surrounding code.

When you are finished, stop. Your uncommitted working-tree diff is the answer.
"""

# Web tools are disabled: SWE-bench solutions are public on GitHub, and an agent
# that can search will simply find the PR. That would measure retrieval, not skill.
BANNED_CLAUDE_TOOLS = ["WebSearch", "WebFetch"]

ARMS = {
    "A_opus_solo": {
        "engine": "claude",
        "model": "opus",
        "helper": None,
        "label": "Opus 4.8 solo (baseline)",
    },
    "B_opus_codex_helper": {
        "engine": "claude",
        "model": "opus",
        "helper": HELPER_MODEL,
        "label": f"Opus 4.8 + {HELPER_MODEL} helper (MoCA)",
    },
    "C_codex_solo": {
        "engine": "codex",
        "model": CODEX_SOLO_MODEL,
        "helper": None,
        "label": f"{CODEX_SOLO_MODEL} solo (control)",
    },
}
