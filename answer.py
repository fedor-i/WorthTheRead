"""Grounded answers about one paper: "ask this paper" and the auto-summary.

The model answers as a list of short, atomic claims, each with the verbatim
passages it rests on. Then the same quote guard the judge uses checks every
passage against the paper's text: a claim with no verified passage is shown as
UNGROUNDED and never counts toward confidence. With check=True each grounded
claim is also put through the full judge, which catches a real quote stretched
into a claim it doesn't support (overstatement).
"""
import html
import json
import re

from . import config
from . import focus as _focus
from . import judge as _judge
from . import score as _score

_SYSTEM = """You answer a QUESTION about one scientific paper using ONLY the \
PAPER TEXT provided. Never use outside knowledge.

Answer as a list of short, ATOMIC claims — one fact per claim, in plain \
language, in the order a reader needs them. For EACH claim give the passage(s) \
from the paper that establish it, copied VERBATIM, word for word, including \
numbers. Prefer RESULTS and CONCLUSION passages over the abstract. Keep \
numbers, units, populations and caveats exactly as the paper states them; do \
not generalize beyond the passage. If the paper does not address part of the \
question, say so in "not_addressed" instead of guessing.

Return ONLY a JSON object: {"claims": [{"claim": "...", "quotes": ["..."]}], \
"not_addressed": ["..."]}"""

SUMMARY_QUESTION = (
    "Summarize this paper for a researcher: the question it asks, the study "
    "design and population or system, the main results with their numbers, and "
    "the limitations the authors state.")


def _complete(system: str, user: str, max_tokens: int = 1500) -> str:
    """One completion on the configured backend: local Ollama, else the
    Anthropic API / Bedrock."""
    if config.USE_OLLAMA:
        return _judge._ollama_chat(system, user, num_predict=max_tokens)
    client, model = _judge._client_and_model()
    msg = client.messages.create(
        model=model, max_tokens=max_tokens, system=system, temperature=0,
        messages=[{"role": "user", "content": user}])
    return "".join(getattr(b, "text", "") for b in msg.content
                   if getattr(b, "type", None) == "text")


def _parse(raw: str) -> dict:
    raw = (raw or "").strip()
    raw = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", raw).strip()
    s, e = raw.find("{"), raw.rfind("}")
    try:
        return json.loads(raw[s:e + 1]) if s != -1 and e != -1 else {}
    except Exception:
        return _salvage(raw)


def _salvage(raw: str) -> dict:
    """An answer cut off by the token limit is invalid JSON as a whole, but the
    claims before the cut are complete objects: keep those."""
    dec, claims = json.JSONDecoder(), []
    for m in re.finditer(r'\{\s*"claim"', raw):
        try:
            obj, _ = dec.raw_decode(raw, m.start())
            claims.append(obj)
        except ValueError:
            pass
    return {"claims": claims, "truncated": True} if claims else {"truncated": bool(raw)}


def ask(question: str, fetched: dict, check: bool = False) -> dict:
    """Answer `question` from `fetched` (a fetch_text / library.load dict).

    Returns {question, claims: [{claim, status, quotes: [{text, verified}],
    verdict?, confidence?}], not_addressed, confidence, band, grounded,
    ungrounded, source}."""
    out = {"question": question, "claims": [], "not_addressed": [],
           "confidence": 0.0, "band": "Low", "grounded": 0, "ungrounded": 0,
           "source": fetched.get("source"), "abstract_only": not fetched.get("has_fulltext")}
    text = fetched.get("full_text") or ""
    if not text:
        out["not_addressed"] = ["No paper text could be read."]
        return out
    budget = config.OLLAMA_FOCUS_CHARS if config.USE_OLLAMA else 0
    shown = _focus.focus_text(question, text, budget)
    data = _parse(_complete(_SYSTEM, f"QUESTION:\n{question}\n\nPAPER TEXT:\n{shown}"))

    haystack = _judge._norm(text)
    for c in data.get("claims") or []:
        if not isinstance(c, dict) or not (c.get("claim") or "").strip():
            continue
        quotes = [{"text": q, "verified": _judge._quote_verified(q, haystack)}
                  for q in (c.get("quotes") or []) if isinstance(q, str) and q.strip()]
        grounded = any(q["verified"] for q in quotes)
        out["claims"].append({"claim": c["claim"].strip(),
                              "status": "GROUNDED" if grounded else "UNGROUNDED",
                              "quotes": quotes})
    out["not_addressed"] = [s for s in data.get("not_addressed") or []
                            if isinstance(s, str) and s.strip()]

    if check:
        for c in out["claims"]:
            if c["status"] == "GROUNDED":
                r = _judge.judge_claim(c["claim"], fetched)
                c["verdict"], c["confidence"] = r["verdict"], r["confidence"]
                c["verdict_note"] = r.get("distortion_note") or ""

    _score_answer(out, check)
    return out


