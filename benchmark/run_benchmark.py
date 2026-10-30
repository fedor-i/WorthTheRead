"""Claim-grounding benchmark for Worth Your Read.

Runs every claim-paper pair in gold.jsonl through the real pipeline
(resolve + retraction + fetch + judge) and scores the tool against the gold
labels.

Two tiers of metrics:
  - Keyless (always meaningful): retraction recall, wrong-paper recall,
    citation-resolution rate, full-text retrieval rate. These exercise the
    parts that need no model, so they score even without an API key.
  - Verdict (needs a judge backend — ANTHROPIC_API_KEY or Bedrock): overall
    accuracy, per-class precision/recall/F1, a confusion matrix, and the
    false-SUPPORTED rate (the dangerous error: calling an overstated,
    unsupported, or retracted claim SUPPORTED).

Usage:
    python -m worth_your_read.benchmark.run_benchmark
    python benchmark/run_benchmark.py [path/to/gold.jsonl]

The gold set is a seed (v0): labels are human-reasoned and carry a `rationale`;
extend gold.jsonl to grow coverage.
"""
import asyncio
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))  # repo root on path

from worth_your_read import server  # noqa: E402

LABELS = ["SUPPORTED", "OVERSTATED", "UNSUPPORTED", "INSUFFICIENT"]


def load_gold(path):
    rows = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#"):
                rows.append(json.loads(line))
    return rows


async def run(rows):
    out = []
    for r in rows:
        res = await server._verify(r.get("claim", ""), r.get("reference", ""),
                                   r.get("expected_title", ""))
        out.append((r, res))
        print(f"  ran {r['id']:<26} -> {res['verdict']:<12} "
              f"(retracted={res.get('retracted')}, link_ok={res.get('citation_ok')}, "
              f"full_text={res.get('has_results')})")
    return out


def _prf(pairs, label):
    tp = sum(1 for g, p in pairs if g == label and p == label)
    fp = sum(1 for g, p in pairs if g != label and p == label)
    fn = sum(1 for g, p in pairs if g == label and p != label)
    prec = tp / (tp + fp) if tp + fp else float("nan")
    rec = tp / (tp + fn) if tp + fn else float("nan")
    f1 = (2 * prec * rec / (prec + rec)
          if prec == prec and rec == rec and (prec + rec) else float("nan"))
    support = sum(1 for g, _ in pairs if g == label)
    return prec, rec, f1, support


def _fmt(x):
    return "  n/a" if x != x else f"{x:5.2f}"


def report(results):
    n = len(results)
    print("\n" + "=" * 68)
    print(f"CLAIM-GROUNDING BENCHMARK — {n} pairs")
    print("=" * 68)

    # ---- keyless metrics ----
    gold_retracted = [(g, r) for g, r in results if g.get("retracted")]
    gold_wrong = [(g, r) for g, r in results if g.get("wrong_paper")]
    resolved = sum(1 for _, r in results
                   if r.get("citation_ok") or (r.get("resolved") or {}).get("title"))
    fulltext = sum(1 for _, r in results
                   if r.get("has_results")
                   or (r.get("fetch_source") not in (None, "none", "abstract_only")))

    print("\nKeyless signals (no model needed):")
    if gold_retracted:
        caught = sum(1 for _, r in gold_retracted if r.get("retracted"))
        print(f"  retraction recall     {caught}/{len(gold_retracted)} "
              f"({caught / len(gold_retracted):.0%}) — flagged the retracted papers")
    if gold_wrong:
        caught = sum(1 for _, r in gold_wrong if not r.get("citation_ok"))
        print(f"  wrong-paper recall    {caught}/{len(gold_wrong)} "
              f"({caught / len(gold_wrong):.0%}) — caught the title mismatch")
    print(f"  citation resolved     {resolved}/{n} ({resolved / n:.0%})")
    print(f"  full text retrieved   {fulltext}/{n} ({fulltext / n:.0%}) "
          "(rest fall back to abstract-level)")

    # ---- verdict metrics (need a judge backend) ----
    non_retracted = [(g, r) for g, r in results if not g.get("retracted")]
    backends = {r.get("judge_backend") for _, r in non_retracted}
    has_backend = backends - {"none", "n/a", None}
    print("\nVerdict metrics:")
    if not has_backend:
        print("  — unavailable: no judge backend ran (set ANTHROPIC_API_KEY or "
              "WYR_USE_BEDROCK). Every non-retracted claim came back INSUFFICIENT.")
        print(f"  backends seen: {sorted(b for b in backends if b)}")
        return

    pairs = [(g["gold"], r["verdict"]) for g, r in results]
    acc = sum(1 for gl, pr in pairs if gl == pr) / n
    print(f"  overall accuracy      {acc:.0%} ({sum(1 for gl, pr in pairs if gl == pr)}/{n})")

    print("\n  per-class   precision recall   f1  support")
    for lab in LABELS:
        p, rc, f1, sup = _prf(pairs, lab)
        print(f"  {lab:<12}  {_fmt(p)}  {_fmt(rc)} {_fmt(f1)}   {sup}")

    # false-SUPPORTED: the dangerous error (an exaggerated/false claim waved through)
    risky = [(g, r) for g, r in results
             if g["gold"] in ("OVERSTATED", "UNSUPPORTED") or g.get("retracted")]
    false_sup = sum(1 for _, r in risky if r["verdict"] == "SUPPORTED")
    print(f"\n  FALSE-SUPPORTED rate  {false_sup}/{len(risky)} "
          f"({false_sup / len(risky):.0%}) — overstated/unsupported/retracted "
          "claims wrongly called SUPPORTED (lower is better)")

    # confusion matrix
    print("\n  confusion (rows=gold, cols=predicted):")
    cols = LABELS
    print("              " + "".join(f"{c[:4]:>7}" for c in cols))
    for gl in LABELS:
        row = [sum(1 for g, p in pairs if g == gl and p == c) for c in cols]
        print(f"    {gl:<11}" + "".join(f"{v:>7}" for v in row))


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(_HERE, "gold.jsonl")
    rows = load_gold(path)
    print(f"Running {len(rows)} claim-paper pairs through the pipeline...\n")
    results = asyncio.run(run(rows))
    report(results)


if __name__ == "__main__":
    main()
