"""Research mode: snowball through the citation graph until it runs dry.

Seeds come from keyword discovery on the question (and/or DOIs you give, for
"find similar"). Then, round after round, for every paper accepted in the last
round we collect the papers it REFERENCES and the papers that CITE it, screen
those candidates for relevance to the question, and accept the relevant ones
into the next frontier. It stops when a round accepts nothing new — the search
is exhausted — or when a cap is hit, and says which.

Screening is two-stage so it stays cheap on a laptop:
  1. lexical: BM25 of the question against title + abstract, relative to the
     seeds' own scores, plus how many accepted papers point at the candidate;
  2. the model: batches of titles + abstract openings, "which are relevant?".
Only OpenAlex ids and the question's keywords leave the machine.
"""
import json
import re
import statistics

from . import config
from . import discover as _discover
from . import focus as _focus
from . import plan as _plan
from . import semantic as _semantic
from . import store as _store

OPENALEX = _discover.OPENALEX
_SELECT = ("id,doi,display_name,publication_year,cited_by_count,type,is_retracted,"
           "open_access,abstract_inverted_index,referenced_works")


# --- OpenAlex access (module-level so tests can replace them) ------------------

def _work(w: dict) -> dict:
    return {
        "id": (w.get("id") or "").split("/")[-1],
        "doi": _discover.clean_doi(w.get("doi")),
        "title": w.get("display_name") or "",
        "year": w.get("publication_year"),
        "cited_by": w.get("cited_by_count") or 0,
        "type": w.get("type") or "",
        "is_oa": bool((w.get("open_access") or {}).get("is_oa")),
        "retracted": bool(w.get("is_retracted")),
        "abstract": _discover._abstract_from_index(w.get("abstract_inverted_index"))[:1500],
        "refs": [r.split("/")[-1] for r in w.get("referenced_works") or []],
    }


# OpenAlex often holds one paper as several works (journal version, reprint,
# preprint), plus correction notices that carry its title; collapse them by title.
_NOTICE_RE = re.compile(r"^(correction|erratum|corrigendum|retraction|retracted|"
                        r"expression of concern)\b", re.I)


