"""Save the exact paper texts each question was answered from, so every tool
reads the same papers. No model runs here.

For each question in questions.json, loads its papers the way the plugin
does (Zotero PDF by item key, else the DOI fetched online and cached) and
writes papers/<question id>/:
    manifest.json   {id, question, papers: [{n, label, key, txt, chars}]}
    NN.txt          the full text (give these to PaperQA2 / NotebookLM / ...)
    NN.json         our loader's output, which run_ours.py reads back

    python benchmark/compare/export_papers.py ZOTERO_SQLITE_COPY [QUESTION_ID ...]

ZOTERO_SQLITE_COPY: a copy of zotero.sqlite (Zotero locks the live one).
The texts are full papers: papers/ is git-ignored, keep it local.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(HERE))))

from worth_your_read import sidecar, store  # noqa: E402
from worth_your_read.benchmark.compare import common  # noqa: E402
from worth_your_read.benchmark.multi_source_experiment import papers_from_zotero  # noqa: E402


def spec_for(paper: dict, db_path: str) -> dict:
    if paper["key"].startswith("10."):                      # a DOI from a Research run
        title = paper["label"].rsplit(" (", 1)[0]
        return {"title": title, "reference": paper["key"]}
    return papers_from_zotero(db_path, "keys:" + paper["key"])[0]["spec"]


def export(q: dict, db_path: str) -> None:
    out = os.path.join(common.PAPERS, q["id"])
    os.makedirs(out, exist_ok=True)
    papers = []
    for n, p in enumerate(q["papers"], 1):
        fetched, pages, _ = sidecar._load(sidecar.Paper(**spec_for(p, db_path)))
        text = fetched.get("full_text") or ""
        with open(os.path.join(out, f"{n:02d}.txt"), "w") as f:
            f.write(text)
        with open(os.path.join(out, f"{n:02d}.json"), "w") as f:
            json.dump({"fetched": fetched, "pages": pages}, f, ensure_ascii=False)
        papers.append({"n": n, "label": p["label"], "key": p["key"], "txt": f"{n:02d}.txt", "chars": len(text)})
        print(f"  {n:2d} {len(text):7d} chars  {p['label'][:70]}")
    with open(os.path.join(out, "manifest.json"), "w") as f:
        json.dump({"id": q["id"], "question": q["question"], "papers": papers}, f, indent=1, ensure_ascii=False)


if __name__ == "__main__":
    store.enable()          # the on-disk cache our runs read through: same texts
    db, only = sys.argv[1], sys.argv[2:]
    for q in common.questions():
        if not only or q["id"] in only:
            print(q["id"])
            export(q, db)
