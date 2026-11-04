"""Answer each question with PaperQA2 on the same local model and the same
exported texts, and save it in the shared format (answers/paperqa2/<id>.json).
Runs the local model. PaperQA2 lives in its own venv:

    uv venv -p 3.12 .venv-paperqa && uv pip install -p .venv-paperqa/bin/python paper-qa
    .venv-paperqa/bin/python benchmark/compare/run_paperqa.py [QUESTION_ID ...]

Settings are PaperQA2's defaults (10 evidence pieces, up to 5 sources per
answer, 5000-character chunks) except: the model (PQA_LLM, default
ollama/gemma4:26b, for both answering and evidence summaries), keyword
("sparse") retrieval since Ollama has no embedding model installed here
(PQA_EMBEDDING=ollama/nomic-embed-text after `ollama pull nomic-embed-text`
for dense), no image parsing, and each paper's citation given rather than
inferred. PQA_EVIDENCE_K / PQA_MAX_SOURCES override the two answer limits.

Each citation (pqac-xxxxxxxx) maps to the chunk it came from; that chunk is
the source passage. A citation id that matches no chunk is counted in
`unknown_citations`.
"""
import asyncio
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import common  # noqa: E402

_ID = re.compile(r"\bpqac-[a-zA-Z0-9]{8}\b")


def pqa_to_shared(qid: str, question: str, raw: str, contexts: list, settings: dict, seconds: float) -> dict:
    """`contexts`: [{id, n, text, summary}], n = the paper's number."""
    by_id = {c["id"]: c for c in contexts}
    sentences, unknown = [], 0
    for s in common.split_sentences(raw):
        ids = list(dict.fromkeys(_ID.findall(s)))
        sources = []
        for i in ids:
            if i in by_id:
                sources.append({"paper": by_id[i]["n"], "passage": by_id[i]["text"], "summary": by_id[i]["summary"]})
            else:
                unknown += 1
        text = re.sub(r"\s*\((?:[^()]*\bpqac-[^()]*)\)", "", s)      # drop "(pqac-…, pqac-…)"
        text = re.sub(r"\s+([.,;:])", r"\1", _ID.sub("", text)).strip()
        if text:
            sentences.append({"text": text, "sources": sources})
    return {"tool": "paperqa2", "question_id": qid, "question": question, "settings": settings,
            "seconds": round(seconds), "sentences": sentences, "unknown_citations": unknown, "raw": raw}


async def run(qid: str) -> str:
    from paperqa import Docs, Settings
    from paperqa.settings import AnswerSettings, ParsingSettings

    m = common.manifest(qid)
    llm = os.environ.get("PQA_LLM", "ollama/gemma4:26b")
    llm_config = {"name": llm, "model_list": [{"model_name": llm, "litellm_params": {
        "model": llm, "temperature": 0.0, "api_base": os.environ.get("OLLAMA_HOST", "http://localhost:11434"),
        "timeout": 900}}]}
    answer = AnswerSettings(evidence_k=int(os.environ.get("PQA_EVIDENCE_K", 10)),
                            answer_max_sources=int(os.environ.get("PQA_MAX_SOURCES", 5)),
                            max_concurrent_requests=1)
    settings = Settings(llm=llm, llm_config=llm_config, summary_llm=llm, summary_llm_config=llm_config,
                        embedding=os.environ.get("PQA_EMBEDDING", "sparse"), temperature=0.0, answer=answer,
                        parsing=ParsingSettings(use_doc_details=False, multimodal=False))
    t = time.time()
    docs = Docs()
    for p in m["papers"]:
        path = os.path.join(common.PAPERS, qid, p["txt"])
        if os.path.getsize(path):
            await docs.aadd(path, citation=p["label"], docname=f"p{p['n']:02d}", settings=settings)
    session = await docs.aquery(m["question"], settings=settings)
    contexts = [{"id": c.id, "n": int(c.text.doc.docname[1:3]), "text": c.text.text, "summary": c.context}
                for c in session.contexts]
    used = {"llm": llm, "embedding": settings.embedding, "evidence_k": answer.evidence_k,
            "answer_max_sources": answer.answer_max_sources, "chunk_chars": 5000,
            "paper_qa": __import__("importlib.metadata").metadata.version("paper-qa")}
    out = pqa_to_shared(qid, m["question"], session.raw_answer, contexts, used, time.time() - t)
    path = common.save_answer("paperqa2", qid, out)
    print(f"{qid}: {out['seconds']}s, {len(out['sentences'])} sentences -> {path}")
    return path


if __name__ == "__main__":
    for q in common.questions():
        if len(sys.argv) < 2 or q["id"] in sys.argv[1:]:
            asyncio.run(run(q["id"]))
