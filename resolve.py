"""Resolve a reference (DOI / PMID / PMCID / URL) to canonical metadata,
check the link actually resolves, and detect retractions.

All network calls are best-effort: on failure we return what we have with a
clear flag rather than raising, so verify_claim can still produce a verdict.
"""
import re
import requests

from . import config

_DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+", re.I)
_PMID_RE = re.compile(r"(?:pmid[:\s]*)(\d{4,9})", re.I)
_PMCID_RE = re.compile(r"PMC\d{5,9}", re.I)

CROSSREF = "https://api.crossref.org/works/{doi}"
IDCONV = "https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/"
BIORXIV_API = "https://api.biorxiv.org/details/{server}/{doi}"


def _headers():
    return {"User-Agent": config.USER_AGENT, "Accept": "application/json"}


def parse_reference(reference: str) -> dict:
    """Pull whatever identifiers we can out of a free-form reference string."""
    ref = (reference or "").strip()
    ids = {"doi": None, "pmid": None, "pmcid": None, "url": None, "raw": ref}

    if ref.lower().startswith("http"):
        ids["url"] = ref

    m = _DOI_RE.search(ref)
    if m:
        # Trim common trailing junk a DOI regex can over-capture from a URL.
        ids["doi"] = m.group(0).rstrip(").,;>\"']").replace("%2F", "/")

    m = _PMCID_RE.search(ref)
    if m:
        ids["pmcid"] = m.group(0).upper()

    m = _PMID_RE.search(ref)
    if m:
        ids["pmid"] = m.group(1)
    elif ref.isdigit() and 4 <= len(ref) <= 9:
        ids["pmid"] = ref  # a bare number is almost always a PMID

    # pubmed / pmc URLs carry the id in the path
    if ids["url"]:
        mu = re.search(r"pubmed\.ncbi\.nlm\.nih\.gov/(\d{4,9})", ref)
        if mu and not ids["pmid"]:
            ids["pmid"] = mu.group(1)
        mu = re.search(r"PMC(\d{5,9})", ref, re.I)
        if mu and not ids["pmcid"]:
            ids["pmcid"] = "PMC" + mu.group(1)
    return ids


def _idconv(any_id: str) -> dict:
    """Map between DOI / PMID / PMCID via the NCBI ID Converter."""
    params = {"ids": any_id, "format": "json", "tool": "worthyourread"}
    if config.CONTACT_EMAIL:
        params["email"] = config.CONTACT_EMAIL
    try:
        r = requests.get(IDCONV, params=params, headers=_headers(),
                         timeout=config.HTTP_TIMEOUT)
        r.raise_for_status()
        recs = r.json().get("records", [])
        if recs and "status" not in recs[0]:
            return recs[0]  # {doi, pmid, pmcid}
    except Exception:
        pass
    return {}


