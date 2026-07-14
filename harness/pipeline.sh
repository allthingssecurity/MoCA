#!/usr/bin/env bash
# Wait for the agent runs to finish, then score everything and write RESULTS.md.
# Each stage is gated on the previous one succeeding -- a half-finished pipeline
# that still emits a RESULTS.md would be worse than no results at all.
set -uo pipefail
cd "$(dirname "$0")/.."
source .venv/bin/activate

echo "[pipeline] waiting for agent runs…"
while pgrep -f "run_arm.py" > /dev/null; do sleep 60; done

n=$(find runs -name ledger.json | wc -l | tr -d ' ')
echo "[pipeline] agent runs finished: $n ledgers"
if [ "$n" -lt 3 ]; then
  echo "[pipeline] ABORT: too few runs to score"; exit 1
fi

echo
echo "[pipeline] === CORRECTNESS (official SWE-bench, x86_64 under emulation, slow) ==="
python harness/grade.py --arm all --workers 4 || { echo "[pipeline] grading FAILED"; exit 1; }

echo
echo "[pipeline] === QUALITY (blind judge) ==="
python harness/judge.py || echo "[pipeline] WARNING: judging failed; RESULTS.md will omit quality"

echo
echo "[pipeline] === COST (shadow-priced, both vendors) ==="
python harness/cost.py || { echo "[pipeline] cost accounting FAILED"; exit 1; }

echo
echo "[pipeline] === REPORT ==="
python harness/report.py || { echo "[pipeline] report FAILED"; exit 1; }

echo
echo "[pipeline] DONE, RESULTS.md written."
