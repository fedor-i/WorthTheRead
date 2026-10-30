"""SciFact adapter — run Worth Your Read's judge on SciFact.

SciFact (Wadden et al., EMNLP 2020) verifies a claim against a paper abstract,
labels SUPPORT / CONTRADICT / NOINFO. That's our judge's task, so we run it in
the oracle-abstract setting: feed the gold abstract via verify_claim's
`evidence_text` path (no retrieval) and map our 4-way verdict to SciFact's 3:

    SUPPORTED -> SUPPORT ; UNSUPPORTED -> CONTRADICT ;
    INSUFFICIENT -> NOINFO ; OVERSTATED -> NOINFO (claim overreaches the abstract)

This is zero-shot transfer of a judge built for grounding/overstatement, not a
SciFact-tuned system. Needs a judge backend (ANTHROPIC_API_KEY or Bedrock).

Run:
    python -m worth_your_read.benchmark.adapters.scifact            # 15/class
    SAMPLE_PER_CLASS=25 python -m worth_your_read.benchmark.adapters.scifact
"""
import asyncio
import json
import os
import random
import tarfile
import urllib.request

from ... import server

DATA_URL = "https://scifact.s3-us-west-2.amazonaws.com/release/latest/data.tar.gz"
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scifact_data")
PER_CLASS = int(os.environ.get("SAMPLE_PER_CLASS", "15"))
MAP = {"SUPPORTED": "SUPPORT", "UNSUPPORTED": "CONTRADICT",
       "INSUFFICIENT": "NOINFO", "OVERSTATED": "NOINFO"}
LABELS = ["SUPPORT", "CONTRADICT", "NOINFO"]


def ensure_data():
    data = os.path.join(CACHE, "data")
    if os.path.isdir(data):
        return data
    os.makedirs(CACHE, exist_ok=True)
    tgz = os.path.join(CACHE, "data.tar.gz")
    print("downloading SciFact...")
    urllib.request.urlretrieve(DATA_URL, tgz)
    with tarfile.open(tgz) as t:
        t.extractall(CACHE)
    return data


def load_pairs(data):
    corpus = {}
    with open(os.path.join(data, "corpus.jsonl")) as fh:
        for line in fh:
            d = json.loads(line)
            corpus[d["doc_id"]] = d["title"] + " " + " ".join(d["abstract"])
    pairs = []  # (claim, abstract_text, gold)
    with open(os.path.join(data, "claims_dev.jsonl")) as fh:
        for line in fh:
            c = json.loads(line)
            if c.get("evidence"):
                for doc_id, ev in c["evidence"].items():
                    if int(doc_id) in corpus:
                        pairs.append((c["claim"], corpus[int(doc_id)], ev[0]["label"]))
            elif c.get("cited_doc_ids") and c["cited_doc_ids"][0] in corpus:
                pairs.append((c["claim"], corpus[c["cited_doc_ids"][0]], "NOINFO"))
    return pairs


def sample(pairs):
    rng = random.Random(42)
    out = []
    for lab in LABELS:
        rows = [p for p in pairs if p[2] == lab]
        rng.shuffle(rows)
        out += rows[:PER_CLASS]
    rng.shuffle(out)
    return out


def _prf(scored, label):
    tp = sum(1 for g, p in scored if g == label and p == label)
    fp = sum(1 for g, p in scored if g != label and p == label)
    fn = sum(1 for g, p in scored if g == label and p != label)
    pr = tp / (tp + fp) if tp + fp else float("nan")
    rc = tp / (tp + fn) if tp + fn else float("nan")
    f1 = 2 * pr * rc / (pr + rc) if pr == pr and rc == rc and pr + rc else float("nan")
    return pr, rc, f1, sum(1 for g, _ in scored if g == label)


async def run():
    data = sample(load_pairs(ensure_data()))
    print(f"SciFact oracle-label — {len(data)} pairs via verify_claim(evidence_text)\n")
    scored = []
    for i, (claim, abstract, gold) in enumerate(data, 1):
        r = await server._verify(claim, "", evidence_text=abstract)
        pred = MAP.get(r.get("verdict"), "NOINFO")
        scored.append((gold, pred))
        print(f"  [{i:>2}] gold={gold:<10} pred={pred:<10} {'ok' if gold == pred else 'X'}  {claim[:50]}")
    acc = sum(1 for g, p in scored if g == p) / len(scored)
    print(f"\naccuracy {acc:.0%} ({sum(1 for g, p in scored if g == p)}/{len(scored)})")
    print("\n  label        precision recall   f1  support")
    for lab in LABELS:
        pr, rc, f1, sup = _prf(scored, lab)
        fm = lambda x: "  n/a" if x != x else f"{x:5.2f}"
        print(f"  {lab:<11}  {fm(pr)}  {fm(rc)} {fm(f1)}   {sup}")


if __name__ == "__main__":
    asyncio.run(run())
