"""Determinism harness — measure how stable the judge's verdicts are.

Runs each gold claim N times and reports, per claim and overall, how often the
verdict equals that claim's most common verdict (agreement), plus the flip rate
(fraction of runs that deviate). With temperature=0 and the per-run fetch cache,
agreement should be near 1.0; this turns "seems stable now" into a number.

Run:
    python -m worth_your_read.benchmark.stability          # N=3
    RUNS=5 python -m worth_your_read.benchmark.stability
"""
import asyncio
import os
import sys
from collections import Counter

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))

from worth_your_read import server  # noqa: E402
from worth_your_read import fetch as _fetch  # noqa: E402
from worth_your_read.benchmark.run_benchmark import load_gold  # noqa: E402

RUNS = int(os.environ.get("RUNS", "3"))


async def main():
    rows = load_gold(os.path.join(_HERE, "gold.jsonl"))
    print(f"Stability — {len(rows)} claims x {RUNS} runs\n")
    agreements, total_flips, total_runs = [], 0, 0
    backend_ok = False
    for r in rows:
        _fetch.clear_cache()  # fresh cache per claim; identical text within its runs
        verds = []
        for _ in range(RUNS):
            res = await server._verify(r.get("claim", ""), r.get("reference", ""))
            verds.append(res.get("verdict"))
            if res.get("judge_backend") not in ("none", "n/a", None):
                backend_ok = True
        mode, n = Counter(verds).most_common(1)[0]
        agree = n / len(verds)
        flips = len(verds) - n
        agreements.append(agree)
        total_flips += flips
        total_runs += len(verds)
        mark = "" if flips == 0 else f"  <-- {flips} flip(s): {verds}"
        print(f"  {r['id']:<30} {mode:<12} agree={agree:.0%}{mark}")
    if not backend_ok:
        print("\n(no judge backend ran — set ANTHROPIC_API_KEY or Bedrock to measure verdict stability)")
        return
    print(f"\nmean agreement   {sum(agreements) / len(agreements):.1%}")
    print(f"flip rate        {total_flips}/{total_runs} "
          f"({total_flips / total_runs:.1%}) of all runs deviated from the claim's mode")
    print(f"fully-stable     {sum(1 for a in agreements if a == 1.0)}/{len(agreements)} claims")


if __name__ == "__main__":
    asyncio.run(main())
