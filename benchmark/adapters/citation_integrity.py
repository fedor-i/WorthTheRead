"""Citation-integrity adapter — the CiteAudit / CiteCheck dimension.

Those benchmarks ask: is a reference real, or fabricated/corrupted? That's our
resolve() step (does the DOI resolve to a real record, does the title match, is
it retracted) — no model needed. We score detection of fabricated references on
a small self-contained set (real DOIs vs valid-format-but-nonexistent ones, plus
a couple of retractions). Their published datasets can be dropped in by
replacing ITEMS with their records; the scoring is the same.

Run (keyless):
    python -m worth_your_read.benchmark.adapters.citation_integrity
"""
from ... import resolve as _resolve

# (doi, gold)  gold in {REAL, FABRICATED, RETRACTED}
ITEMS = [
    # real, resolvable papers
    ("10.3389/fpls.2016.00295", "REAL"),
    ("10.1038/s41586-021-03819-2", "REAL"),
    ("10.1016/j.bbabio.2011.04.012", "REAL"),
    ("10.1021/bi962467l", "REAL"),
    ("10.1038/s41467-019-13056-x", "REAL"),
    ("10.1126/science.1260419", "REAL"),
    ("10.1073/pnas.1832753100", "REAL"),
    ("10.1016/j.cell.2011.02.013", "REAL"),
    # valid-format but non-existent (hallucination-style)
    ("10.1038/nature99999", "FABRICATED"),
    ("10.1016/j.cell.2099.12.345", "FABRICATED"),
    ("10.1126/science.0000000", "FABRICATED"),
    ("10.1073/pnas.1234567890", "FABRICATED"),
    ("10.1038/s41586-021-99999-9", "FABRICATED"),
    ("10.9999/nonexistent.0001", "FABRICATED"),
    ("10.1016/j.fakejournal.2088.100001", "FABRICATED"),
    ("10.1101/2099.01.01.000000", "FABRICATED"),
    # retracted (real but should be flagged, not recommended)
    ("10.1016/S0140-6736(97)11096-0", "RETRACTED"),
    ("10.1038/nature12968", "RETRACTED"),
]


def predict(doi):
    r = _resolve.resolve(doi)
    if r.get("retracted"):
        return "RETRACTED"
    if r.get("resolved") and r.get("citation_ok"):
        return "REAL"
    return "FABRICATED"


def main():
    print(f"Citation-integrity (CiteAudit/CiteCheck dimension) — {len(ITEMS)} refs, keyless\n")
    scored = []
    for doi, gold in ITEMS:
        pred = predict(doi)
        scored.append((gold, pred))
        print(f"  gold={gold:<11} pred={pred:<11} {'ok' if gold == pred else 'X'}  {doi}")
    acc = sum(1 for g, p in scored if g == p) / len(scored)
    # binary fabricated-vs-not detection (the headline CiteAudit metric)
    tp = sum(1 for g, p in scored if g == "FABRICATED" and p == "FABRICATED")
    fp = sum(1 for g, p in scored if g != "FABRICATED" and p == "FABRICATED")
    fn = sum(1 for g, p in scored if g == "FABRICATED" and p != "FABRICATED")
    prec = tp / (tp + fp) if tp + fp else float("nan")
    rec = tp / (tp + fn) if tp + fn else float("nan")
    retr = [(g, p) for g, p in scored if g == "RETRACTED"]
    retr_ok = sum(1 for g, p in retr if p == "RETRACTED")
    print(f"\n3-way accuracy        {acc:.0%} ({sum(1 for g, p in scored if g == p)}/{len(scored)})")
    print(f"fabricated precision  {prec:.2f}")
    print(f"fabricated recall     {rec:.2f}")
    print(f"retraction recall     {retr_ok}/{len(retr)}")


if __name__ == "__main__":
    main()
