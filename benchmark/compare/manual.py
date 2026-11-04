"""Bring in answers from tools that can only be used by hand (NotebookLM,
ReadCube Papers, Elicit, ...). No model runs here.

    python benchmark/compare/manual.py template TOOL [QUESTION_ID ...]
        writes answers/TOOL/<id>.md to fill in
    python benchmark/compare/manual.py parse TOOL
        turns every filled answers/TOOL/<id>.md into answers/TOOL/<id>.json

In the tool: add the question's papers (papers/<id>/NN.txt, or the same PDFs),
ask the question exactly as written, then fill the template:
- under "## Answer", paste the answer; write each citation as [1], [2], ...
  (NotebookLM already numbers them);
- under "## Sources", one line per citation number:  [1] P3 | the passage the
  tool shows for it (hover / click the citation and copy its text). P3 is the
  paper's number from the list in the template; use P0 if it is none of them.
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import common  # noqa: E402

_MARK = re.compile(r"\[(\d+(?:\s*[,–-]\s*\d+)*)\]")


def template(tool: str, qid: str) -> str:
    m = common.manifest(qid)
    papers = "\n".join(f"P{p['n']} = {p['label']}  ({p['txt']})" for p in m["papers"])
    body = (f"<!-- tool: {tool} -->\n<!-- seconds: (how long the answer took, if known) -->\n"
            f"<!-- settings: (model / mode used in the tool) -->\n\n"
            f"# {m['question']}\n\nPapers:\n{papers}\n\n## Answer\n\n\n## Sources\n\n")
    path = os.path.join(common.ANSWERS, tool, f"{qid}.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        print("exists, left as is:", path)
    else:
        with open(path, "w") as f:
            f.write(body)
        print("wrote", path)
    return path


def _numbers(marker: str) -> list:
    """"1, 3-5" -> [1, 3, 4, 5]."""
    out = []
    for part in re.split(r"\s*,\s*", marker.strip()):
        m = re.match(r"(\d+)\s*[–-]\s*(\d+)$", part)
        out += list(range(int(m[1]), int(m[2]) + 1)) if m else [int(part)]
    return out


def parse_md(text: str, tool: str, qid: str, question: str) -> dict:
    meta = dict(re.findall(r"<!--\s*(\w+):\s*(.*?)\s*-->", text))
    answer = text.split("## Answer", 1)[1].split("## Sources", 1)[0].strip()
    sources = {}
    for line in text.split("## Sources", 1)[1].splitlines():
        m = re.match(r"\s*\[(\d+)\]\s*P(\d+)\s*\|\s*(.+)", line)
        if m:
            sources[int(m.group(1))] = {"paper": int(m.group(2)), "passage": m.group(3).strip()}
    sentences, unknown = [], 0
    for s in common.split_sentences(answer):
        cited = [n for mk in _MARK.findall(s) for n in _numbers(mk)]
        unknown += sum(n not in sources for n in cited)
        text = re.sub(r"\s+([.,;:])", r"\1", _MARK.sub("", s)).strip()
        sentences.append({"text": text, "sources": [sources[n] for n in dict.fromkeys(cited) if n in sources]})
    seconds = re.match(r"\d+", meta.get("seconds", ""))
    return {"tool": tool, "question_id": qid, "question": question, "settings": {"as_used": meta.get("settings", "")},
            "seconds": int(seconds.group(0)) if seconds else None, "sentences": sentences,
            "unknown_citations": unknown, "raw": answer}


def parse(tool: str) -> None:
    d = os.path.join(common.ANSWERS, tool)
    for name in sorted(os.listdir(d)):
        if not name.endswith(".md"):
            continue
        qid = name[:-3]
        with open(os.path.join(d, name)) as f:
            text = f.read()
        if not text.split("## Answer", 1)[1].split("## Sources", 1)[0].strip():
            print("not filled in yet:", name)
            continue
        out = parse_md(text, tool, qid, common.manifest(qid)["question"])
        print(f"{name}: {len(out['sentences'])} sentences, "
              f"{sum(len(s['sources']) for s in out['sentences'])} citations -> {common.save_answer(tool, qid, out)}")


if __name__ == "__main__":
    mode, tool, only = sys.argv[1], sys.argv[2], sys.argv[3:]
    if mode == "template":
        for q in common.questions():
            if not only or q["id"] in only:
                template(tool, q["id"])
    else:
        parse(tool)
