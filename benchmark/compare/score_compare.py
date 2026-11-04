"""Score every tool's answers from the judge's labels. No model runs here.

    python benchmark/compare/score_compare.py [JUDGE]     (default: the only one in judged/)

Per tool, over all questions it answered (and per question with --each):
  sentences         answer sentences
  uncited           sentences with no citation at all
  citations         (sentence, passage) pairs
  precision         share of citations that back the sentence, wholly or in part
  wrong             citations that don't back their sentence
  supported         sentences whose citations together support all of it,
                    out of ALL sentences (uncited ones count as unsupported)
  part-supported    sentences supported only in part
  not in paper      cited passages that are not in the paper's text (made up,
                    or cited to the wrong paper), plus citations to no paper
  papers cited      distinct papers cited, of those given
  minutes           time to answer (as recorded)
"""
import json
import os
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import common  # noqa: E402

JUDGED = os.path.join(HERE, "judged")
COLS = ["sentences", "uncited", "citations", "precision", "wrong", "supported", "part-supported",
        "not in paper", "papers cited", "minutes"]


def tally(answer: dict, judged: dict) -> dict:
    qid = answer["question_id"]
    texts = {}
    labels = {j["text"]: j for j in judged["sentences"]}
    t = defaultdict(float)
    cited_papers = set()
    for s in answer["sentences"]:
        j = labels.get(s["text"], {"labels": [None] * len(s["sources"]), "together": None})
        t["sentences"] += 1
        t["uncited"] += not s["sources"]
        t["supported"] += j["together"] == "yes"
        t["part-supported"] += j["together"] == "partly"
        for src, label in zip(s["sources"], j["labels"]):
            t["citations"] += 1
            t["judged"] += label is not None
            t["good"] += label in ("backs", "partly")
            t["wrong"] += label == "no"
            n = src.get("paper") or 0
            if n not in texts and n:
                try:
                    texts[n] = common.paper_text(qid, n)
                except OSError:
                    texts[n] = ""
            t["not in paper"] += not (n and common.in_paper(src["passage"], texts[n]))
            if n:
                cited_papers.add(n)
    t["not in paper"] += answer.get("unknown_citations") or 0
    t["papers cited"] = len(cited_papers)
    t["papers given"] = len(common.manifest(qid)["papers"])
    t["seconds"] = answer.get("seconds") or 0
    return t


def row(t: dict) -> list:
    pct = lambda a, b: f"{a / b:.0%}" if b else "–"
    return [int(t["sentences"]), f"{int(t['uncited'])} ({pct(t['uncited'], t['sentences'])})",
            int(t["citations"]), pct(t["good"], t["judged"]), int(t["wrong"]),
            pct(t["supported"], t["sentences"]), pct(t["part-supported"], t["sentences"]),
            int(t["not in paper"]), f"{int(t['papers cited'])}/{int(t['papers given'])}",
            round(t["seconds"] / 60, 1)]


def main(judge: str, each: bool) -> None:
    jdir = os.path.join(JUDGED, judge)
    totals = defaultdict(lambda: defaultdict(float))
    lines = []
    for a in common.load_answers():
        path = os.path.join(jdir, a["tool"], f"{a['question_id']}.json")
        if not os.path.exists(path):
            print(f"not judged yet: {a['tool']} {a['question_id']}")
            continue
        with open(path) as f:
            t = tally(a, json.load(f))
        for k, v in t.items():
            totals[a["tool"]][k] += v
        if each:
            lines.append([f"{a['tool']} · {a['question_id']}", *row(t)])
    print(f"judge: {judge}\n")
    print("| tool | " + " | ".join(COLS) + " |")
    print("|---" * (len(COLS) + 1) + "|")
    for tool, t in sorted(totals.items()):
        print("| " + " | ".join(str(x) for x in [tool, *row(t)]) + " |")
    for line in lines:
        print("| " + " | ".join(str(x) for x in line) + " |")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    judges = sorted(os.listdir(JUDGED)) if os.path.isdir(JUDGED) else []
    judges = [j for j in judges if os.path.isdir(os.path.join(JUDGED, j))]
    if not args and len(judges) != 1:
        raise SystemExit(f"Name the judge: {judges or 'none judged yet'}")
    main(args[0] if args else judges[0], "--each" in sys.argv)