# --- one question across several papers ---------------------------------------
# Map, then merge. Each paper is asked on its own (the single-paper `ask`, whose
# quotes are checked against that paper), then one call merges the per-paper
# claims into a synthesis. The merge only points at claim ids — it never writes
# quotes — so every passage in the answer is one already verified in the paper
# it's attributed to. A local model given all the papers in one prompt tends to
# answer from the first obvious one; asked one at a time, every paper speaks.

MAX_PAPERS = 12        # the plugin's default when the user doesn't choose
MERGE_BATCH = 24       # findings per merge call; past this a local model starts dropping papers
MAX_ANSWER_CLAIMS = 10  # the merge prompt's "at most 10"; a longer answer is merged once more

_MERGE_SYSTEM = """You merge findings from SEVERAL papers into one answer to a \
QUESTION. You get numbered CLAIMS, each already checked against the paper in \
brackets ([P1], [P2], ...). Use ONLY these claims; never add outside knowledge.

Write at most 10 short answer claims in plain language, ordered so they answer \
the question. When claims from different papers agree, merge them into one \
answer claim that lists all their ids. When papers disagree or differ \
(population, method, estimate, conclusion), keep that visible: a separate \
answer claim per side, or one that states the disagreement and lists both \
sides' ids. Keep numbers and caveats as the claims state them. Every answer \
claim must list the ids it rests on, and must not say more than those claims \
say. If part of the question is answered by no claim, say so in "not_addressed".

Return ONLY a JSON object: {"claims": [{"claim": "...", "from": ["C1", "C4"]}], \
"not_addressed": ["..."]}"""


def _rank_papers(question: str, papers: list) -> list:
    """Indices of `papers` ordered by how much of their text bears on the question."""
    docs = [(p.get("fetched") or {}).get("full_text") or "" for p in papers]
    scores = _focus.bm25_rank(question, [_focus.focus_text(question, d, 4000) for d in docs])
    return sorted(range(len(papers)), key=lambda i: -scores[i])


# A per-paper "finding" that is about the paper, not the question ("The study
# does not state where nitrogen limits growth"): a gap, not a claim.
_META_CLAIM = re.compile(
    r"\b(?:the|this) (?:study|paper|article|text|excerpt|authors?|review|abstract)\b[^.]*?"
    r"\b(?:does|do|did)(?: not|n't)(?: explicitly| directly| clearly)?"
    r" (?:state|mention|specify|say|address|discuss|provide|report|describe|identify)\b", re.I)


