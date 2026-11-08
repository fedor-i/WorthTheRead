"""How much of what an expert review says does an answer recover?

The comparison in benchmark/compare asks whether what we said is supported, so
it can't see what we left out. Here a highly cited review is the answer key.
The tool never sees the review: it reads the papers the review cites, all
published no later than the review, so nothing can be copied from it.

Reviews in reviews.json are split: tune on "tune", and look at "test" only to
report, so that prompt changes aren't fitted to the scores.

    python benchmark/reviews/run.py load [ID ...]      papers, no model
    python benchmark/reviews/run.py extract [ID ...]   key statements (Bedrock)
    python benchmark/reviews/run.py answer [ID ...]    our answer (local model)
    python benchmark/reviews/run.py score [ID ...]     coverage (Bedrock)
    python benchmark/reviews/run.py table [--test]     no model
    python benchmark/reviews/run.py export [ID ...]    the papers we read, for other tools
    python benchmark/reviews/run.py rival TOOL [ID ...]   score another tool's answer (Bedrock)

Each key statement gets one label per level of the answer, so a miss shows
where it was lost: never in the papers read, not read out of them, dropped in
the merge, or dropped from the short answer. papers/, answers/ and scored/ are
git-ignored; key/ holds statements in our judge's own words.

`answer` also reads up to answer.FOLLOW_PAPERS papers that the chosen papers'
introductions cite (cites.py). They are older than the review, being cited by
papers that are, and are saved to papers/ID/followed.json for scoring.
"""
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(HERE))))

from worth_your_read import answer, focus, research, sidecar, store  # noqa: E402

LOAD_PAPERS = 80       # cited papers fetched, the ones closest to the question
READ_PAPERS = 40       # as in the OCP run; ask_many ranks them by their full text
KEY_STATEMENTS = 12
PASSAGES = 8           # per statement, for "is it in the papers read at all"
LEVELS = ("reachable", "findings", "evidence", "short")

_EXTRACT_SYSTEM = f"""You read a REVIEW article and list what a knowledgeable \
reader must find in any good answer to a QUESTION. Give at most {KEY_STATEMENTS} \
key statements, most important first. Each one is self-contained, under 30 \
words, in your own words, and is a conclusion the review draws from the \
literature it cites, not the authors' outlook or a suggestion for future work. \
"kind" is "finding", or "disagreement" when the review says the evidence or \
views conflict, in which case the statement names both sides.

Return ONLY a JSON object: {{"statements": [{{"text": "...", "kind": "finding"}}]}}"""

_COVER_SYSTEM = """You check whether a KEY STATEMENT appears in each of four \
TEXTS. For each answer "yes" (it says the same thing, in any words), "partly" \
(a real part of it, with the main point missing) or "no" (not there, only the \
same topic, or the opposite). When the key statement is a disagreement, "yes" \
needs both sides and some sign that they conflict; both sides with no sign of \
conflict is "partly".

Return ONLY a JSON object: {"passages": "no", "findings": "no", "evidence": "no", "short": "no"}"""


def _path(*parts) -> str:
    return os.path.join(HERE, *parts)


def reviews(only: list = (), split: str = None) -> list:
    with open(_path("reviews.json")) as f:
        rows = json.load(f)
    return [r for r in rows if (not only or r["id"] in only) and (not split or r["split"] == split)]


def _read(*parts):
    with open(_path(*parts)) as f:
        return json.load(f)


def _write(data, *parts) -> None:
    path = _path(*parts)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)


def cited_papers(review: dict, works: list) -> list:
    """The works a review can draw on: not the review itself, not newer than
    it, with a DOI to fetch, one per title, closest to the question first."""
    seen, out = {research._title_key(review["title"])}, []
    for w in works:
        key = research._title_key(w["title"])
        if key in seen or not w.get("doi") or not w.get("year") or w["year"] > review["year"]:
            continue
        seen.add(key)
        out.append(w)
    docs = [f"{w['title']} {w.get('abstract') or ''}" for w in out]
    scores = focus.bm25_rank(review["question"], docs)
    return [w for _, w in sorted(zip(scores, out), key=lambda x: -x[0])]


