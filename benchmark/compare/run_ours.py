"""Answer each question with Worth Your Read's Ask across papers, from the
exported texts, and save it in the shared format (answers/wyr/<id>.json).
Runs the local model: set WYR_USE_OLLAMA=1 WYR_OLLAMA_MODEL=gemma4:26b.

    python benchmark/compare/run_ours.py [QUESTION_ID ...]

The answer is the merged claims; the per-paper "further findings" the plugin
lists below them are counted (`extra_findings`) but not scored, since the
other tools have no such section.
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(HERE))))

from worth_your_read import answer, config  # noqa: E402
from worth_your_read.benchmark.compare import common  # noqa: E402


def to_shared(qid: str, question: str, r: dict, seconds: float) -> dict:
    main = [c for c in r["claims"] if not c.get("extra")]
    return {
        "tool": "wyr", "question_id": qid, "question": question,
        "settings": {"model": os.environ.get("WYR_OLLAMA_MODEL", "") if config.USE_OLLAMA else "api",
                     "method": "merge + part check", "max_papers": None},
        "seconds": round(seconds),
        "sentences": [{"text": c["claim"],
                       "sources": [{"paper": s["paper"], "passage": s["quote"], "part": s.get("part")}
                                   for s in c["sources"]],
                       "flags": {"unsourced_numbers": c.get("unsourced_numbers") or [],
                                 "problems": c.get("problems") or []}} for c in main],
        "unknown_citations": 0,
        "extra_findings": len(r["claims"]) - len(main),
        "raw": json.dumps(r, ensure_ascii=False),
    }


def run(qid: str) -> str:
    m = common.manifest(qid)
    papers = []
    for p in m["papers"]:
        with open(os.path.join(common.PAPERS, qid, f"{p['n']:02d}.json")) as f:
            saved = json.load(f)
        papers.append({"label": p["label"], "key": p["key"], "fetched": saved["fetched"], "pages": saved["pages"]})
    t = time.time()
    r = answer.ask_many(m["question"], papers,
                        progress=lambda p: print(p.get("stage"), p.get("paper") or p.get("claim") or "", end="  ", flush=True))
    path = common.save_answer("wyr", qid, to_shared(qid, m["question"], r, time.time() - t))
    print(f"\n{qid}: {time.time() - t:.0f}s -> {path}")
    return path


if __name__ == "__main__":
    for q in common.questions():
        if len(sys.argv) < 2 or q["id"] in sys.argv[1:]:
            run(q["id"])