def _merge(question: str, chosen: list, found: list, items: list) -> tuple:
    """One merge call. `items` are (text, {finding ids}); returns the merged
    (text, {finding ids}) — each resting on the findings of the items it
    names — and the model's parsed reply."""
    listing = "\n".join(
        f"C{i} [{', '.join(f'P{n}' for n in sorted({found[j][0] for j in ids}))}] {text}"
        for i, (text, ids) in enumerate(items, 1))
    papers = sorted({found[j][0] for _, ids in items for j in ids})
    titles = "\n".join(f"[P{n}] {chosen[n - 1].get('label', '')}" for n in papers)
    try:
        data = _parse(_complete(_MERGE_SYSTEM, f"QUESTION:\n{question}\n\nPAPERS:\n{titles}"
                                f"\n\nCLAIMS:\n{listing}", max_tokens=2000))
    except Exception:
        data = {}
    merged = []
    for c in data.get("claims") or []:
        if not isinstance(c, dict) or not (c.get("claim") or "").strip():
            continue
        ids = set()
        for ref in c.get("from") or []:
            m = re.search(r"\d+", str(ref))
            if m and 1 <= int(m.group(0)) <= len(items):
                ids |= items[int(m.group(0)) - 1][1]
        # The model echoes "[P1, P3]"; the citations come from the ids.
        merged.append((_strip_ids(c["claim"]), ids))
    return merged, data


_SUPPORT_SYSTEM = """You check which FINDINGS back a SENTENCE. A finding backs \
it only if, on its own, it says what the sentence says about the same thing: \
the same place or region, population, nutrient and outcome. A finding on the \
same topic but about a different region, organism or result does NOT back it. \
Return ONLY a JSON object: {"support": ["C1", "C3"]} — an empty list if none do."""


def _supporting(sentence: str, found: list, ids: set) -> set:
    """The ids, among `ids`, whose findings back `sentence` on their own. The
    merge groups findings by topic, so a sentence about one region can pick up
    findings about another; one short call per sentence prunes them. If the
    check fails to answer, the merge's ids stand."""
    ordered = sorted(ids)
    listing = "\n".join(f"C{k} {found[i][1]}" for k, i in enumerate(ordered, 1))
    try:
        data = _parse(_complete(_SUPPORT_SYSTEM, f"SENTENCE:\n{sentence}\n\nFINDINGS:\n{listing}",
                                max_tokens=200))
    except Exception:
        return ids
    if not isinstance(data.get("support"), list):
        return ids
    keep = set()
    for ref in data["support"]:
        m = re.search(r"\d+", str(ref))
        if m and 1 <= int(m.group(0)) <= len(ordered):
            keep.add(ordered[int(m.group(0)) - 1])
    return keep


_PART_SYSTEM = """You check which FINDINGS back a SENTENCE, wholly or in part. \
For each finding answer "all" if, on its own, it says what the whole sentence \
says; "part" if it says what one distinct part of the sentence says, about the \
same place or region, population, nutrient and outcome; "none" if it is about a \
different region, organism or result, or only on the same topic. For "part", \
copy the words of the sentence that it backs, exactly as they appear.

Return ONLY a JSON object: {"findings": [{"id": "C1", "backs": "all"}, \
{"id": "C2", "backs": "part", "part": "exact words from the sentence"}, \
{"id": "C3", "backs": "none"}]}"""


def _supporting_parts(sentence: str, found: list, ids: set) -> tuple:
    """Like _supporting, but a finding that backs one part of the sentence is
    kept too, with that part. Returns ({kept ids}, {id: part of the sentence});
    a part not found verbatim in the sentence is dropped (the source is kept,
    cited at the end). If the check fails to answer, the merge's ids stand."""
    ordered = sorted(ids)
    listing = "\n".join(f"C{k} {found[i][1]}" for k, i in enumerate(ordered, 1))
    try:
        data = _parse(_complete(_PART_SYSTEM, f"SENTENCE:\n{sentence}\n\nFINDINGS:\n{listing}",
                                max_tokens=600))
    except Exception:
        return ids, {}
    if not isinstance(data.get("findings"), list):
        return ids, {}
    keep, parts = set(), {}
    for f in data["findings"]:
        if not isinstance(f, dict):
            continue
        m = re.search(r"\d+", str(f.get("id", "")))
        if not (m and 1 <= int(m.group(0)) <= len(ordered)):
            continue
        i, backs = ordered[int(m.group(0)) - 1], str(f.get("backs", "")).lower()
        if backs in ("all", "part"):
            keep.add(i)
            part = (f.get("part") or "").strip().strip('"').rstrip(".,;")
            if backs == "part" and part and part in sentence and part != sentence.rstrip("."):
                parts[i] = part
    return keep, parts