def _crossref(doi: str) -> dict:
    params = {}
    if config.CONTACT_EMAIL:
        params["mailto"] = config.CONTACT_EMAIL
    r = requests.get(CROSSREF.format(doi=doi), params=params,
                     headers=_headers(), timeout=config.HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json().get("message", {})


def _authors(msg: dict) -> str:
    out = []
    for a in msg.get("author", [])[:8]:
        name = " ".join(x for x in (a.get("given"), a.get("family")) if x)
        if name:
            out.append(name)
    return ", ".join(out)


def _year(msg: dict):
    for key in ("published-print", "published-online", "published", "issued"):
        parts = (msg.get(key) or {}).get("date-parts") or [[]]
        if parts and parts[0]:
            return parts[0][0]
    return None


def _retraction(msg: dict) -> dict:
    """A retracted paper carries an 'updated-by' entry of type 'retraction'
    (Crossref now ingests Retraction Watch). 'update-to' means THIS record is
    itself the retraction notice."""
    flags = {"retracted": False, "concern": False, "notices": []}
    for field in ("updated-by", "update-to"):
        for upd in msg.get(field, []) or []:
            t = (upd.get("type") or "").lower()
            label = upd.get("label") or t
            if "retraction" in t:
                flags["retracted"] = True
                flags["notices"].append(label)
            elif "concern" in t:
                flags["concern"] = True
                flags["notices"].append(label)
            elif "correction" in t or "erratum" in t:
                flags["notices"].append(label)
    return flags


def _biorxiv_published(doi: str) -> str:
    """The published-journal DOI for a bioRxiv/medRxiv preprint, via their API's
    `published` field, or None if it hasn't been published (or isn't linked)."""
    for server in ("biorxiv", "medrxiv"):
        try:
            r = requests.get(BIORXIV_API.format(server=server, doi=doi),
                             headers=_headers(), timeout=config.HTTP_TIMEOUT)
            coll = r.json().get("collection") or []
            pub = (coll[-1].get("published") if coll else None) or ""
            if pub and pub.upper() != "NA":
                return pub
        except Exception:
            pass
    return None


def _journal_meta(doi: str) -> dict:
    """Minimal journal metadata for a (published) DOI."""
    try:
        m = _crossref(doi)
        return {"doi": doi, "journal": (m.get("container-title") or [None])[0],
                "year": _year(m), "type": m.get("type")}
    except Exception:
        return {"doi": doi, "journal": None, "year": None, "type": None}


def _relation_doi(msg: dict, key: str) -> str:
    for rel in (msg.get("relation") or {}).get(key, []) or []:
        if (rel.get("id-type") or "").lower() == "doi" and rel.get("id"):
            return rel["id"]
    return None


def _preprint_links(out: dict, msg: dict) -> None:
    """Fill is_preprint / published_as / has_preprint on `out`.

    A cited preprint that later cleared peer review at a journal is more
    trustworthy; one that never did is a mild caution. The reverse link (a
    journal article's originating preprint) is reported when the data has it."""
    doi = (out.get("doi") or "")
    ctype = (msg.get("type") or "").lower()
    out["is_preprint"] = ("posted-content" in ctype) or doi.startswith("10.1101/")
    if out["is_preprint"]:
        pub_doi = _biorxiv_published(doi) if doi.startswith("10.1101/") else None
        pub_doi = pub_doi or _relation_doi(msg, "is-preprint-of")
        if pub_doi:
            out["published_as"] = _journal_meta(pub_doi)
    else:
        pre_doi = _relation_doi(msg, "has-preprint")
        if pre_doi:
            out["has_preprint"] = {"doi": pre_doi}


def resolve(reference: str, expected_title: str = None) -> dict:
    """Resolve a reference to metadata + retraction status.

    Returns a dict always containing: ids, resolved(bool), citation_ok(bool),
    title/authors/year/journal, pmcid, retracted(bool), concern(bool),
    notices(list), title_match(float|None), error(str|None).
    """
    ids = parse_reference(reference)
    out = {
        "ids": ids, "resolved": False, "citation_ok": False,
        "title": None, "authors": None, "year": None, "journal": None,
        "doi": ids["doi"], "pmcid": ids["pmcid"], "pmid": ids["pmid"],
        "retracted": False, "concern": False, "notices": [],
        "is_preprint": False, "published_as": None, "has_preprint": None,
        "title_match": None, "error": None,
    }

    # Fill in missing ids via NCBI when we have at least one biomedical id.
    seed = ids["doi"] or ids["pmcid"] or ids["pmid"]
    if seed and not (ids["doi"] and ids["pmcid"]):
        rec = _idconv(seed)
        for k in ("doi", "pmid", "pmcid"):
            if rec.get(k) and not out.get(k):
                out[k] = rec[k]
    out["pmcid"] = out["pmcid"] or ids["pmcid"]

    if not out["doi"]:
        out["error"] = ("No DOI could be resolved from the reference; "
                        "citation cannot be verified against Crossref.")
        return out

    try:
        msg = _crossref(out["doi"])
    except Exception as e:  # dead/wrong DOI, network, etc.
        out["error"] = f"DOI did not resolve at Crossref: {e}"
        return out

    out["resolved"] = True
    out["citation_ok"] = True
    out["title"] = (msg.get("title") or [None])[0]
    out["authors"] = _authors(msg)
    out["year"] = _year(msg)
    out["journal"] = (msg.get("container-title") or [None])[0]
    out.update(_retraction(msg))
    _preprint_links(out, msg)

    # Optional sanity check: does the resolved title match what was claimed?
    if expected_title and out["title"]:
        out["title_match"] = _similar(expected_title, out["title"])
        if out["title_match"] < 0.6:
            out["citation_ok"] = False  # right-looking citation, wrong paper
    return out


def _similar(a: str, b: str) -> float:
    """Cheap token-set similarity in [0,1] — no external deps."""
    norm = lambda s: set(re.findall(r"[a-z0-9]+", s.lower()))
    sa, sb = norm(a), norm(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)
