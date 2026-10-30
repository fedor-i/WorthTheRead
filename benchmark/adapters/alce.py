"""ALCE adapter — is a statement actually supported by its cited passage?

ALCE (Gao et al., EMNLP 2023) scores whether a generated answer's citations
support what it wrote. The per-citation check is our judge on supplied text:
feed (statement, cited passage) via verify_claim's evidence_text and read
SUPPORTED as "citation supports the statement". Binary: supported vs not
(OVERSTATED / UNSUPPORTED / INSUFFICIENT all mean the passage does not back the
statement as written).

Self-contained seed set below; replace ITEMS with ALCE's (statement, passage,
gold) records to run their data. Needs a judge backend.

Run:
    python -m worth_your_read.benchmark.adapters.alce
"""
import asyncio

from ... import server

# (statement, cited_passage, gold_supported)
ITEMS = [
    ("Regular physical activity reduces the risk of type 2 diabetes.",
     "In a prospective cohort, participants in the highest physical-activity quartile had a 30% lower incidence of type 2 diabetes than the least active.",
     True),
    ("CRISPR-Cas9 can introduce targeted double-strand breaks in DNA.",
     "Cas9 guided by a single RNA generated site-specific double-strand breaks at the targeted genomic locus.",
     True),
    ("Smoking substantially increases lung cancer risk.",
     "Current smokers had a roughly 15-fold higher risk of lung cancer compared with never-smokers.",
     True),
    ("Global average surface temperature has risen since pre-industrial times.",
     "Global mean surface temperature has increased by approximately 1.1 degrees C relative to the 1850-1900 baseline.",
     True),
    ("Vitamin C supplementation prevents the common cold in the general population.",
     "Regular vitamin C supplementation did not reduce the incidence of colds in the general community, though it modestly shortened their duration.",
     False),
    ("This monoclonal antibody cures Alzheimer's disease.",
     "The antibody reduced amyloid plaques and modestly slowed cognitive decline over 18 months, with no effect on reversing established dementia.",
     False),
    ("Habitual coffee consumption causes hypertension.",
     "Habitual coffee intake was not associated with incident hypertension in the pooled analysis of prospective cohorts.",
     False),
    ("The Mediterranean diet lowers cardiovascular mortality.",
     "The study examined the effect of sleep duration on cognitive performance in older adults.",
     False),
]


async def run():
    print(f"ALCE citation-support — {len(ITEMS)} (statement, passage) pairs\n")
    scored = []
    for stmt, passage, gold in ITEMS:
        r = await server._verify(stmt, "", evidence_text=passage)
        pred = (r.get("verdict") == "SUPPORTED")
        scored.append((gold, pred))
        print(f"  gold={str(gold):<5} pred={str(pred):<5} ({r.get('verdict'):<12}) "
              f"{'ok' if gold == pred else 'X'}  {stmt[:46]}")
    acc = sum(1 for g, p in scored if g == p) / len(scored)
    tp = sum(1 for g, p in scored if g and p)
    fp = sum(1 for g, p in scored if not g and p)
    fn = sum(1 for g, p in scored if g and not p)
    prec = tp / (tp + fp) if tp + fp else float("nan")
    rec = tp / (tp + fn) if tp + fn else float("nan")
    print(f"\naccuracy {acc:.0%} ({sum(1 for g, p in scored if g == p)}/{len(scored)})")
    print(f"citation-support precision {prec:.2f}  recall {rec:.2f}")


if __name__ == "__main__":
    asyncio.run(run())