def _strip_ids(text: str) -> str:
    """Drop the ids the model echoes ("[P1, P3]", "[C6]", "[G2]"); the
    citations come from the ids it listed, not from the text."""
    return re.sub(r"\s*\[(?:[PCG]\d+[,;\s]*)+\]", "", text).strip()


# A figure, not a label: skip digits stuck to letters (CO2, Fe3+) and the
# "type 2" / "phase 3" kind of name, which findings often leave implicit.
_NUMBER = re.compile(r"(?<![A-Za-z\d.,])(?<!type )(?<!phase )(?<!stage )(?<!grade )\d+(?:[.,]\d+)*", re.I)


def _numbers(text: str) -> set:
    return {n.replace(",", "") for n in _NUMBER.findall(text.replace("\u2212", "-"))}


def _unsourced_numbers(sentence: str, texts: list) -> list:
    """Numbers in a merged sentence that appear in none of its findings or
    quotes. The merge rewrites findings, and a local model can garble a figure
    on the way ("24%" became "2ly%"); nothing else checks the numbers."""
    have = set().union(*(_numbers(t) for t in texts)) if texts else set()
    return sorted(_numbers(sentence) - have, key=sentence.find)


_ANOTHER_STUDY = re.compile(
    r"\b(?:another|a second|a separate|a different|a further|other)\s+"
    r"(?:study|paper|trial|meta-analysis|review|analysis|cohort|report)s?\s+(?:also\s+)?"
    r"(?:found|finds|showed|shows|reported|reports|observed|suggested|suggests|indicated|"
    r"indicates|concluded|demonstrated)\b", re.I)


def _more_studies_than_papers(sentence: str, papers: int) -> list:
    """The merge likes "one meta-analysis found …; another study found …" even
    when every finding is from one paper, and the model check misses it. A
    sentence naming more studies than it cites papers is flagged; "in a second
    study" (a method, not a source) does not count."""
    said = _ANOTHER_STUDY.findall(sentence)
    if not said or len(said) + 1 <= papers:
        return []
    return [{"words": said[0], "why": f"it reads as {len(said) + 1} studies, but its findings come from "
                                      f"{papers} paper{'s' if papers != 1 else ''}"}]


_FAITHFUL_SYSTEM = """You check a SENTENCE that was written from FINDINGS; each \
finding names the paper it comes from. Rewording, shortening and combining \
findings is fine. Report only where the sentence says something its findings \
do not:
- a number that differs from the findings;
- a result given for a different group, subgroup, comparison or place than \
the finding gives it for;
- a reversed direction (more/less, greater/lesser, increase/decrease);
- stronger or weaker certainty (significant vs not significant, shows vs \
suggests);
- results from ONE paper described as different studies, or results from \
different papers described as one study.
For each problem copy the words of the sentence that are wrong, exactly as \
they appear, and say briefly why.

Return ONLY a JSON object: {"problems": [{"words": "...", "why": "..."}]} — \
an empty list if the sentence says only what its findings say."""


def _unfaithful(sentence: str, found: list, ids: set, chosen: list) -> list:
    """Where `sentence` says more than, or other than, its findings: the
    merge can move a figure to the wrong subgroup, reverse a direction, or
    turn one paper into "another study", and the sources still look right.
    Returns [{"words", "why"}] ("words" is "" when the model's words are not
    in the sentence); [] if it is faithful or the check fails to answer."""
    label = lambda n: chosen[n - 1].get("label", f"paper {n}").split(" — ")[0]
    listing = "\n".join(f"C{k} ({label(found[i][0])}) {found[i][1]}" for k, i in enumerate(sorted(ids), 1))
    try:
        data = _parse(_complete(_FAITHFUL_SYSTEM, f"SENTENCE:\n{sentence}\n\nFINDINGS:\n{listing}",
                                max_tokens=400))
    except Exception:
        return []
    out = []
    for p in data.get("problems") or []:
        if isinstance(p, dict) and str(p.get("why") or "").strip():
            words = str(p.get("words") or "").strip().strip('"')
            out.append({"words": words if words and words in sentence else "", "why": str(p["why"]).strip()})
    return out


