"""Multi-provider, iterative source discovery — the Python port of the site's
discovery, extended with citation-trail following.

Round 1 fans out to OpenAlex + Crossref + Europe PMC in parallel (all keyless),
dedupes candidates by DOI, merges their metadata, and ranks by a pure function
of explicit trust signals. Round 2 (optional, default on) follows the citation
trail of the strongest round-1 hits — the papers they reference and OpenAlex's
related-works — and folds those in, rewarding co-citation (a paper pointed to
by several strong seeds). This lifts recall on complex questions the way an
iterative, agent-style search does, while the ranking stays deterministic and
auditable.

Retraction-aware: OpenAlex's `is_retracted` flag is carried through discovery,
and retracted papers are dropped from results by default — a discovery tool
should never recommend a retracted source (verify_claim still does the
authoritative Crossref / Retraction Watch check at verification time).

Every network call is best-effort: a provider that errors or rate-limits
contributes nothing rather than failing the whole search.
"""
import re
from concurrent.futures import ThreadPoolExecutor

import requests

from . import config

OPENALEX = "https://api.openalex.org/works"
CROSSREF = "https://api.crossref.org/works"
EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
S2_SEARCH = "https://api.semanticscholar.org/graph/v1/paper/search"

_REVIEW_RE = re.compile(r"review|meta-analysis|meta analysis", re.I)
_PREPRINT_RE = re.compile(r"preprint|biorxiv|medrxiv|arxiv|posted-content", re.I)
_BANG_RE = re.compile(r"(?:^|\s)(![a-z0-9]+)\b", re.I)

# Fields we pull from OpenAlex works (round 1 and citation-trail fetch alike).
_OA_SELECT = ("id,doi,display_name,title,publication_year,primary_location,"
              "cited_by_count,type,open_access,is_retracted,ids,"
              "referenced_works,related_works")

# DuckDuckGo-style !bang: scope a query to one provider.
BANGS = {
    "!oa": "openalex", "!openalex": "openalex",
    "!cr": "crossref", "!crossref": "crossref",
    "!epmc": "epmc", "!europepmc": "epmc", "!pm": "epmc", "!pubmed": "epmc",
    "!s2": "s2", "!ss": "s2", "!semanticscholar": "s2",
}


def clean_doi(d):
    if not d:
        return None
    d = re.sub(r"^https?://(dx\.)?doi\.org/", "", d, flags=re.I)
    return re.sub(r'[).,;>"\']+$', "", d).lower()


def _headers():
    return {"User-Agent": config.USER_AGENT, "Accept": "application/json"}


