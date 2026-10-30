"""CiteME adapter — find the paper a claim cites, via our discovery.

CiteME (Press et al., NeurIPS 2024) gives a text excerpt that references one
paper and asks the system to identify it. That's our discovery step: search
OpenAlex + Crossref + Europe PMC (+ citation trail) for the claim and check
whether the cited paper is among the top-k candidates. Keyless; scored as
recall@k. Replace ITEMS with CiteME's released excerpts to run their set.

Run (keyless):
    python -m worth_your_read.benchmark.adapters.citeme
"""
from ... import discover

# (claim/excerpt paraphrasing the finding, gold DOI lowercased)
ITEMS = [
    ("a programmable dual-RNA-guided Cas9 endonuclease that cleaves target DNA for genome editing",
     "10.1126/science.1225829"),
    ("t-SNE visualisations of single-cell RNA-seq can distort global structure and between-cluster distances",
     "10.1038/s41467-019-13056-x"),
    ("a deep learning system predicting protein three-dimensional structure with near-experimental accuracy",
     "10.1038/s41586-021-03819-2"),
    ("carotenoids assist cyanobacterial photosystem II assembly and function",
     "10.3389/fpls.2016.00295"),
    ("the role of the xanthophyll cycle and lutein in photoprotection of photosystem II",
     "10.1016/j.bbabio.2011.04.012"),
    ("vitamin D supplementation to prevent acute respiratory tract infections, meta-analysis of randomised trials",
     "10.1136/bmj.i6583"),
    ("induction of pluripotent stem cells from adult fibroblasts by defined transcription factors",
     "10.1016/j.cell.2006.07.024"),
    ("long-term coffee consumption and the risk of cardiovascular disease, dose-response meta-analysis",
     "10.1161/circulationaha.113.005925"),
    ("observation of gravitational waves from a binary black hole merger by LIGO",
     "10.1103/physrevlett.116.061102"),
    ("early time-restricted feeding improves insulin sensitivity and blood pressure in prediabetic men",
     "10.1016/j.cmet.2018.04.010"),
]
KS = (1, 5, 10)


def rank_of_gold(claim, gold):
    cands = discover.discover_sources(claim, n=10, rounds=2)
    for i, c in enumerate(cands, 1):
        if (c.get("doi") or "").lower() == gold:
            return i
    return None


def main():
    print(f"CiteME find-the-source via discovery — {len(ITEMS)} items, keyless\n")
    ranks = []
    for claim, gold in ITEMS:
        r = rank_of_gold(claim, gold)
        ranks.append(r)
        print(f"  rank={r if r else '>10':<4} {gold:<34} {claim[:44]}")
    print()
    for k in KS:
        hits = sum(1 for r in ranks if r and r <= k)
        print(f"  recall@{k:<2}  {hits}/{len(ITEMS)} ({hits / len(ITEMS):.0%})")
    found = [r for r in ranks if r]
    if found:
        mrr = sum(1 / r for r in found) / len(ITEMS)
        print(f"  MRR      {mrr:.2f}")


if __name__ == "__main__":
    main()
