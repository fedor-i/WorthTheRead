"""Shared by the comparison scripts; plain Python, no package imports, so the
PaperQA2 runner (its own venv) can use it too.

Every tool's answer is saved in one format, answers/<tool>/<question id>.json:

    {"tool", "question_id", "question", "settings", "seconds",
     "sentences": [{"text": "...", "sources": [{"paper": n, "passage": "..."}]}],
     "unknown_citations": k, "raw": "the tool's answer as it gave it"}

`paper` is the paper's number in papers/<question id>/manifest.json (1-based),
or 0 when the tool cited something that isn't one of the papers.
"""
import json
import os
import re
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
PAPERS = os.path.join(HERE, "papers")
ANSWERS = os.path.join(HERE, "answers")


def questions() -> list:
    with open(os.path.join(HERE, "questions.json")) as f:
        return json.load(f)


def manifest(qid: str) -> dict:
    """{id, question, papers: [{n, label, key, txt}]} written by export_papers.py."""
    path = os.path.join(PAPERS, qid, "manifest.json")
    if not os.path.exists(path):
        raise SystemExit(f"No paper texts for {qid}: run export_papers.py first.")
    with open(path) as f:
        return json.load(f)


def paper_text(qid: str, n: int) -> str:
    with open(os.path.join(PAPERS, qid, f"{n:02d}.txt")) as f:
        return f.read()


def save_answer(tool: str, qid: str, answer: dict) -> str:
    os.makedirs(os.path.join(ANSWERS, tool), exist_ok=True)
    path = os.path.join(ANSWERS, tool, f"{qid}.json")
    with open(path, "w") as f:
        json.dump(answer, f, indent=1, ensure_ascii=False)
    return path


def load_answers() -> list:
    out = []
    if not os.path.isdir(ANSWERS):
        return out
    for tool in sorted(os.listdir(ANSWERS)):
        d = os.path.join(ANSWERS, tool)
        for name in sorted(os.listdir(d)) if os.path.isdir(d) else []:
            if name.endswith(".json"):
                with open(os.path.join(d, name)) as f:
                    out.append(json.load(f))
    return out


# "et al.", "e.g.", "Fig. 2", "approx. 3" and decimals don't end a sentence.
_NO_END = re.compile(r"(?:\bet al|\be\.g|\bi\.e|\bvs|\bFig|\bFigs|\bEq|\bapprox|\bca|\bNo|\bRef|\b[A-Z])\.$")


def split_sentences(text: str) -> list:
    """Split an answer into sentences; markdown bullets and headings count as
    their own lines. Keeps the citation markers in place."""
    out = []
    for line in text.splitlines():
        line = re.sub(r"^\s*(?:[-*•]|\d+[.)]|#+)\s+", "", line).strip()
        if not line:
            continue
        start = 0
        for m in re.finditer(r"[.!?](?:\s*(?:\[[^\]]*\]|\([^)]*\)))*\s+(?=[A-Z(\[\"“])", line):
            head = line[start:m.start() + 1]
            if _NO_END.search(head.rstrip()):
                continue
            out.append(line[start:m.end()].strip())
            start = m.end()
        if line[start:].strip():
            out.append(line[start:].strip())
    return out


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).lower()
    s = s.replace("−", "-").replace("­", "")
    s = re.sub(r"-\s*\n\s*", "", s)
    return re.sub(r"[^a-z0-9%.]+", " ", s).strip()


def in_paper(passage: str, text: str) -> bool:
    """Is the passage really in the paper? Whitespace, case, hyphenation and
    punctuation are ignored. A long passage passes when 80% of its 8-word
    windows appear, since tools trim and re-flow what they show."""
    p, t = norm(passage), norm(text)
    if not p:
        return False
    if p in t:
        return True
    words = p.split()
    if len(words) < 16:
        return False
    shingles = [" ".join(words[i:i + 8]) for i in range(0, len(words) - 7, 4)]
    return sum(s in t for s in shingles) >= 0.8 * len(shingles)
