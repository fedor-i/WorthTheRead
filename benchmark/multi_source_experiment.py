"""Compare ways of attributing a merged answer sentence to its sources.
Runs the configured model (set WYR_USE_OLLAMA=1 for the local one).

  checks   On the labelled set (multi_source_gold.jsonl): for each sentence,
           treat its cited quotes as the findings and run the whole-sentence
           check (_supporting) and the part check (_supporting_parts). Writes
           predictions and prints scores. One short call per sentence each.

  run      On a Zotero collection: ask every paper once (findings cached in
           OUT/findings.json), then combine the same findings with each method
           in answer.COMBINE and write OUT/<method>.json — sentence, sources —
           for labelling.

    python benchmark/multi_source_experiment.py checks OUT
    python benchmark/multi_source_experiment.py run OUT ZOTERO_SQLITE_COPY SOURCE "QUESTION"
    SOURCE: a collection name, "keys:KEY1,KEY2" (Zotero item keys), or
    "research:N" (snowball the question with research.py, read the N most
    relevant papers online; ZOTERO_SQLITE_COPY is then unused)
    (METHODS=merge,grouped to run only some; findings are reused from OUT)
"""
import json
import os
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from worth_your_read import answer, store  # noqa: E402
from worth_your_read.benchmark.multi_source_score import load_gold, score  # noqa: E402


def checks(out):
    gold = load_gold()
    by_claim = {}
    for g in gold:
        by_claim.setdefault(g["claim"], []).append(g)
    preds = {"strict": {}, "parts": {}}
    parts_seen = {}
    t = time.time()
    for claim, pairs in by_claim.items():
        found = [(1, g["quote"], [g["quote"]]) for g in pairs]   # the quote is the finding
        ids = set(range(len(found)))
        strict = answer._supporting(claim, found, ids)
        keep, where = answer._supporting_parts(claim, found, ids)
        for i, g in enumerate(pairs):
            preds["strict"][g["id"]] = i in strict
            preds["parts"][g["id"]] = i in keep
            if i in where:
                parts_seen[g["id"]] = where[i]
    print(f"{time.time() - t:.0f}s for {len(by_claim)} sentences x 2 checks")
    for name, keep in preds.items():
        path = os.path.join(out, f"checks_{name}.json")
        with open(path, "w") as f:
            json.dump({"method": f"{name} check on quotes ({os.environ.get('WYR_OLLAMA_MODEL', '')})",
                       "keep": keep, "parts": parts_seen if name == "parts" else {}}, f, indent=1)
        for sure_only in (False, True):
            r = score(load_gold(sure_only), keep)
            print(name, "sure-only" if sure_only else "all", {k: r[k] for k in (
                "precision", "recall", "recall_backs", "recall_partly",
                "sentences_with_a_wrong_source", "sentences_left_unsupported")})
            print("   wrong kept:", r["wrong_sources_kept"], " good cut:", r["good_sources_cut"])
    print("parts:", json.dumps(parts_seen, indent=1, ensure_ascii=False))


def papers_from_research(question, n):
    """[{label, key, spec}] for the n most relevant papers a research run finds."""
    from worth_your_read import research
    t = time.time()
    r = research.snowball(question, max_papers=60,
                          progress=lambda p: print(p.get("stage", ""), end=" ", flush=True))
    found = sorted((p for p in r["papers"] if (p.get("relevance") or 0) >= 2 and p.get("doi")),
                   key=lambda p: (-p["relevance"], -(p.get("cited_by") or 0)))[:n]
    print(f"\nresearch: {len(r['papers'])} papers, {len(found)} used, {time.time() - t:.0f}s")
    return [{"label": f"{p['title'][:60]} ({p.get('year')})", "key": p["doi"],
             "spec": {"title": p["title"], "reference": p["doi"]}} for p in found]