def _batches(items: list, found: list, size: int) -> list:
    """Consecutive groups of at most `size` items, never splitting one paper's
    findings when a group can take them whole."""
    runs = []                      # consecutive items from the same paper
    for item in items:
        n = min((found[j][0] for j in item[1]), default=0)
        if runs and runs[-1][0] == n:
            runs[-1][1].append(item)
        else:
            runs.append((n, [item]))
    groups, cur = [], []
    for _, run in runs:
        if cur and len(cur) + len(run) > size:
            groups.append(cur)
            cur = []
        for item in run:
            if len(cur) >= size:
                groups.append(cur)
                cur = []
            cur.append(item)
    return groups + ([cur] if cur else [])


def _map(question: str, chosen: list, report) -> tuple:
    """Ask each paper on its own. Returns (found, gaps, paper infos); found is
    [(paper n, claim text, [verified quotes])]."""
    found, gaps, infos = [], [], []
    for n, p in enumerate(chosen, 1):
        report(stage="paper", paper=n, papers=len(chosen), label=p.get("label", ""))
        info = {"n": n, "label": p.get("label", ""), "key": p.get("key", ""), "cited": 0,
                "used": False, "abstract_only": not p["fetched"].get("has_fulltext")}
        try:
            r = ask(question, p["fetched"])
            for c in r["claims"]:
                quotes = [q["text"] for q in c["quotes"] if q["verified"]]
                if _META_CLAIM.search(c["claim"]):
                    gaps.append(c["claim"])
                elif quotes:
                    found.append((n, c["claim"], quotes))
            gaps += r["not_addressed"]
        except Exception as e:
            info["error"] = f"{type(e).__name__}: {e}"
        infos.append(info)
    return found, gaps, infos


def _combine_merge(question: str, chosen: list, found: list, report, parts: bool = True) -> tuple:
    """Merge the findings into answer sentences, in parts when there are many
    (each round merges groups of at most MERGE_BATCH, until one call can take
    the rest), then check each sentence's findings. With `parts` (the
    default) a finding that backs one part of the sentence stays, cited at
    that part; without, only findings that back the whole sentence stay.
    Returns
    ([(sentence, {finding ids}, {id: part})], not_addressed, truncated)."""
    truncated = False
    items, rnd = [(text, {i}) for i, (_, text, _) in enumerate(found)], 0
    while len(items) > MERGE_BATCH:
        rnd += 1
        chunks, nxt = _batches(items, found, MERGE_BATCH), []
        for k, chunk in enumerate(chunks, 1):
            report(stage="merge", papers=len(chosen), claims=len(found), part=k, parts=len(chunks), round=rnd)
            res, data = _merge(question, chosen, found, chunk)
            truncated = truncated or bool(data.get("truncated"))
            # A part that failed carries its own findings forward.
            nxt += [r for r in res if r[1]] or chunk
        if len(nxt) >= len(items):        # no progress: stop shrinking
            break
        items = nxt
    report(stage="merge", papers=len(chosen), claims=len(found))
    final, data = _merge(question, chosen, found, items)
    truncated = truncated or bool(data.get("truncated"))
    if len(final) > MAX_ANSWER_CLAIMS:
        # The model sometimes ignores "at most 10" and echoes the findings
        # nearly one by one; one more merge over its own sentences fixes that.
        report(stage="merge", papers=len(chosen), claims=len(final))
        again, data2 = _merge(question, chosen, found, [(t, ids) for t, ids in final if ids])
        if again and len(again) < len(final):
            final, data = again, data2
    checked = []
    for k, (text, ids) in enumerate(final, 1):
        where = {}
        if len(ids) > 1:
            report(stage="check", claim=k, claims=len(final))
            ids, where = _supporting_parts(text, found, ids) if parts else (_supporting(text, found, ids), {})
        checked.append((text, ids, where))
    return checked, _strings(data.get("not_addressed")), truncated