def _title_key(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()


# Requests that failed this run (other than rate limiting, which stops the run).
# A run with failures is reported as incomplete, never as exhausted.
_errors = []


def _openalex(params: dict, cache_key: str):
    """One OpenAlex request, served from the on-disk cache when possible.
    RateLimited propagates; any other failure is recorded and returns None."""
    hit = _store.get_response(cache_key)
    if hit is not None:
        return hit
    try:
        j = _discover._get_json(OPENALEX, params)
    except _discover.RateLimited:
        raise
    except Exception as e:
        _errors.append(f"{type(e).__name__}: {e}")
        return None
    _store.put_response(cache_key, j)
    return j


def fetch_works(ids: list = (), dois: list = ()) -> list:
    """Batch-fetch works by OpenAlex id or DOI (50 per request; cached)."""
    out = []
    for field, vals in (("openalex_id", sorted(set(ids))),
                        ("doi", sorted({_discover.clean_doi(d) for d in dois if d}))):
        for i in range(0, len(vals), 50):
            chunk = vals[i:i + 50]
            j = _openalex({"filter": f"{field}:" + "|".join(chunk), "per-page": 50,
                           "select": _SELECT}, f"oa:works:{field}:" + "|".join(chunk))
            out.extend(_work(w) for w in (j or {}).get("results") or [])
    return out


def add_paper_facts(papers: list, doi=lambda p: p.get("doi")) -> None:
    """Set each paper's `cited_by` and `review` (answer.ask_many reads both)
    from OpenAlex. Offline or unknown papers are left without them."""
    try:
        works = {w["doi"]: w for w in fetch_works(dois=[doi(p) for p in papers if doi(p)]) if w.get("doi")}
    except Exception:
        return
    for p in papers:
        w = works.get(_discover.clean_doi(doi(p)))
        if w:
            p["cited_by"], p["review"] = w["cited_by"], w["type"] == "review"


def fetch_citing(work_ids: list, per_paper: int) -> list:
    """Works citing any of `work_ids`, most-cited first: one request per 25
    papers (OpenAlex ORs the ids), up to `per_paper` results each on average.
    Which of them a result cites is read from its own reference list."""
    out, ids = [], sorted(set(work_ids))
    for i in range(0, len(ids), 25):
        chunk = ids[i:i + 25]
        n = min(200, per_paper * len(chunk))
        j = _openalex({"filter": "cites:" + "|".join(chunk), "sort": "cited_by_count:desc",
                       "per-page": n, "select": _SELECT}, f"oa:citing:{'|'.join(chunk)}:{n}")
        out.extend(_work(w) for w in (j or {}).get("results") or [])
    return out


def seed_works(question: str, seed_dois: list, per_query: int) -> list:
    """Keyword discovery over the plan's query variants, plus given DOIs."""
    dois = list(seed_dois or [])
    if question and per_query:
        for q in _plan.plan(question, {})["queries"]:
            dois += [c["doi"] for c in _discover.discover_sources(q, n=per_query, rounds=1)
                     if c.get("doi")]
    return fetch_works(dois=list(dict.fromkeys(dois)))


# --- screening -------------------------------------------------------------------

_SCREEN_SYSTEM = """You screen papers for a systematic literature search on ONE \
RESEARCH QUESTION. Score each numbered paper (title, year, start of abstract) \
for how directly it helps answer THAT question. First name the question's \
intervention (or exposure) and its OUTCOME; judge each paper by that outcome, \
not by whether its result is favourable.
3 = studies the question itself: the same intervention or exposure AND the same \
outcome (a trial, cohort, meta-analysis or review of exactly that).
2 = the same intervention and the same OUTCOME in another population or model \
(animals, worms, cells), or a study of the mechanism by which the intervention \
would produce that outcome.
1 = same intervention but a different outcome or purpose: its effect on one \
particular disease, organ, injury or side effect rather than the outcome asked \
about, or background on the intervention.
A study in patients who have some disease still scores 2-3 if it measures the \
question's outcome itself (e.g. all-cause mortality for a question about \
lifespan); survival from that one disease (cancer survival, cardiac death) is a \
different outcome and scores 1.
0 = unrelated, or only shares a word.
Return ONLY JSON: {"scores": {"<number>": <0-3>, ...}}"""


def model_screen(question: str, papers: list) -> dict:
    """{index: relevance 0-3} from the model. Raises if no model."""
    from .answer import _complete
    lines = [f"{i + 1}. {p['title']} ({p.get('year') or 'n.d.'}) — {p['abstract'][:280]}"
             for i, p in enumerate(papers)]
    raw = _complete(_SCREEN_SYSTEM, f"RESEARCH QUESTION:\n{question}\n\nPAPERS:\n" + "\n".join(lines))
    m = re.search(r"\{.*\}", raw or "", re.S)
    scores = json.loads(m.group(0)).get("scores", {}) if m else {}
    out = {}
    for k, v in scores.items():
        if str(k).isdigit() and 0 < int(k) <= len(papers):
            try:
                out[int(k) - 1] = int(v)
            except (TypeError, ValueError):
                pass
    return out


class Lexical:
    """How much of the question a paper's title + abstract covers: the share of
    the question's terms present, each weighted by its rarity (idf) across ALL
    candidates seen so far. Unlike BM25 over one batch, scores stay comparable
    from round to round, which the rising relevance bar depends on."""

    def __init__(self, question: str):
        self.terms = set(_focus.tokens(question))
        self.df, self.n = {}, 0

    def score(self, papers: list) -> list:
        import math
        docs = [set(_focus.tokens(f"{p['title']} {p['abstract']}")) for p in papers]
        for d in docs:
            self.n += 1
            for t in d & self.terms:
                self.df[t] = self.df.get(t, 0) + 1
        idf = {t: math.log(1 + (self.n + 1) / (self.df.get(t, 0) + 1)) for t in self.terms}
        total = sum(idf.values()) or 1.0
        return [10 * sum(idf[t] for t in d & self.terms) / total for d in docs]


# --- the snowball ----------------------------------------------------------------

ACCEPT = 2   # model relevance needed to join the frontier (see _SCREEN_SYSTEM)
# A candidate that many accepted papers point at is central to the literature
# being mapped; it joins with any non-zero relevance (e.g. a landmark trial
# rationale whose title alone the model under-rates). The bar grows with the
# accepted set, and such "cited core" papers are kept but NOT expanded, so
# widely cited background can't pull the search off-topic.
CORE_SUPPORT = 4


def _core_bar(n_accepted: int) -> float:
    return max(CORE_SUPPORT, 0.05 * n_accepted)


# A relevance-2 paper (same outcome in another model, or a mechanism) is kept,
# but expanded only within this many citation hops of a relevance-3 paper or a
# seed. Without it, chains of "metformin protects X" papers each scored 2 carry
# the search from lifespan into cardiology and oncology a link at a time.
MAX_HOPS = 1


def _hops(paper: dict, accepted: dict) -> int:
    if paper.get("relevance", 3) >= 3 or not paper.get("via"):
        return 0
    return 1 + min((accepted[v["from"]].get("hops", 0) for v in paper["via"]
                    if v["from"] in accepted), default=0)


def snowball(question: str, seed_dois: list = (), max_rounds: int = 8,
             max_papers: int = 200, per_round: int = 150, citing_per_paper: int = 15,
             seeds_per_query: int = 10, screen=None, progress=None,
             semantic: bool = None) -> dict:
    """Run the search. `screen(question, papers)` returns {index: relevance 0-3}
    (a set of indices counts as 3); it defaults to the model — pass a function to
    override (tests, or no model available)."""
    screen = screen or model_screen
    if semantic is None:
        semantic = config.SEMANTIC_STAGE1
    use_semantic = semantic and _semantic.available()
    report = lambda **kw: progress and progress(kw)
    rounds, accepted, seen, edges, screened_log = [], {}, set(), [], []
    # Every accepted paper that has pointed at a candidate so far, across rounds,
    # and the candidates screened out — a paper turned down early is promoted
    # later if enough accepted papers end up citing or being cited by it.
    pointers, rejected, far_out = {}, {}, set()
    lexical = Lexical(question)

    def judge(cands, rnd):
        """Screen candidates; returns (accepted, n screened, n left unscreened)."""
        if not cands:
            return [], 0, 0
        lex = lexical.score(cands)
        for c, s in zip(cands, lex):
            c["lexical"] = round(s, 2)
        # Stage 1 (cheap, no model): the bar rises each round, so the search
        # can't wander far from the question by chaining weakly related papers.
        # A candidate that two or more accepted papers point at is let through
        # regardless — that's citation evidence it belongs in this literature.
        anchors = [p for p in accepted.values() if p.get("relevance", 3) >= 3]
        if use_semantic and anchors:
            # Semantic (LSA): the query is the question plus the papers that
            # answer it directly, so it learns the literature's own vocabulary
            # ("ageing", "all-cause mortality"). The bar is anchored on those
            # papers' own scores in the same fit, so it can't sink round by round.
            text = lambda p: f'{p["title"]} {p.get("abstract") or ""}'
            query = question + " " + " ".join(text(p)[:600] for p in anchors[:40])
            sims = _semantic.semantic_scores(query, [text(c) for c in cands] +
                                             [text(p) for p in anchors])
            for c, s in zip(cands, sims):
                c["stage1"] = c["semantic"] = round(s, 3)
            base = statistics.median(sims[len(cands):]) or 1
            tau = min(0.2 + 0.05 * rnd, 0.4)
        else:
            # Word overlap: weak when papers use other words for the outcome.
            for c in cands:
                c["stage1"] = c["lexical"]
            base = statistics.median([p.get("lexical", 0) for p in accepted.values()]
                                     or [max(lex) or 1]) or 1
            tau = min(0.35 + 0.1 * rnd, 0.8)
        pool = [c for c in cands if not c["retracted"] and c["title"]
                and not _NOTICE_RE.match(c["title"])
                and (c["stage1"] >= tau * base or c.get("support", 0) >= 2)]
        pool.sort(key=lambda c: -(c["stage1"] / base + c.get("support", 0)))
        # Same title as a paper already accepted, or as a better-ranked
        # candidate: another record of the same paper.
        titles = {_title_key(p["title"]) for p in accepted.values()}
        unique = set()
        for c in sorted(pool, key=lambda c: -c.get("cited_by", 0)):
            k = _title_key(c["title"])
            if k not in titles:
                titles.add(k)
                unique.add(c["id"])
        pool = [c for c in pool if c["id"] in unique]
        left = max(0, len(pool) - per_round)
        pool = pool[:per_round]
        # Stage 2 (the model): graded relevance; only 2-3 joins the frontier.
        keep = []
        for i in range(0, len(pool), 15):
            batch = pool[i:i + 15]
            try:
                scores = screen(question, batch)
                if isinstance(scores, set):
                    scores = {j: 3 for j in scores}
            except Exception:
                scores = {j: (2 if c["stage1"] >= 0.6 * base else 0) for j, c in enumerate(batch)}
            for j, c in enumerate(batch):
                c["relevance"] = scores.get(j, 0)
                screened_log.append({"id": c["id"], "doi": c["doi"], "title": c["title"],
                                     "round": rnd, "lexical": c["lexical"],
                                     "semantic": c.get("semantic"),
                                     "support": c.get("support", 0), "relevance": c["relevance"]})
                if c["relevance"] >= ACCEPT:
                    keep.append(c)
                elif c["relevance"] >= 1 and c.get("support", 0) >= _core_bar(len(accepted)):
                    c["cited_core"] = True
                    keep.append(c)
                else:
                    rejected[c["id"]] = c
        return keep, len(pool), left

    _errors.clear()
    stopped, retry_after, frontier = "exhausted", None, []
    try:
        # Round 0: seeds.
        report(stage="seeds", round=0, accepted=0)
        seeds = [w for w in seed_works(question, seed_dois, seeds_per_query) if w["id"]]
        if not seeds:
            # Discovery swallows provider errors, so an empty seed set may mean
            # the providers were unreachable rather than that nothing exists.
            _errors.append("no seed papers came back from the search providers")
        seen.update(w["id"] for w in seeds)
        given = {_discover.clean_doi(d) for d in seed_dois or []}
        for w in [w for w in seeds if w["doi"] in given]:              # given seeds always in
            w["lexical"] = round(lexical.score([w])[0], 2)
            accepted[w["id"]] = {**w, "round": 0, "via": []}
        keep_seeds, screened, left = judge([w for w in seeds if w["doi"] not in given], 0)
        for w in keep_seeds:
            accepted[w["id"]] = {**w, "round": 0, "via": []}
        rounds.append({"round": 0, "candidates": len(seeds), "screened": screened,
                       "unscreened": left, "accepted": len(accepted)})
        frontier = list(accepted)

        for rnd in range(1, max_rounds + 1):
            if not frontier:
                break
            if len(accepted) >= max_papers:
                stopped = "max_papers"
                break
            report(stage="expanding", round=rnd, accepted=len(accepted), frontier=len(frontier))
            # Backward: what the frontier references. Forward: what cites it.
            via, ref_ids = {}, []
            for pid in frontier:
                for r in accepted[pid]["refs"]:
                    via.setdefault(r, []).append({"from": pid, "rel": "referenced by"})
                    ref_ids.append(r)
            cands = {w["id"]: w for w in fetch_works(ids=[r for r in dict.fromkeys(ref_ids) if r not in seen])}
            in_frontier = set(frontier)
            for w in fetch_citing(frontier, citing_per_paper):
                for pid in in_frontier.intersection(w["refs"]):
                    via.setdefault(w["id"], []).append({"from": pid, "rel": "cites"})
                if w["id"] not in seen:
                    cands[w["id"]] = w
            for cid, links in via.items():
                pointers.setdefault(cid, set()).update(v["from"] for v in links)
            for cid, w in cands.items():
                w["support"] = len(pointers.get(cid, ()))
            seen.update(cands)
            report(stage="screening", round=rnd, accepted=len(accepted), candidates=len(cands))
            keep, screened, left = judge(list(cands.values()), rnd)
            for cid in [c for c in rejected if c in via]:
                w = rejected[cid]
                w["support"] = len(pointers[cid])
                if w["relevance"] >= 1 and w["support"] >= _core_bar(len(accepted)):
                    w["cited_core"] = True
                    keep.append(rejected.pop(cid))
            # Beyond MAX_HOPS a single weak link isn't enough: a relevance-2 paper
            # that far out joins only if two accepted papers point at it. (Live
            # metformin run: ~60 of 75 such papers were off-topic, e.g. insulin
            # receptor biochemistry.) It stays eligible for promotion later.
            for w in list(keep):
                w["hops"] = _hops({**w, "via": via.get(w["id"], [])}, accepted)
                if (w["hops"] > MAX_HOPS and w["relevance"] < 3 and not w.get("cited_core")
                        and w.get("support", 0) < 2):
                    keep.remove(w)
                    rejected[w["id"]] = w
                    far_out.add(w["id"])
            keep.sort(key=lambda c: -(c["relevance"] * 10 + c.get("support", 0) + c["lexical"]))
            room = max_papers - len(accepted)
            if len(keep) > room:
                keep, stopped = keep[:room], "max_papers"
            for w in keep:
                accepted[w["id"]] = {**w, "round": rnd, "via": via.get(w["id"], [])}
                edges += [{"from": v["from"], "to": w["id"], "rel": v["rel"]} for v in via.get(w["id"], [])]
            rounds.append({"round": rnd, "candidates": len(cands), "screened": screened,
                           "unscreened": left, "accepted": len(keep)})
            frontier = [w["id"] for w in keep if not w.get("cited_core")
                        and accepted[w["id"]]["hops"] <= MAX_HOPS]
            if stopped == "max_papers":
                break
        else:
            if frontier:
                stopped = "max_rounds"

    except _discover.RateLimited as e:
        # Out of OpenAlex requests: keep what we have, and say so plainly —
        # a cut-short search must never be reported as exhausted.
        stopped, retry_after = "rate_limited", e.retry_after
    if stopped == "exhausted" and _errors:
        stopped = "incomplete"

    papers = sorted(accepted.values(), key=lambda p: (-p.get("relevance", 3), p["round"],
                                                      -p.get("support", 0)))
    for p in papers:
        p.pop("refs", None)
    msg = {"exhausted": "Search exhausted: the last round found no new relevant papers.",
           "max_rounds": f"Stopped at the {max_rounds}-round cap; more papers may remain.",
           "max_papers": f"Stopped at the {max_papers}-paper cap; more papers may remain.",
           "rate_limited": "Stopped early: OpenAlex's request limit was reached (try again in "
                           f"about {max(1, round((retry_after or 60) / 60))} min, or set "
                           "WYR_OPENALEX_API_KEY). More papers likely remain.",
           "incomplete": f"Stopped with {len(_errors)} failed request(s) to OpenAlex, so "
                         "results may be incomplete; more papers may remain."}[stopped]
    far = sum(1 for p in papers if p.get("hops", 0) > MAX_HOPS and not p.get("cited_core"))
    dropped = len(far_out - set(accepted))
    if (far or dropped) and stopped == "exhausted":
        msg = msg[:-1] + f" within {MAX_HOPS} citation hop(s) of the core evidence."
        if far:
            msg += f" {far} paper(s) further out were kept but not followed."
        if dropped:
            msg += (f" {dropped} related paper(s) further out, each linked by a single"
                    " citation, were set aside.")
    left = sum(r.get("unscreened", 0) for r in rounds)
    if left:
        msg += (f" {left} weaker candidate(s) were ranked below the per-round screening"
                " budget and not screened.")
    return {"question": question, "papers": papers, "edges": edges, "rounds": rounds,
            "stopped": stopped, "retry_after": retry_after, "errors": _errors[:5],
            "summary": f"{len(papers)} relevant papers over "
            f"{max(0, len(rounds) - 1)} round(s). {msg}", "seen": len(seen),
            "screened": screened_log}