def _get_json(url, params):
    # Polite-pool contact only for OpenAlex/Crossref, and only when configured.
    if config.CONTACT_EMAIL and "mailto" not in params and url in (OPENALEX, CROSSREF):
        params = {**params, "mailto": config.CONTACT_EMAIL}
    r = requests.get(url, params=params, headers=_headers(),
                     timeout=config.HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()


def _oa_candidate(w, provider, rank):
    ids = w.get("ids") or {}
    pmid = ids.get("pmid").split("/")[-1] if ids.get("pmid") else None
    loc = (w.get("primary_location") or {}).get("source") or {}
    oa = w.get("open_access") or {}
    return {
        "doi": clean_doi(w.get("doi")), "pmcid": None, "pmid": pmid,
        "title": w.get("display_name") or w.get("title"),
        "year": w.get("publication_year"),
        "venue": loc.get("display_name"),
        "citations": w.get("cited_by_count") or 0,
        "type": w.get("type") or "", "is_oa": bool(oa.get("is_oa")),
        "retracted": bool(w.get("is_retracted")),
        "provider": provider, "rank": rank, "cocite": 0,
        "_oaid": (w.get("id") or "").split("/")[-1] or None,
        "_refs": w.get("referenced_works") or [],
        "_related": w.get("related_works") or [],
    }


def _openalex_search(query, n):
    j = _get_json(OPENALEX, {"search": query, "per_page": n,
                             "select": _OA_SELECT})
    return [_oa_candidate(w, "openalex", i)
            for i, w in enumerate(j.get("results") or [])]


def _crossref_search(query, n):
    j = _get_json(CROSSREF, {
        "query": query, "rows": n,
        "select": "DOI,title,container-title,issued,is-referenced-by-count,type",
    })
    out = []
    for i, m in enumerate((j.get("message") or {}).get("items") or []):
        parts = (m.get("issued") or {}).get("date-parts") or [[]]
        year = parts[0][0] if parts and parts[0] else None
        out.append({
            "doi": clean_doi(m.get("DOI")), "pmcid": None, "pmid": None,
            "title": (m.get("title") or [None])[0], "year": year,
            "venue": (m.get("container-title") or [None])[0],
            "citations": m.get("is-referenced-by-count") or 0,
            "type": m.get("type") or "", "is_oa": False, "retracted": False,
            "provider": "crossref", "rank": i, "cocite": 0,
        })
    return out


def _epmc_search(query, n):
    j = _get_json(EPMC, {"query": query, "format": "json",
                         "resultType": "lite", "pageSize": n})
    out = []
    for i, r in enumerate((j.get("resultList") or {}).get("result") or []):
        out.append({
            "doi": clean_doi(r.get("doi")), "pmcid": r.get("pmcid"),
            "pmid": r.get("pmid"), "title": r.get("title"),
            "year": int(r["pubYear"]) if r.get("pubYear") else None,
            "venue": r.get("journalTitle"),
            "citations": r.get("citedByCount") or 0,
            "type": r.get("pubType") or "",
            "is_oa": r.get("isOpenAccess") == "Y", "retracted": False,
            "provider": "epmc", "rank": i, "cocite": 0,
        })
    return out


def _s2_search(query, n):
    h = _headers()
    if config.S2_API_KEY:
        h["x-api-key"] = config.S2_API_KEY
    r = requests.get(S2_SEARCH, params={
        "query": query, "limit": n,
        "fields": "title,year,venue,citationCount,externalIds,isOpenAccess,publicationTypes",
    }, headers=h, timeout=config.HTTP_TIMEOUT)
    r.raise_for_status()
    j = r.json()
    out = []
    for i, p in enumerate(j.get("data") or []):
        ext = p.get("externalIds") or {}
        out.append({
            "doi": clean_doi(ext.get("DOI")),
            "pmcid": ("PMC" + str(ext["PubMedCentral"])) if ext.get("PubMedCentral") else None,
            "pmid": str(ext["PubMed"]) if ext.get("PubMed") else None,
            "title": p.get("title"), "year": p.get("year"), "venue": p.get("venue"),
            "citations": p.get("citationCount") or 0,
            "type": " ".join(p.get("publicationTypes") or []),
            "is_oa": bool(p.get("isOpenAccess")), "retracted": False,
            "provider": "s2", "rank": i, "cocite": 0,
        })
    return out


PROVIDERS = {"openalex": _openalex_search, "crossref": _crossref_search,
             "epmc": _epmc_search, "s2": _s2_search}


def _openalex_fetch_ids(short_ids, cocite):
    """Batch-fetch OpenAlex works by id (the citation-trail candidates).
    `cocite` maps a short id -> how many seeds pointed at it."""
    out = []
    for i in range(0, len(short_ids), 50):      # OR filter takes up to 50 ids
        chunk = short_ids[i:i + 50]
        try:
            j = _get_json(OPENALEX, {"filter": "openalex_id:" + "|".join(chunk),
                                     "per_page": 50, "select": _OA_SELECT})
        except Exception:
            continue
        for w in j.get("results") or []:
            c = _oa_candidate(w, "openalex-cite", 99)
            c["cocite"] = cocite.get((w.get("id") or "").split("/")[-1], 1)
            out.append(c)
    return out


def _merge_candidates(lists):
    by_key = {}
    for lst in lists:
        for c in lst:
            key = c["doi"] or (c["pmcid"].lower() if c.get("pmcid") else None)
            if not key:
                continue
            m = by_key.get(key)
            if m is None:
                by_key[key] = {**c, "providers": [c["provider"]],
                               "bestRank": c["rank"]}
                continue
            if c["provider"] not in m["providers"]:
                m["providers"].append(c["provider"])
            m["bestRank"] = min(m["bestRank"], c["rank"])
            m["title"] = m["title"] or c["title"]
            m["year"] = m["year"] or c["year"]
            m["venue"] = m["venue"] or c["venue"]
            m["pmcid"] = m["pmcid"] or c["pmcid"]
            m["pmid"] = m["pmid"] or c["pmid"]
            m["citations"] = max(m["citations"] or 0, c["citations"] or 0)
            m["type"] = m["type"] or c["type"]
            m["is_oa"] = m["is_oa"] or c["is_oa"]
            m["retracted"] = m.get("retracted") or c.get("retracted")
            m["cocite"] = max(m.get("cocite") or 0, c.get("cocite") or 0)
            for k in ("_oaid", "_refs", "_related"):
                if c.get(k) and not m.get(k):
                    m[k] = c[k]
    return list(by_key.values())


def _trust_score(c):
    import math
    s = 0.0
    kw_providers = [p for p in (c.get("providers") or []) if p != "openalex-cite"]
    s += len(kw_providers) * 3                        # keyword agreement across providers
    rank = c["bestRank"] if c.get("bestRank") is not None else 9
    s += max(0, 6 - rank)                             # query-match (top of a list)
    s += min(c.get("cocite") or 0, 4) * 2.5           # co-cited by strong seeds
    s += math.log10((c.get("citations") or 0) + 1) * 2   # impact
    if c.get("year"):
        s += max(0, c["year"] - 2000) * 0.05          # mild recency tilt
    if c.get("is_oa"):
        s += 2                                        # full text is verifiable
    if _REVIEW_RE.search(c.get("type") or "") or _REVIEW_RE.search(c.get("title") or ""):
        s += 3                                        # evidence tier
    if _PREPRINT_RE.search(c.get("type") or ""):
        s -= 2                                        # not yet peer-reviewed
    return s


def _sort_key(c):
    # Retracted papers always sort last (defense-in-depth even when a caller
    # asks to include them), then by trust score, then a stable id tiebreak.
    return (bool(c.get("retracted")), -c["_score"],
            str(c.get("doi") or c.get("pmcid")))


def _citation_trail(ranked, seed_k):
    """Collect citation-trail ids (referenced + related works) from the top
    `seed_k` OpenAlex-backed hits, counting how many seeds point at each."""
    cocite = {}
    for c in ranked[:seed_k]:
        if not c.get("_oaid"):
            continue
        seen = set()
        for oid in (c.get("_refs") or []) + (c.get("_related") or []):
            sid = (oid or "").split("/")[-1]
            if sid and sid not in seen:
                seen.add(sid)
                cocite[sid] = cocite.get(sid, 0) + 1
    return cocite


def discover_sources(raw_query, n=3, scope=None, peer_reviewed_only=False,
                     rounds=2, include_retracted=False):
    """Discover up to `n` candidate sources for a query, trust-ranked.

    rounds=1 is the original one-shot keyword search. rounds>=2 (default) also
    follows the citation trail of the top keyword hits to lift recall. A leading
    !bang (!oa / !crossref / !epmc / !pubmed) scopes round 1 to one provider and
    skips the trail. Retracted papers are dropped unless include_retracted=True.
    Returns candidate dicts with doi/pmcid/pmid/title/year/venue/citations/
    providers/retracted plus a `_score`.
    """
    query = (raw_query or "").strip()
    m = _BANG_RE.search(query)
    if m and BANGS.get(m.group(1).lower()):
        scope = BANGS[m.group(1).lower()]
        query = re.sub(r"\s+", " ", query.replace(m.group(1), "")).strip()
    if not query:
        return []

    names = [scope] if scope else ["openalex", "crossref", "epmc", "s2"]
    per = max(n + 3, 8)

    def _safe(name):
        try:
            return PROVIDERS[name](query, per)
        except Exception:
            return []

    with ThreadPoolExecutor(max_workers=len(names)) as ex:
        lists = list(ex.map(_safe, names))

    cands = _merge_candidates(lists)
    for c in cands:
        c["_score"] = _trust_score(c)
    cands.sort(key=_sort_key)

    # Round 2: follow the citation trail of the strongest OpenAlex hits.
    if rounds >= 2 and not scope and "openalex" in names:
        cocite = _citation_trail(cands, seed_k=min(4, len(cands)))
        # Require co-citation by >=2 seeds: a paper only one seed points at is
        # weak signal and the main source of topic drift (the citation analog
        # of provider agreement). Strongest signal first, capped.
        ids = sorted((k for k, v in cocite.items() if v >= 2),
                     key=lambda k: -cocite[k])[:50]
        if ids:
            trail = _openalex_fetch_ids(ids, cocite)
            cands = _merge_candidates(lists + [trail])
            for c in cands:
                c["_score"] = _trust_score(c)
            cands.sort(key=_sort_key)

    cands = [c for c in cands if c.get("doi") or c.get("pmcid")]
    if not include_retracted:
        cands = [c for c in cands if not c.get("retracted")]
    if peer_reviewed_only:
        cands = [c for c in cands if not _PREPRINT_RE.search(c.get("type") or "")]
    return cands[:n]