def _strings(xs) -> list:
    return [x for x in xs or [] if isinstance(x, str) and x.strip()]


# --- group first, then write (experimental) -------------------------------------
# The merge above writes sentences and then points at findings, so a sentence can
# pick up findings that are merely on its topic. Here the findings are grouped
# first — same point about the same place, population and nutrient — and one
# sentence is written per group, so a sentence's sources are its group by
# construction (Slobodkin et al. 2024, "Attribute First, then Generate").

_GROUP_SYSTEM = """You sort FINDINGS from several papers into groups, to answer \
a QUESTION. Put findings in the same group only when they make the same point \
about the same place or region, population, nutrient and outcome (they agree, \
or one adds detail to the other). Findings about different regions, nutrients \
or results go in different groups, even when the topic is similar. Findings \
that contradict each other about the same thing go in separate groups. Every \
finding goes in exactly one group; a finding unlike any other is a group of its own.

Return ONLY a JSON object: {"groups": [{"ids": ["C1", "C4"], "point": "short label"}]}"""

_WRITE_SYSTEM = """You write an answer to a QUESTION from GROUPS of findings. \
For each group you use, write ONE short plain-language sentence that says only \
what that group's findings say: you may combine them, keep their numbers, \
places and caveats, and never add outside knowledge. Choose and order the groups \
that answer the question best, at most 10; skip groups that don't bear on it. \
If part of the question is answered by no group, say so in "not_addressed".

Return ONLY a JSON object: {"claims": [{"group": "G1", "claim": "..."}], "not_addressed": ["..."]}"""


def _combine_grouped(question: str, chosen: list, found: list, report) -> tuple:
    """Group the findings, then write one sentence per group. Returns
    ([(sentence, {finding ids})], not_addressed, truncated)."""
    report(stage="group", papers=len(chosen), claims=len(found))
    listing = "\n".join(f"C{i} [P{n}] {text}" for i, (n, text, _) in enumerate(found, 1))
    try:
        data = _parse(_complete(_GROUP_SYSTEM, f"QUESTION:\n{question}\n\nFINDINGS:\n{listing}",
                                max_tokens=1500))
    except Exception:
        data = {}
    groups, seen = [], set()
    for g in data.get("groups") or []:
        ids = []
        for ref in (g.get("ids") or []) if isinstance(g, dict) else []:
            m = re.search(r"\d+", str(ref))
            i = int(m.group(0)) - 1 if m else -1
            if 0 <= i < len(found) and i not in seen:
                ids.append(i)
                seen.add(i)
        if ids:
            groups.append(ids)
    groups += [[i] for i in range(len(found)) if i not in seen]   # the model's leftovers
    truncated = bool(data.get("truncated"))

    report(stage="merge", papers=len(chosen), claims=len(found))
    listing = "\n\n".join(f"G{k}:\n" + "\n".join(f"  [P{found[i][0]}] {found[i][1]}" for i in ids)
                           for k, ids in enumerate(groups, 1))
    try:
        data = _parse(_complete(_WRITE_SYSTEM, f"QUESTION:\n{question}\n\nGROUPS:\n{listing}",
                                max_tokens=2000))
    except Exception:
        data = {}
    final, used = [], set()
    for c in data.get("claims") or []:
        if not isinstance(c, dict) or not (c.get("claim") or "").strip():
            continue
        m = re.search(r"\d+", str(c.get("group", "")))
        k = int(m.group(0)) - 1 if m else -1
        if 0 <= k < len(groups) and k not in used:
            used.add(k)
            final.append((_strip_ids(c["claim"]), set(groups[k]), {}))
    return final, _strings(data.get("not_addressed")), truncated or bool(data.get("truncated"))