def load(r: dict) -> None:
    work = research.fetch_works(dois=[r["doi"]])[0]
    fetched, _, _ = sidecar._load(sidecar.Paper(title=work["title"], reference=r["doi"]))
    os.makedirs(_path("papers", r["id"]), exist_ok=True)
    with open(_path("papers", r["id"], "review.txt"), "w") as f:
        f.write(fetched.get("full_text") or "")
    works = cited_papers({**work, "question": r["question"]}, research.fetch_works(ids=work["refs"]))
    papers = []
    for w in works[:LOAD_PAPERS]:
        try:
            got, pages, _ = sidecar._load(sidecar.Paper(title=w["title"], reference=w["doi"]))
        except Exception:
            continue
        if got.get("full_text"):
            papers.append({"label": f"{w['title']} ({w['year']})", "key": w["doi"], "fetched": got, "pages": pages})
    _write({"id": r["id"], "title": work["title"], "year": work["year"], "cited": len(work["refs"]),
            "papers": papers}, "papers", r["id"], "papers.json")
    print(f"{r['id']}: {work['title'][:60]} | {len(work['refs'])} cited, {len(works)} usable, {len(papers)} loaded, "
          f"{sum(1 for p in papers if p['fetched'].get('has_fulltext'))} full text", flush=True)


def extract(r: dict) -> None:
    with open(_path("papers", r["id"], "review.txt")) as f:
        text = f.read()
    data = answer._parse(answer._complete(_EXTRACT_SYSTEM, f"QUESTION: {r['question']}\n\nREVIEW:\n{text}",
                                          max_tokens=2000))
    rows = [{"text": s["text"].strip(), "kind": "disagreement" if s.get("kind") == "disagreement" else "finding"}
            for s in data.get("statements") or [] if isinstance(s, dict) and (s.get("text") or "").strip()]
    _write({"id": r["id"], "question": r["question"], "statements": rows[:KEY_STATEMENTS]}, "key", f"{r['id']}.json")
    print(f"{r['id']}: {len(rows)} statements", flush=True)


def run_answer(r: dict) -> None:
    papers = _read("papers", r["id"], "papers.json")["papers"]
    followed = []

    def follow(doi, title):
        # Cited by a paper older than the review, so older than the review too.
        got, pages, _ = sidecar._load(sidecar.Paper(title=title, reference=doi))
        followed.append({"label": title or doi, "key": doi, "fetched": got, "pages": pages})
        return followed[-1]
    t = time.time()
    out = answer.ask_many(r["question"], papers, progress=lambda p: print(p.get("stage"), end=" ", flush=True),
                          max_papers=READ_PAPERS, follow=follow)
    out["seconds"] = round(time.time() - t)
    out["read"] = [p["key"] for p in out["papers"]]
    _write({"id": r["id"], "papers": followed}, "papers", r["id"], "followed.json")
    _write(out, "answers", f"{r['id']}.json")
    print(f"\n{r['id']}: {out['seconds']} s", flush=True)


def levels(ans: dict) -> dict:
    """The answer as the reader meets it: every finding, the evidence, the short answer."""
    main = [c["claim"] for c in ans["claims"] if not c.get("extra")]
    return {"short": "\n".join(c["claim"] for c in ans.get("overview") or []), "evidence": "\n".join(main),
            "findings": "\n".join(c["claim"] for c in ans["claims"])}


def top_passages(statement: str, papers: list, n: int = PASSAGES) -> list:
    chunks = [p for paper in papers for p in focus.passages(paper["fetched"].get("full_text") or "")]
    scores = focus.bm25_rank(statement, chunks)
    return [c for _, c in sorted(zip(scores, chunks), key=lambda x: -x[0])[:n]]


def cover(statement: dict, texts: dict, passages: list) -> dict:
    listing = "\n\n".join(f"{name.upper()}:\n{texts[name]}" for name in ("findings", "evidence", "short"))
    raw = answer._complete(_COVER_SYSTEM, f"KEY STATEMENT ({statement['kind']}): {statement['text']}\n\n"
                           f"PASSAGES:\n" + "\n\n".join(passages) + f"\n\n{listing}", max_tokens=800)
    data = answer._parse(raw)
    label = lambda k: str(data.get(k, "")).lower() if str(data.get(k, "")).lower() in ("yes", "partly", "no") else None
    return {"reachable": label("passages"), "findings": label("findings"), "evidence": label("evidence"),
            "short": label("short")}


def score(r: dict) -> None:
    ans = _read("answers", f"{r['id']}.json")
    read = set(ans["read"])
    papers = [p for p in _papers_read(r["id"]) if p["key"] in read]
    texts = levels(ans)
    path = _path("scored", f"{r['id']}.json")
    done = {s["text"]: s for s in _read("scored", f"{r['id']}.json")["statements"]} if os.path.exists(path) else {}
    rows = []
    for s in _read("key", f"{r['id']}.json")["statements"]:
        prev = done.get(s["text"])
        if prev and all(prev[k] for k in LEVELS):      # only a reply we couldn't read is asked again
            rows.append(prev)
            continue
        rows.append({**s, **cover(s, texts, top_passages(s["text"], papers))})
        print(r["id"], rows[-1]["kind"], [rows[-1][k] for k in LEVELS], flush=True)
    _write({"id": r["id"], "statements": rows}, "scored", f"{r['id']}.json")