def papers_from_zotero(db_path, collection):
    """[{label, key, spec}] for the regular items of a collection, or for
    "keys:K1,K2" — those items."""
    db = sqlite3.connect(db_path)
    field = lambda iid, name: (db.execute(
        "SELECT v.value FROM itemData d JOIN fields f USING(fieldID) JOIN itemDataValues v USING(valueID)"
        " WHERE d.itemID = ? AND f.fieldName = ?", (iid, name)).fetchone() or [""])[0]
    if collection.startswith("keys:"):
        keys = collection[5:].split(",")
        rows = [db.execute("SELECT itemID, key FROM items WHERE key = ?", (k,)).fetchone() for k in keys]
    else:
        coll = db.execute("SELECT collectionID FROM collections WHERE collectionName = ?", (collection,)).fetchone()[0]
        rows = db.execute(
            "SELECT i.itemID, i.key FROM collectionItems ci JOIN items i USING(itemID)"
            " JOIN itemTypes t USING(itemTypeID) WHERE ci.collectionID = ? AND t.typeName NOT IN"
            " ('note', 'attachment', 'annotation') ORDER BY ci.orderIndex", (coll,)).fetchall()
    out = []
    for iid, key in rows:
        names = [r[0] for r in db.execute(
            "SELECT c.lastName FROM itemCreators ic JOIN creators c USING(creatorID)"
            " WHERE ic.itemID = ? ORDER BY ic.orderIndex", (iid,))]
        who = names[0] + (" et al." if len(names) > 2 else f" and {names[1]}" if len(names) == 2 else "") if names else ""
        year = field(iid, "date")[:4]
        title = field(iid, "title")
        pdf = db.execute("SELECT i.key FROM itemAttachments a JOIN items i USING(itemID)"
                         " WHERE a.parentItemID = ? AND a.contentType = 'application/pdf'", (iid,)).fetchone()
        spec = {"title": title, "reference": field(iid, "DOI") or field(iid, "url")}
        if pdf:
            spec["attachment_key"] = pdf[0]
        out.append({"label": f"{who} {year} — {title[:70]}".strip(), "key": key, "spec": spec})
    return out


def run(out, db_path, collection, question):
    from worth_your_read import sidecar
    store.enable()
    cache = os.path.join(out, "findings.json")
    if os.path.exists(cache):
        with open(cache) as f:
            saved = json.load(f)
        chosen = saved["papers"]
        found = [tuple(x) for x in saved["found"]]
    else:
        chosen = []
        source = (papers_from_research(question, int(collection[9:])) if collection.startswith("research:")
                  else papers_from_zotero(db_path, collection))
        for p in source:
            try:
                fetched, pages, _ = sidecar._load(sidecar.Paper(**p["spec"]))
            except Exception:
                continue
            if fetched.get("full_text"):
                chosen.append({"label": p["label"], "key": p["key"], "fetched": fetched})
        t = time.time()
        found, _, _ = answer._map(question, chosen, lambda **kw: print(kw.get("paper"), end=" ", flush=True))
        print(f"\nmap: {len(chosen)} papers, {len(found)} findings, {time.time() - t:.0f}s")
        chosen = [{"label": p["label"], "key": p["key"]} for p in chosen]
        with open(cache, "w") as f:
            json.dump({"question": question, "papers": chosen, "found": found}, f, indent=1, ensure_ascii=False)
    only = os.environ.get("METHODS", "").split(",") if os.environ.get("METHODS") else list(answer.COMBINE)
    for method, combine in answer.COMBINE.items():
        if method not in only:
            continue
        t = time.time()
        final, not_addressed, truncated = combine(question, chosen, found, lambda **kw: None)
        rows = [{"claim": text, "sources": [
            {"finding": i + 1, "paper": chosen[found[i][0] - 1]["label"].split(" — ")[0],
             "finding_text": found[i][1], "part": where.get(i)} for i in sorted(ids)]}
            for text, ids, where in final]
        with open(os.path.join(out, f"{method}.json"), "w") as f:
            json.dump({"method": method, "seconds": round(time.time() - t), "truncated": truncated,
                       "not_addressed": not_addressed, "claims": rows}, f, indent=1, ensure_ascii=False)
        print(f"{method}: {len(rows)} sentences, {sum(len(r['sources']) for r in rows)} sources, "
              f"{time.time() - t:.0f}s")


if __name__ == "__main__":
    mode, out = sys.argv[1], sys.argv[2]
    os.makedirs(out, exist_ok=True)
    if mode == "checks":
        checks(out)
    else:
        run(out, *sys.argv[3:6])
