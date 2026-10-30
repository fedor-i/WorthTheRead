"""DeSpin adapter — is an abstract's stated conclusion spin?

"Spin" is a conclusion that overstates what the results actually show. That is
exactly our OVERSTATED verdict: judge the stated conclusion (claim) against the
results text (evidence), and read OVERSTATED or UNSUPPORTED as spin, SUPPORTED
as faithful. Our distortion_type (e.g. effect_size_spin) adds the why.

Self-contained seed set of (conclusion, results, is_spin) below; replace ITEMS
with the DeSpin / MiRoR RCT-abstract corpus to run their data. Needs a judge
backend.

Run:
    python -m worth_your_read.benchmark.adapters.despin
"""
import asyncio

from ... import server

# (stated_conclusion, results_text, is_spin)
ITEMS = [
    ("Drug X is an effective treatment for major depression.",
     "The primary outcome, change in HAM-D score at 8 weeks, did not differ significantly between drug X and placebo (p=0.21). A secondary subscale showed a nominal improvement.",
     True),
    ("The intervention improved overall survival in advanced cancer.",
     "Overall survival did not differ between arms (hazard ratio 0.95, 95% CI 0.80-1.13); the trial was stopped early for futility.",
     True),
    ("The treatment was well tolerated and effective.",
     "The primary efficacy endpoint was not met (p=0.30). Serious adverse events occurred in 40% of the treatment group versus 22% of controls.",
     True),
    ("Our results demonstrate that the vaccine prevents infection.",
     "Vaccine efficacy against infection was 12% (95% CI -5% to 28%), which was not statistically significant.",
     True),
    ("Early mobilisation shortened hospital stay.",
     "Median length of stay was similar between groups (5.1 vs 5.3 days, p=0.62).",
     True),
    ("Drug Y reduced HbA1c compared with placebo.",
     "At 24 weeks, drug Y lowered HbA1c by 0.8 percentage points versus placebo (95% CI 0.6-1.0, p<0.001).",
     False),
    ("Exercise improved six-minute walk distance.",
     "The exercise group walked 42 m further than controls at 12 weeks (95% CI 20-64, p<0.001).",
     False),
    ("No significant difference in the primary outcome was found between groups.",
     "The primary outcome did not differ significantly between arms (p=0.48).",
     False),
    ("High biomarker levels were associated with worse prognosis.",
     "High biomarker levels were associated with increased mortality after adjustment (hazard ratio 1.8, 95% CI 1.2-2.6, p=0.002).",
     False),
]
# OVERSTATED or UNSUPPORTED => the conclusion isn't faithfully backed => spin.
SPIN_VERDICTS = {"OVERSTATED", "UNSUPPORTED"}


async def run():
    print(f"DeSpin spin detection — {len(ITEMS)} (conclusion, results) pairs\n")
    scored = []
    for concl, results, gold in ITEMS:
        r = await server._verify(concl, "", evidence_text=results)
        pred = r.get("verdict") in SPIN_VERDICTS
        dt = r.get("distortion_type") or ""
        scored.append((gold, pred))
        tag = f"{r.get('verdict')}" + (f"/{dt}" if dt and dt != "none" else "")
        print(f"  gold_spin={str(gold):<5} pred_spin={str(pred):<5} ({tag:<22}) "
              f"{'ok' if gold == pred else 'X'}  {concl[:40]}")
    acc = sum(1 for g, p in scored if g == p) / len(scored)
    tp = sum(1 for g, p in scored if g and p)
    fp = sum(1 for g, p in scored if not g and p)
    fn = sum(1 for g, p in scored if g and not p)
    prec = tp / (tp + fp) if tp + fp else float("nan")
    rec = tp / (tp + fn) if tp + fn else float("nan")
    print(f"\naccuracy {acc:.0%} ({sum(1 for g, p in scored if g == p)}/{len(scored)})")
    print(f"spin precision {prec:.2f}  recall {rec:.2f}")


if __name__ == "__main__":
    asyncio.run(run())