def _combine_grouped_parts(question: str, chosen: list, found: list, report) -> tuple:
    """Group first, then the part check on each written sentence: the group
    proposes the sources, the check drops any the sentence doesn't use."""
    final, not_addressed, truncated = _combine_grouped(question, chosen, found, report)
    checked = []
    for k, (text, ids, _) in enumerate(final, 1):
        where = {}
        if len(ids) > 1:
            report(stage="check", claim=k, claims=len(final))
            ids, where = _supporting_parts(text, found, ids)
        checked.append((text, ids, where))
    return checked, not_addressed, truncated


COMBINE = {"merge": _combine_merge, "grouped": _combine_grouped,
           "merge_whole": lambda *a: _combine_merge(*a, parts=False),
           "grouped_parts": _combine_grouped_parts}


def ask_many(question: str, papers: list, progress=None, max_papers: int = None,
             method: str = "merge") -> dict:
    """Answer `question` from several papers at once.

    `papers` is a list of {"label", "fetched", "pages"?, "key"?}. Returns
    {question, claims: [{claim, status, sources: [{paper, quote, verified,
    page}]}], papers: [{n, label, key, cited, abstract_only, used, error?}],
    skipped, left_out: [{label, key, reason}], not_addressed, merged,
    confidence, band, grounded, ungrounded}. Papers beyond `max_papers` (the
    least relevant to the question) are left out, reason "limit"; None reads all.
    However many findings come back, no single merge call sees more than
    MERGE_BATCH of them: larger sets are merged in parts, then the parts merged.
    Each merged claim that rests on several findings is then checked against
    them, and keeps those that back it wholly or in part (each source carries
    the `part` of the sentence it backs, or None); a claim none back is
    UNGROUNDED. `method` picks another way to combine (see COMBINE)."""
    report = lambda **kw: progress and progress(kw)
    readable = [p for p in papers if (p.get("fetched") or {}).get("full_text")]
    out = {"question": question, "claims": [], "not_addressed": [], "papers": [],
           "skipped": [p.get("label", "") for p in papers if p not in readable],
           "left_out": [{"label": p.get("label", ""), "key": p.get("key", ""), "reason": "no_text"}
                        for p in papers if p not in readable],
           "merged": False, "confidence": 0.0, "band": "Low", "grounded": 0, "ungrounded": 0}
    if not readable:
        out["not_addressed"] = ["None of the papers had readable text."]
        return out
    order = _rank_papers(question, readable)
    max_papers = len(readable) if not max_papers else max(1, int(max_papers))
    out["skipped"] += [readable[i].get("label", "") for i in order[max_papers:]]
    out["left_out"] += [{"label": readable[i].get("label", ""), "key": readable[i].get("key", ""),
                         "reason": "limit"} for i in order[max_papers:]]
    chosen = [readable[i] for i in sorted(order[:max_papers])]   # keep the user's order

    found, gaps, out["papers"] = _map(question, chosen, report)

    def source(n, quote):
        return {"paper": n, "quote": quote, "verified": True, "reattributed": False,
                "page": _library_page(quote, chosen[n - 1].get("pages"))}

    merged, used = [], set()
    if len(found) > 1:
        final, out["not_addressed"], out["truncated"] = COMBINE[method](question, chosen, found, report)
        for k, (text, ids, where) in enumerate(final, 1):
            sources = [dict(source(found[i][0], q), part=where.get(i)) for i in sorted(ids) for q in found[i][2]]
            used |= ids
            numbers = _unsourced_numbers(text, [t for i in ids for t in [found[i][1], *found[i][2]]])
            problems = []
            if ids:
                report(stage="wording", claim=k, claims=len(final))
                problems = _more_studies_than_papers(text, len({found[i][0] for i in ids}))
                # A figure the number guard already flags needs no second warning.
                problems += [p for p in _unfaithful(text, found, ids, chosen)
                             if not (p["words"] and _numbers(p["words"]) & set(numbers))]
            merged.append({"claim": text, "sources": sources, "extra": False,
                           "status": "GROUNDED" if sources else "UNGROUNDED",
                           "unsourced_numbers": numbers, "problems": problems})
        out["merged"] = bool(merged)
    else:
        out["not_addressed"] = gaps[:3]
    # Per-paper findings the merge didn't use are kept, not dropped.
    for i, (n, text, quotes) in enumerate(found):
        if i not in used:
            merged.append({"claim": text, "sources": [source(n, q) for q in quotes],
                           "status": "GROUNDED", "extra": out["merged"]})
    out["claims"] = merged

    for c in merged:
        for n in {s["paper"] for s in c["sources"]}:
            out["papers"][n - 1]["cited"] += 1
    for info in out["papers"]:
        info["used"] = info["cited"] > 0
    out["grounded"] = sum(1 for c in merged if c["status"] == "GROUNDED")
    out["ungrounded"] = len(merged) - out["grounded"]
    if merged:
        # Passages are real, but nothing has judged each claim against them:
        # cap below "High", as for a single unchecked answer.
        conf = min(0.7, out["grounded"] / len(merged))
        if all(p["abstract_only"] for p in out["papers"] if p["used"]):
            conf = min(conf, 0.5)
        out["confidence"] = round(conf, 2)
        out["band"] = _judge.band(out["confidence"])
    return out


