"""Label every (answer sentence, cited passage) pair with one judge, the same
for every tool, with the rubric of multi_source_gold.jsonl:

  backs   the passage alone supports the whole sentence
  partly  it supports one distinct part of it (same place, population, outcome)
  no      a different region, population or result; same topic only; or it
          contradicts the sentence

and, per sentence, whether its passages TOGETHER support all of it (yes /
partly / no). The judge never sees which tool wrote the answer. One call per
cited sentence; results are cached in judged/<judge>/<tool>/<id>.json, so a
re-run only judges what is new.

The judge is Claude on Bedrock (independent of the local model all the tools
share): WYR_USE_OLLAMA=0 WYR_USE_BEDROCK=1 AWS_REGION=us-east-1
WYR_BEDROCK_MODEL=us.anthropic.claude-opus-4-5-20251101-v1:0 and the bearer
key in AWS_BEARER_TOKEN_BEDROCK.

    python benchmark/compare/judge_pairs.py calibrate   # judge vs the 165 hand labels first
    python benchmark/compare/judge_pairs.py run [TOOL ...]
"""
import json
import os
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(HERE))))

from worth_your_read import answer  # noqa: E402
from worth_your_read.benchmark.compare import common  # noqa: E402
from worth_your_read.benchmark.multi_source_score import load_gold  # noqa: E402

JUDGED = os.path.join(HERE, "judged")
MAX_PASSAGE = 6000          # PaperQA2's chunks are 5000 characters

SYSTEM = """You check whether cited PASSAGES from scientific papers support a \
SENTENCE from an answer. For each passage answer:
- "backs": the passage on its own supports everything the sentence says;
- "partly": it supports one distinct part of the sentence, about the same \
place or region, population, intervention and outcome;
- "no": it is about a different region, population or result, is only on the \
same topic, or contradicts the sentence.
Numbers, directions (more/less), certainty (significant or not) and the group \
a result applies to must match. Then say whether the passages TOGETHER \
support everything the sentence says: "yes", "partly" (some of it is in none \
of them) or "no".

Return ONLY a JSON object: {"passages": [{"id": "S1", "label": "backs"}, \
{"id": "S2", "label": "no"}], "together": "partly", \
"unsupported": "the words of the sentence that no passage supports, or empty"}"""


def judge_model() -> str:
    from worth_your_read import config
    if config.USE_OLLAMA:
        return "ollama-" + os.environ.get("WYR_OLLAMA_MODEL", "local").replace(":", "-").replace("/", "-")
    return os.environ.get("WYR_BEDROCK_MODEL", "api").split(".")[-1].split("-v")[0] or "api"


def judge_sentence(sentence: str, passages: list) -> dict:
    """{"labels": [label per passage], "together", "unsupported"}; labels are
    None where the judge gave no usable answer."""
    listing = "\n\n".join(f"S{k} {p[:MAX_PASSAGE]}" for k, p in enumerate(passages, 1))
    raw = answer._complete(SYSTEM, f"SENTENCE:\n{sentence}\n\nPASSAGES:\n{listing}", max_tokens=800)
    data = answer._parse(raw)
    labels = [None] * len(passages)
    for item in data.get("passages") or []:
        if not isinstance(item, dict):
            continue
        digits = "".join(ch for ch in str(item.get("id", "")) if ch.isdigit())
        label = str(item.get("label", "")).lower()
        if digits and 1 <= int(digits) <= len(passages) and label in ("backs", "partly", "no"):
            labels[int(digits) - 1] = label
    together = str(data.get("together", "")).lower()
    return {"labels": labels, "together": together if together in ("yes", "partly", "no") else None,
            "unsupported": str(data.get("unsupported") or "")}


def run(tools: list) -> None:
    for a in common.load_answers():
        if tools and a["tool"] not in tools:
            continue
        path = os.path.join(JUDGED, judge_model(), a["tool"], f"{a['question_id']}.json")
        done = {}
        if os.path.exists(path):
            with open(path) as f:
                done = {j["text"]: j for j in json.load(f)["sentences"]}
        out = []
        for s in a["sentences"]:
            passages = [src["passage"] for src in s["sources"]]
            prev = done.get(s["text"])
            if prev and len(prev["labels"]) == len(passages) and None not in prev["labels"]:
                out.append(prev)
                continue
            j = judge_sentence(s["text"], passages) if passages else {"labels": [], "together": "no", "unsupported": ""}
            out.append({"text": s["text"], **j})
            print(f"{a['tool']} {a['question_id']}: {j['labels']} together={j['together']}", flush=True)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump({"judge": judge_model(), "tool": a["tool"], "question_id": a["question_id"],
                       "sentences": out}, f, indent=1, ensure_ascii=False)


def calibrate() -> None:
    """Agreement with the hand labels (multi_source_gold.jsonl), keep vs no."""
    by_claim = {}
    for g in load_gold():
        by_claim.setdefault(g["claim"], []).append(g)
    conf, exact = Counter(), 0
    for claim, pairs in by_claim.items():
        j = judge_sentence(claim, [g["quote"] for g in pairs])
        for g, label in zip(pairs, j["labels"]):
            conf[(g["gold"], label)] += 1
            exact += g["gold"] == label
    keep = lambda x: x in ("backs", "partly")
    n = sum(conf.values())
    agree = sum(v for (g, p), v in conf.items() if keep(g) == keep(p))
    print(f"judge {judge_model()}: keep/no agreement {agree}/{n} = {agree / n:.1%}; exact label {exact}/{n}")
    print("hand label -> judge label:", {f"{g}->{p}": v for (g, p), v in sorted(conf.items(), key=str)})
    os.makedirs(JUDGED, exist_ok=True)
    with open(os.path.join(JUDGED, f"calibration_{judge_model()}.json"), "w") as f:
        json.dump({f"{g}->{p}": v for (g, p), v in conf.items()}, f, indent=1)


if __name__ == "__main__":
    if sys.argv[1] == "calibrate":
        calibrate()
    else:
        run(sys.argv[2:])