def _papers_read(rid: str) -> list:
    """The review's loaded papers, plus those its papers' introductions led to."""
    more = _read("papers", rid, "followed.json")["papers"] if os.path.exists(_path("papers", rid, "followed.json")) else []
    return _read("papers", rid, "papers.json")["papers"] + more


def export(r: dict) -> None:
    """The papers our run read, as plain text, so another tool gets the same ones."""
    read = _read("answers", f"{r['id']}.json")["read"]
    by_key = {p["key"]: p for p in _papers_read(r["id"])}
    out = _path("papers", "_rivals", r["id"])
    os.makedirs(out, exist_ok=True)
    papers = []
    for n, key in enumerate(read, 1):
        with open(os.path.join(out, f"{n:02d}.txt"), "w") as f:
            f.write(by_key[key]["fetched"].get("full_text") or "")
        papers.append({"n": n, "label": by_key[key]["label"], "key": key, "txt": f"{n:02d}.txt"})
    _write({"id": r["id"], "question": r["question"], "papers": papers}, "papers", "_rivals", r["id"], "manifest.json")


def rival(tool: str, r: dict) -> list:
    """Label another tool's whole answer. "In the papers read" stays ours: same papers."""
    ans = _read("answers", "rivals", tool, f"{r['id']}.json")
    text = "\n".join(s["text"] for s in ans["sentences"])
    rows = []
    for s in _read("scored", f"{r['id']}.json")["statements"]:
        got = cover(s, {"findings": text, "evidence": text, "short": text}, ["(not needed)"])
        rows.append({**s, "answer": got["short"]})
        if got["short"] is None:
            raise SystemExit(f"No label for: {s['text']}")
    _write({"id": r["id"], "tool": tool, "statements": rows}, "scored", "rivals", tool, f"{r['id']}.json")
    return rows


def tally(rows: list) -> dict:
    """Share recovered at each level, out of the statements in the papers read:
    a review may say what those papers don't, which is no fault of the answer."""
    if any(s["reachable"] is None for s in rows):
        raise SystemExit("Some statements have no label: run score again.")
    reach = [s for s in rows if s["reachable"] in ("yes", "partly")]
    pct = lambda k: sum(s[k] == "yes" for s in reach) / len(reach) if reach else 0.0
    part = lambda k: sum(s[k] in ("yes", "partly") for s in reach) / len(reach) if reach else 0.0
    conflicts = [s for s in reach if s["kind"] == "disagreement"]
    return {"statements": len(rows), "reachable": len(reach), **{k: pct(k) for k in LEVELS[1:]},
            "short_or_partly": part("short"), "conflicts": len(conflicts),
            "conflicts_short": sum(s["short"] == "yes" for s in conflicts)}


def table(split: str) -> None:
    print("| review | key statements | in papers read | in findings | in evidence | in short answer | "
          "short, yes or partly | conflicts in short |\n|---|---|---|---|---|---|---|---|")
    every = []
    for r in reviews(split=split):
        if not os.path.exists(_path("scored", f"{r['id']}.json")):
            print(f"| {r['id']} | not scored yet |")
            continue
        rows = _read("scored", f"{r['id']}.json")["statements"]
        every += rows
        print(_row(r["id"], tally(rows)))
    if every:
        print(_row("**all**", tally(every)))


def _row(name: str, t: dict) -> str:
    cells = [name, t["statements"], t["reachable"], *(f"{t[k]:.0%}" for k in ("findings", "evidence", "short", "short_or_partly")),
             f"{t['conflicts_short']}/{t['conflicts']}"]
    return "| " + " | ".join(str(c) for c in cells) + " |"


if __name__ == "__main__":
    store.enable()
    step, ids = sys.argv[1], [a for a in sys.argv[2:] if not a.startswith("--")]
    if step == "rival":
        for row in reviews(ids[1:]):
            rows = [s for s in rival(ids[0], row) if s["reachable"] in ("yes", "partly")]
            print(ids[0], row["id"], f"yes {sum(s['answer'] == 'yes' for s in rows)}/{len(rows)},",
                  f"yes or partly {sum(s['answer'] in ('yes', 'partly') for s in rows)}/{len(rows)}", flush=True)
    elif step == "table":
        table("test" if "--test" in sys.argv else "tune")
    else:
        step_fn = {"load": load, "extract": extract, "answer": run_answer, "score": score, "export": export}[step]
        for row in reviews(ids):
            step_fn(row)