def _library_page(quote: str, pages):
    if not pages:
        return None
    from . import library
    return library.page_of(quote, pages)


def _score_answer(out: dict, checked: bool) -> None:
    claims = out["claims"]
    out["grounded"] = sum(1 for c in claims if c["status"] == "GROUNDED")
    out["ungrounded"] = len(claims) - out["grounded"]
    if not claims:
        return
    if checked:
        # Grounded claims count by their verdict; ungrounded ones count as 0.
        conf = sum(_score._VERDICT_WEIGHT.get(c.get("verdict"), 0.0) * (c.get("confidence") or 0)
                   for c in claims) / len(claims)
    else:
        # Unchecked: we only know the passages are real, not that each claim
        # follows from them, so cap below "High".
        conf = min(0.7, out["grounded"] / len(claims))
    if out["abstract_only"]:
        conf = min(conf, 0.5)
    out["confidence"] = round(conf, 2)
    out["band"] = _judge.band(out["confidence"])


def summarize(fetched: dict, check: bool = False) -> dict:
    return ask(SUMMARY_QUESTION, fetched, check=check)


def note_html(result: dict, title: str = "") -> str:
    """A Zotero-note-ready HTML rendering: grounded claims with their quotes
    and pages; ungrounded claims are listed separately, clearly marked."""
    esc = html.escape
    parts = [f"<h1>{esc(title or 'Worth Your Read summary')}</h1>",
             f"<p><b>Confidence:</b> {result.get('band')} ({result.get('confidence'):.2f}) · "
             f"{result.get('grounded')} grounded, {result.get('ungrounded')} ungrounded"
             + (" · abstract only" if result.get("abstract_only") else "") + "</p>"]
    grounded = [c for c in result["claims"] if c["status"] == "GROUNDED"]
    ungrounded = [c for c in result["claims"] if c["status"] != "GROUNDED"]
    if grounded:
        parts.append("<ul>")
        for c in grounded:
            v = f" <i>[{esc(c['verdict'])}]</i>" if c.get("verdict") else ""
            parts.append(f"<li>{esc(c['claim'])}{v}")
            for q in c["quotes"]:
                if q["verified"]:
                    pg = f" (p. {q['page']})" if q.get("page") else ""
                    parts.append(f"<blockquote>“{esc(q['text'])}”{pg}</blockquote>")
            parts.append("</li>")
        parts.append("</ul>")
    if ungrounded:
        parts.append("<p><b>Not verified against the text</b> (no passage matched):</p><ul>")
        parts.extend(f"<li>{esc(c['claim'])}</li>" for c in ungrounded)
        parts.append("</ul>")
    if result.get("not_addressed"):
        parts.append("<p><b>Not addressed by the paper:</b> "
                     + esc("; ".join(result["not_addressed"])) + "</p>")
    return "\n".join(parts)
