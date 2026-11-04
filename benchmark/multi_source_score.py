"""Score a method that decides which sources an answer sentence keeps.

`multi_source_gold.jsonl` holds (answer sentence, cited quote) pairs from an
Ask-across-papers run, each labelled backs / partly / no:
  backs   the quote alone supports the whole sentence
  partly  it supports one distinct part of the sentence (same place, subject)
  no      a different region, nutrient or result; same topic only
A source should be KEPT if it backs or partly backs the sentence.

Predictions are a JSON file {"method": "...", "keep": {pair id: true/false}}.
No model runs here; producing predictions is the method's job.

    python benchmark/multi_source_score.py benchmark/multi_source_baseline.json [--sure-only]
"""
import json
import os
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))


def load_gold(sure_only=False):
    with open(os.path.join(HERE, "multi_source_gold.jsonl")) as f:
        gold = [json.loads(line) for line in f if line.strip()]
    return [g for g in gold if g["sure"] or not sure_only]


def score(gold, keep):
    should = {g["id"]: g["gold"] in ("backs", "partly") for g in gold}
    kept = [g for g in gold if keep.get(g["id"])]
    good = [g for g in gold if should[g["id"]]]
    rate = lambda a, b: round(a / b, 3) if b else None
    by_claim = defaultdict(list)
    for g in gold:
        by_claim[g["claim"]].append(g)
    return {
        "pairs": len(gold),
        # of the sources kept, how many back the sentence (fully or in part)
        "precision": rate(sum(should[g["id"]] for g in kept), len(kept)),
        # of the sources that back it, how many were kept
        "recall": rate(sum(bool(keep.get(g["id"])) for g in good), len(good)),
        "recall_backs": rate(sum(bool(keep.get(g["id"])) for g in good if g["gold"] == "backs"),
                             sum(g["gold"] == "backs" for g in good)),
        "recall_partly": rate(sum(bool(keep.get(g["id"])) for g in good if g["gold"] == "partly"),
                              sum(g["gold"] == "partly" for g in good)),
        # the dangerous error: a sentence cites a paper that doesn't back it
        "wrong_sources_kept": [g["id"] for g in kept if not should[g["id"]]],
        "good_sources_cut": [g["id"] for g in good if not keep.get(g["id"])],
        "sentences_with_a_wrong_source": sum(
            any(keep.get(g["id"]) and not should[g["id"]] for g in gs) for gs in by_claim.values()),
        "sentences_left_unsupported": sum(
            not any(keep.get(g["id"]) and should[g["id"]] for g in gs) for gs in by_claim.values()),
        "sentences": len(by_claim),
    }


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    gold = load_gold(sure_only="--sure-only" in sys.argv)
    keep_all = {g["id"]: True for g in gold}
    print("keep everything (no check):", json.dumps(score(gold, keep_all), indent=1))
    for path in args:
        with open(path) as f:
            pred = json.load(f)
        print(f"{pred.get('method', path)}:", json.dumps(score(gold, pred["keep"]), indent=1))
