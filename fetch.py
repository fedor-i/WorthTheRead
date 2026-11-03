"""Fetch the publication's actual text, section by section.

Primary source: PMC BioC JSON — passages pre-tagged by section_type, no key,
no XML parsing. Fallbacks: Europe PMC abstract, then the Crossref abstract.
We always report which sections we actually obtained so the judge (and the
reader) never mistake an abstract-only check for a results-level one.
"""
import re
import time
import xml.etree.ElementTree as ET
from html import unescape as _unescape

import requests

from . import config
from . import store as _store

BIOC = ("https://www.ncbi.nlm.nih.gov/research/bionlp/RESTful/"
        "pmcoa.cgi/BioC_json/{pmcid}/unicode")
EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
EPMC_FULLTEXT = ("https://www.ebi.ac.uk/europepmc/webservices/rest/"
                 "PMC/{pmcid}/fullTextXML")
OPENALEX_WORK = "https://api.openalex.org/works/doi:{doi}"
CROSSREF_WORK = "https://api.crossref.org/works/{doi}"
UNPAYWALL = "https://api.unpaywall.org/v2/{doi}"
S2_PAPER = "https://api.semanticscholar.org/graph/v1/paper/{id}"
FIGSHARE_API = "https://api.figshare.com/v2/articles/{id}"
ZENODO_API = "https://zenodo.org/api/records/{id}"
CORE_SEARCH = "https://api.core.ac.uk/v3/search/works"
SPRINGER_OA = "https://api.springernature.com/openaccess/jats"
ELSEVIER_ARTICLE = "https://api.elsevier.com/content/article/doi/{doi}"
DOAJ_SEARCH = "https://doaj.org/api/search/articles/{query}"

_RETRIES = 3

# OA-location "pdf_url"s sometimes point at a figure or graphical abstract, not
# the article (e.g. OpenAlex handed us a '...-ga1_lrg.jpg' for an OA paper). Skip
# anything that is plainly an image/asset so a bad link can't preempt a real PDF.
_ASSET_URL = re.compile(r"\.(?:jpe?g|png|gif|svg|webp|tiff?|bmp)(?:\?|$)"
                        r"|/content/image/|[-_]ga\d", re.I)

# BioC section_type -> our normalized bucket.
_SECTION_MAP = {
    "TITLE": "title", "ABSTRACT": "abstract", "INTRO": "introduction",
    "METHODS": "methods", "RESULTS": "results", "DISCUSS": "discussion",
    "CONCL": "conclusion", "CASE": "results", "SUPPL": "supplement",
}
# Order we feed sections to the judge: evidence-bearing first.
_PRIORITY = ["results", "conclusion", "abstract", "methods", "discussion",
             "introduction", "title", "supplement", "other"]


def _headers():
    return {"User-Agent": config.USER_AGENT}


def _s2_headers():
    h = {"User-Agent": config.USER_AGENT}
    if config.S2_API_KEY:
        h["x-api-key"] = config.S2_API_KEY
    return h


def _strip(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _get(url: str, **kw):
    """GET with a few retries + linear backoff (these public endpoints are
    occasionally flaky / rate-limited)."""
    last = None
    for attempt in range(_RETRIES):
        try:
            r = requests.get(url, headers=_headers(),
                             timeout=config.HTTP_TIMEOUT, **kw)
            r.raise_for_status()
            return r
        except Exception as e:
            last = e
            time.sleep(0.6 * (attempt + 1))
    raise last


def _bucket_from_title(title: str) -> str:
    t = (title or "").lower()
    if "result" in t:
        return "results"
    if "method" in t or "material" in t:
        return "methods"
    if "discuss" in t:
        return "discussion"
    if "conclu" in t:
        return "conclusion"
    if "introduc" in t or "background" in t:
        return "introduction"
    return "other"


def _from_bioc(pmcid: str) -> dict:
    """Return {sectionbucket: text} from PMC BioC, or {} if unavailable."""
    pmcid = pmcid if pmcid.upper().startswith("PMC") else "PMC" + pmcid
    r = _get(BIOC.format(pmcid=pmcid))
    data = r.json()
    # BioC comes back either as a list or as {documents: [...]}.
    docs = data if isinstance(data, list) else data.get("documents", [])
    sections = {}
    for doc in docs:
        for d in (doc.get("documents", [doc]) if "documents" in doc else [doc]):
            for p in d.get("passages", []):
                infons = p.get("infons", {})
                stype = (infons.get("section_type") or infons.get("type")
                         or "OTHER").upper()
                bucket = _SECTION_MAP.get(stype, "other")
                txt = _strip(p.get("text", ""))
                if txt:
                    sections.setdefault(bucket, []).append(txt)
    return {k: "\n".join(v) for k, v in sections.items()}


def _jats_sections(content: bytes) -> dict:
    """Parse JATS XML (Europe PMC full-text, Springer OA) into section buckets.
    "{*}" matches tags in any namespace. Returns {} if unparseable."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return {}
    sections = {}
    for ab in root.findall(".//{*}abstract"):
        txt = _strip("".join(ab.itertext()))
        if txt:
            sections.setdefault("abstract", []).append(txt)
        break
    # findall (not find) so a multi-record response — Springer can return
    # several <article> elements — contributes every body, not just the first.
    for body in root.findall(".//{*}body"):
        for sec in body.findall(".//{*}sec"):
            title_el = sec.find("{*}title")
            title = (title_el.text if title_el is not None else "") or ""
            bucket = _bucket_from_title(title)
            # Only this sec's direct paragraphs — nested <sec> handled on their
            # own iteration, so text isn't duplicated.
            txt = _strip(" ".join("".join(p.itertext())
                                  for p in sec.findall("{*}p")))
            if txt:
                sections.setdefault(bucket, []).append(txt)
    return {k: "\n".join(v) for k, v in sections.items()}


def _from_epmc_fulltext(pmcid: str) -> dict:
    """Alternate full-text source: Europe PMC JATS XML. Parsed into the same
    section buckets. Returns {} if unavailable or unparseable."""
    pmcid = pmcid if pmcid.upper().startswith("PMC") else "PMC" + pmcid
    r = _get(EPMC_FULLTEXT.format(pmcid=pmcid))
    return _jats_sections(r.content)


BIORXIV = "https://www.{server}.org/content/{doi}v{ver}.full"
ARXIV_HTML = "https://arxiv.org/html/{aid}"
AR5IV = "https://ar5iv.labs.arxiv.org/html/{aid}"
_ARXIV_ID_RE = r"([a-z\-]+(?:\.[A-Za-z]{2})?/\d{7}|\d{4}\.\d{4,5})(v\d+)?"


def _html_to_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|noscript|head)[^>]*>.*?</\1>", " ", html)
    return _strip(re.sub(r"(?s)<[^>]+>", " ", _unescape(html)))


def _from_html_sections(html: str) -> dict:
    """Split server-rendered article HTML into section buckets by its headings.
    Used for preprint full text (bioRxiv/medRxiv), which PMC rarely carries.

    A heading that names a known section (Results, Methods, ...) sets the
    current bucket; descriptive sub-headings (e.g. "Sequence changes in X") map
    to "other" and are left to inherit the enclosing section, so results text
    under a named subsection still counts as results."""
    html = re.sub(r"(?is)<(script|style|noscript|head)[^>]*>.*?</\1>", " ", html)
    parts = re.split(r"(?is)(<h[1-4][^>]*>.*?</h[1-4]>)", html)
    sections, current = {}, "other"
    for tok in parts:
        mh = re.match(r"(?is)^\s*<h[1-4][^>]*>(.*?)</h[1-4]>\s*$", tok)
        if mh:
            head = _strip(re.sub(r"(?s)<[^>]+>", " ", _unescape(mh.group(1))))
            bucket = _bucket_from_title(head)
            if bucket != "other":
                current = bucket
            continue
        txt = _html_to_text(tok)
        if txt and len(txt) > 40:
            sections.setdefault(current, []).append(txt)
    return {k: "\n".join(v) for k, v in sections.items()}


def _usable_fulltext(secs: dict) -> bool:
    """Accept parsed HTML as real full text: an evidence-bearing section, or
    enough body prose (arXiv CS/physics papers often label it Evaluation /
    Experiments rather than Results).

    When NOTHING sectioned into an evidence-bearing bucket, the content is one
    undifferentiated 'other' blob — real only if it is substantial. A landing /
    abstract page is a few thousand characters and must NOT qualify, or it
    short-circuits the PDF tier that holds the actual article (NEJM/Lancet off
    PMC): require a real body's worth of prose before trusting an unsectioned
    blob."""
    if not secs:
        return False
    if any(secs.get(b) for b in ("results", "discussion", "conclusion", "methods")):
        return True
    return sum(len(v) for k, v in secs.items() if k != "title") >= 8000


def _biorxiv_fulltext(resolved: dict) -> dict:
    """Full text for a bioRxiv / medRxiv preprint (DOI prefix 10.1101), served
    as server-rendered HTML — no PMC version needed. Single-shot requests (these
    servers rate-limit aggressively, so no retry loop and v1 first)."""
    doi = (resolved.get("doi") or "")
    if not doi.startswith("10.1101/"):
        return {}
    for server in ("biorxiv", "medrxiv"):
        for ver in (1, 2):                             # v1 always exists; try a revision next
            try:
                r = requests.get(BIORXIV.format(server=server, doi=doi, ver=ver),
                                 headers=_headers(), timeout=config.HTTP_TIMEOUT)
            except Exception:
                continue
            if r.status_code != 200 or len(r.text) < 2000:
                continue
            secs = _from_html_sections(r.text)
            if _usable_fulltext(secs):
                return secs
    return {}


def _arxiv_id(resolved: dict) -> str:
    """Pull an arXiv id from a DataCite DOI (10.48550/arXiv.*), an arxiv.org
    URL, or a raw 'arXiv:...' reference."""
    m = re.search(r"10\.48550/arxiv\.(.+)$", resolved.get("doi") or "", re.I)
    if m:
        return m.group(1)
    ids = resolved.get("ids") or {}
    blob = " ".join(str(ids.get(k) or "") for k in ("url", "raw"))
    m = re.search(r"arxiv\.org/(?:abs|pdf|html)/" + _ARXIV_ID_RE, blob, re.I) \
        or re.search(r"arxiv[:\s]*" + _ARXIV_ID_RE, blob, re.I)
    return (m.group(1) + (m.group(2) or "")) if m else ""


def _arxiv_fulltext(resolved: dict) -> dict:
    """Full text for an arXiv paper, from arXiv's own HTML (newer papers) or
    ar5iv (older ones) — both server-rendered LaTeX->HTML with section headings.
    Covers the physics / CS / math / quant-bio literature off PMC."""
    aid = _arxiv_id(resolved)
    if not aid:
        return {}
    for url in (ARXIV_HTML.format(aid=aid), AR5IV.format(aid=aid)):
        try:
            r = requests.get(url, headers=_headers(),
                             timeout=config.HTTP_TIMEOUT, allow_redirects=True)
        except Exception:
            continue
        if r.status_code != 200 or len(r.text) < 2000:
            continue
        secs = _from_html_sections(r.text)
        if _usable_fulltext(secs):
            return secs
    return {}


def _oa_html_urls(doi: str) -> list:
    """Candidate open-access full-text HTML URLs for a DOI, off PMC — from
    OpenAlex (keyless), Crossref full-text links, and Unpaywall (only when a
    contact email is configured; never a personal address). PDFs are skipped
    (no PDF parser)."""
    urls = []
    try:
        j = requests.get(OPENALEX_WORK.format(doi=doi),
                         params={"select": "open_access,best_oa_location,"
                                 "primary_location,locations"},
                         headers=_headers(), timeout=config.HTTP_TIMEOUT).json()
        locs = [j.get("best_oa_location"), j.get("primary_location")] + \
            (j.get("locations") or [])
        for loc in locs:
            if loc and loc.get("is_oa") and loc.get("landing_page_url"):
                urls.append(loc["landing_page_url"])
        if (j.get("open_access") or {}).get("oa_url"):
            urls.append(j["open_access"]["oa_url"])
    except Exception:
        pass
    try:
        m = requests.get(CROSSREF_WORK.format(doi=doi), headers=_headers(),
                         timeout=config.HTTP_TIMEOUT).json().get("message", {})
        for lk in (m.get("link") or []):
            if "pdf" not in (lk.get("content-type") or "").lower() and lk.get("URL"):
                urls.append(lk["URL"])
    except Exception:
        pass
    if config.CONTACT_EMAIL:  # Unpaywall requires an email; opt-in only
        try:
            j = requests.get(UNPAYWALL.format(doi=doi),
                             params={"email": config.CONTACT_EMAIL},
                             headers=_headers(), timeout=config.HTTP_TIMEOUT).json()
            loc = j.get("best_oa_location") or {}
            for k in ("url_for_landing_page", "url"):
                if loc.get(k):
                    urls.append(loc[k])
        except Exception:
            pass
    seen = []
    for u in urls:
        if u and u not in seen and not u.lower().endswith(".pdf"):
            seen.append(u)
    return seen[:5]


def _oa_fulltext(resolved: dict) -> dict:
    """Full text from an open-access publisher/repository HTML page (Copernicus,
    MDPI, Frontiers, PLOS off-PMC, etc.), section-split by headings. Single-shot
    best-effort; a paywall/landing stub yields too little and is rejected."""
    doi = resolved.get("doi")
    if not doi:
        return {}
    for url in _oa_html_urls(doi):
        try:
            r = requests.get(url, headers={"User-Agent": config.USER_AGENT,
                                           "Accept": "text/html"},
                             timeout=config.HTTP_TIMEOUT, allow_redirects=True)
        except Exception:
            continue
        ct = (r.headers.get("content-type") or "").lower()
        if r.status_code != 200 or "html" not in ct or len(r.text) < 3000:
            continue
        secs = _from_html_sections(r.text)
        if _usable_fulltext(secs):
            return secs
    return {}


# Standalone section headings as they survive PDF text extraction — a short
# line that is exactly a canonical section name (structured-abstract headings
# like "RESULTS" and body headings like "Results" both match).
_TEXT_HEAD = re.compile(
    r"^\s*(?:\d+\.?\s*)?(abstract|introduction|background|"
    r"materials?\s+and\s+methods|methods?|results?(?:\s+and\s+discussion)?|"
    r"discussion|conclusions?|references|acknowledg(?:e)?ments?|"
    r"supplementary(?:\s+\w+)*)\s*:?\s*$", re.I)


def _bucket_from_heading(h: str):
    """Map a matched heading to a section bucket, or None if unrecognized."""
    h = h.lower()
    if "abstract" in h:
        return "abstract"
    if "result" in h:
        return "results"
    if "method" in h or "material" in h:
        return "methods"
    if "discuss" in h:
        return "discussion"
    if "conclu" in h:
        return "conclusion"
    if "introduc" in h or "background" in h:
        return "introduction"
    if "reference" in h or "acknowledg" in h or "supplement" in h:
        return "supplement"  # captured but deprioritized (not evidence-bearing)
    return None


def _text_sections(text: str) -> dict:
    """Split plain extracted text (e.g. from a PDF) into section buckets by its
    standalone heading lines, so a PDF-sourced paper gets a real RESULTS bucket
    (and thus has_results) instead of one undifferentiated blob. Returns {} when
    no canonical heading is found, so the caller can fall back to one 'other'
    blob — no regression for headingless extractions."""
    sections, current, buf = {}, "other", []

    def flush():
        if buf:
            t = _strip(" ".join(buf))
            if t:
                sections.setdefault(current, []).append(t)

    for line in text.splitlines():
        s = line.strip()
        m = _TEXT_HEAD.match(s) if s and len(s) <= 40 else None
        bucket = _bucket_from_heading(m.group(1)) if m else None
        if bucket:
            flush()
            buf, current = [], bucket
            continue
        buf.append(line)
    flush()
    merged = {k: "\n".join(v) for k, v in sections.items()}
    # Only claim success if we actually carved out named sections (more than the
    # leading 'other' preamble); otherwise let the caller keep the raw blob.
    return merged if any(k != "other" for k in merged) else {}


def _pdf_to_text(content: bytes) -> str:
    """Extract text from a PDF (pypdf). Returns '' if unavailable/unparseable."""
    try:
        import io
        import pypdf
        reader = pypdf.PdfReader(io.BytesIO(content))
        parts = [(pg.extract_text() or "") for pg in reader.pages[:40]]
        return "\n".join(p for p in parts if p)
    except Exception:
        return ""


def _oa_pdf_urls(doi: str) -> list:
    """Open-access PDF URLs for a DOI, from OpenAlex, Crossref, Semantic Scholar,
    and Unpaywall (email opt-in)."""
    urls = []
    try:
        j = requests.get(OPENALEX_WORK.format(doi=doi),
                         params={"select": "best_oa_location,primary_location,locations"},
                         headers=_headers(), timeout=config.HTTP_TIMEOUT).json()
        for loc in [j.get("best_oa_location"), j.get("primary_location")] + \
                (j.get("locations") or []):
            if loc and loc.get("pdf_url"):
                urls.append(loc["pdf_url"])
    except Exception:
        pass
    try:
        m = requests.get(CROSSREF_WORK.format(doi=doi), headers=_headers(),
                         timeout=config.HTTP_TIMEOUT).json().get("message", {})
        for lk in (m.get("link") or []):
            if "pdf" in (lk.get("content-type") or "").lower() and lk.get("URL"):
                urls.append(lk["URL"])
    except Exception:
        pass
    try:
        j = requests.get(S2_PAPER.format(id=f"DOI:{doi}"),
                         params={"fields": "openAccessPdf"}, headers=_s2_headers(),
                         timeout=config.HTTP_TIMEOUT).json()
        if (j.get("openAccessPdf") or {}).get("url"):
            urls.append(j["openAccessPdf"]["url"])
    except Exception:
        pass
    if config.CONTACT_EMAIL:
        try:
            j = requests.get(UNPAYWALL.format(doi=doi),
                             params={"email": config.CONTACT_EMAIL},
                             headers=_headers(), timeout=config.HTTP_TIMEOUT).json()
            if (j.get("best_oa_location") or {}).get("url_for_pdf"):
                urls.append(j["best_oa_location"]["url_for_pdf"])
        except Exception:
            pass
    seen = []
    for u in urls:
        if u and u not in seen and not _ASSET_URL.search(u):
            seen.append(u)
    return seen[:5]


def _pdf_fulltext(resolved: dict) -> dict:
    """Full text from an open-access PDF — section-split by heading lines when
    possible (so RESULTS is recognized), else one 'other' blob."""
    doi = resolved.get("doi")
    if not doi:
        return {}
    for url in _oa_pdf_urls(doi):
        r = None
        for attempt in range(_RETRIES):  # OA mirrors are flaky; the PDF tier is
            try:                         # the valuable fallback, so retry it.
                r = requests.get(url, headers={"User-Agent": config.USER_AGENT},
                                 timeout=config.HTTP_TIMEOUT, allow_redirects=True)
                if r.status_code == 200:
                    break
            except Exception:
                r = None
            time.sleep(0.6 * (attempt + 1))
        if r is None or r.status_code != 200:
            continue
        ct = (r.headers.get("content-type") or "").lower()
        if "pdf" not in ct and not r.content[:5] == b"%PDF-":
            continue
        text = _pdf_to_text(r.content)
        if len(text) >= 1500:
            return _text_sections(text) or {"other": _strip(text)}
    return {}


def _deposit_pdf_urls(doi: str) -> list:
    """Direct PDF download URLs from open deposit repositories (figshare, Zenodo)
    that OpenAlex lists as OA locations. These host the author's copy of the
    article and expose the actual PDF through a documented keyless JSON API, so
    they rescue papers whose publisher PDF is bot-blocked but whose deposit is
    openly downloadable (e.g. an Elsevier gold-OA paper mirrored on figshare)."""
    landings = []
    try:
        j = requests.get(OPENALEX_WORK.format(doi=doi),
                         params={"select": "best_oa_location,primary_location,locations"},
                         headers=_headers(), timeout=config.HTTP_TIMEOUT).json()
        for loc in [j.get("best_oa_location"), j.get("primary_location")] + \
                (j.get("locations") or []):
            if loc and loc.get("landing_page_url"):
                landings.append(loc["landing_page_url"])
    except Exception:
        pass
    urls = []
    for u in landings:
        m = re.search(r"figshare\.com/.*?/(\d{6,})(?:/\d+)?/?(?:\?|$)", u, re.I)
        if m:
            try:
                d = requests.get(FIGSHARE_API.format(id=m.group(1)),
                                 headers=_headers(), timeout=config.HTTP_TIMEOUT).json()
                for f in (d.get("files") or []):
                    if (f.get("name") or "").lower().endswith(".pdf") and f.get("download_url"):
                        urls.append(f["download_url"])
            except Exception:
                pass
            continue
        m = re.search(r"zenodo\.org/record[s]?/(\d+)", u, re.I)
        if m:
            try:
                d = requests.get(ZENODO_API.format(id=m.group(1)),
                                 headers=_headers(), timeout=config.HTTP_TIMEOUT).json()
                for f in (d.get("files") or []):
                    link = (f.get("links") or {}).get("self")
                    if (f.get("key") or "").lower().endswith(".pdf") and link:
                        urls.append(link)
            except Exception:
                pass
    seen = []
    for u in urls:
        if u and u not in seen:
            seen.append(u)
    return seen[:5]


def _deposit_fulltext(resolved: dict) -> dict:
    """Full text from an open deposit-repository PDF (figshare/Zenodo), parsed and
    section-split like any other PDF."""
    doi = resolved.get("doi")
    if not doi:
        return {}
    for url in _deposit_pdf_urls(doi):
        try:
            r = requests.get(url, headers=_headers(),
                             timeout=config.HTTP_TIMEOUT, allow_redirects=True)
        except Exception:
            continue
        if r.status_code != 200:
            continue
        if not (r.content[:5] == b"%PDF-"
                or "pdf" in (r.headers.get("content-type") or "").lower()):
            continue
        text = _pdf_to_text(r.content)
        if len(text) >= 1500:
            return _text_sections(text) or {"other": _strip(text)}
    return {}


def _core_fulltext(resolved: dict) -> dict:
    """Green-OA full text from CORE (core.ac.uk), the large aggregator of
    institutional-repository copies. Requires a free API key (config.CORE_API_KEY);
    CORE's anonymous endpoints are bot-blocked, so this tier is inactive without
    one. Returns CORE's own extracted fullText, else a parsed download, else {}."""
    doi = resolved.get("doi")
    if not doi or not config.CORE_API_KEY:
        return {}
    hdr = {"User-Agent": config.USER_AGENT,
           "Authorization": f"Bearer {config.CORE_API_KEY}"}
    try:
        r = requests.get(CORE_SEARCH, params={"q": f'doi:"{doi}"', "limit": 3},
                         headers=hdr, timeout=config.HTTP_TIMEOUT)
        if r.status_code != 200:
            return {}
        results = r.json().get("results") or []
    except Exception:
        return {}
    for work in results:
        # a) CORE's own extracted full text — already plain text, best case.
        ft = _strip(work.get("fullText") or "")
        if len(ft) >= 1500:
            return _text_sections(ft) or {"other": ft}
        # b) the ORIGINAL repository PDFs CORE indexed (sourceFulltextUrls). We
        #    skip CORE's own core.ac.uk download/image endpoints (bot-blocked)
        #    and plain assets, and fetch the repository copy directly.
        for u in (work.get("sourceFulltextUrls") or []):
            if not u or "core.ac.uk" in u or _ASSET_URL.search(u):
                continue
            try:
                d = requests.get(u, headers=_headers(), timeout=config.HTTP_TIMEOUT,
                                 allow_redirects=True)
            except Exception:
                continue
            if d.status_code == 200 and d.content[:5] == b"%PDF-":
                text = _pdf_to_text(d.content)
                if len(text) >= 1500:
                    return _text_sections(text) or {"other": _strip(text)}
    return {}


def _springer_fulltext(resolved: dict) -> dict:
    """Full text from the Springer Nature Open Access API (JATS) — the sanctioned
    machine path to Springer/Nature/BMC open-access articles, not bot-blocked
    like the public pages. Requires a free key (config.SPRINGER_API_KEY)."""
    doi = resolved.get("doi")
    if not doi or not config.SPRINGER_API_KEY:
        return {}
    try:
        r = requests.get(SPRINGER_OA,
                         params={"q": f"doi:{doi}", "api_key": config.SPRINGER_API_KEY},
                         headers=_headers(), timeout=config.HTTP_TIMEOUT)
        if r.status_code != 200:
            return {}
        return _jats_sections(r.content)
    except Exception:
        return {}


def _elsevier_fulltext(resolved: dict) -> dict:
    """Full text from the Elsevier Article Retrieval API. A free key
    (config.ELSEVIER_API_KEY) returns full text for Elsevier OA articles; an
    optional institutional token (config.ELSEVIER_INSTTOKEN) also unlocks
    subscription content the institution is entitled to. Inactive without a key
    (the keyless response carries only metadata, no originalText)."""
    doi = resolved.get("doi")
    if not doi or not config.ELSEVIER_API_KEY:
        return {}
    hdr = {"User-Agent": config.USER_AGENT, "Accept": "application/json",
           "X-ELS-APIKey": config.ELSEVIER_API_KEY}
    if config.ELSEVIER_INSTTOKEN:
        hdr["X-ELS-Insttoken"] = config.ELSEVIER_INSTTOKEN
    try:
        r = requests.get(ELSEVIER_ARTICLE.format(doi=doi), headers=hdr,
                         timeout=config.HTTP_TIMEOUT)
        if r.status_code != 200:
            return {}
        body = (r.json().get("full-text-retrieval-response") or {})
        text = _strip(body.get("originalText") or "")
        if len(text) >= 1500:
            return _text_sections(text) or {"other": text}
    except Exception:
        return {}
    return {}


def _doaj_fulltext(resolved: dict) -> dict:
    """Full text via DOAJ (keyless): DOAJ indexes gold-OA journal articles and
    lists each one's 'fulltext' link (publisher HTML or a repository PDF). We
    fetch that link and parse it — catches gold OA that OpenAlex/Crossref miss."""
    doi = resolved.get("doi")
    if not doi:
        return {}
    from urllib.parse import quote
    try:
        r = requests.get(DOAJ_SEARCH.format(query=quote(f'doi:"{doi}"', safe="")),
                         headers=_headers(), timeout=config.HTTP_TIMEOUT)
        if r.status_code != 200:
            return {}
        results = r.json().get("results") or []
    except Exception:
        return {}
    urls = []
    for art in results:
        for lk in ((art.get("bibjson") or {}).get("link") or []):
            if lk.get("type") == "fulltext" and lk.get("url"):
                urls.append(lk["url"])
    for u in urls[:4]:
        if _ASSET_URL.search(u):
            continue
        try:
            r = requests.get(u, headers={"User-Agent": config.USER_AGENT,
                             "Accept": "text/html,application/pdf,*/*"},
                             timeout=config.HTTP_TIMEOUT, allow_redirects=True)
        except Exception:
            continue
        if r.status_code != 200:
            continue
        ct = (r.headers.get("content-type") or "").lower()
        if r.content[:5] == b"%PDF-" or "pdf" in ct:
            text = _pdf_to_text(r.content)
            if len(text) >= 1500:
                return _text_sections(text) or {"other": _strip(text)}
        elif "html" in ct:
            secs = _from_html_sections(r.text)
            if _usable_fulltext(secs):
                return secs
    return {}


def _s2_abstract(doi: str) -> str:
    try:
        j = requests.get(S2_PAPER.format(id=f"DOI:{doi}"),
                         params={"fields": "abstract"}, headers=_s2_headers(),
                         timeout=config.HTTP_TIMEOUT).json()
        return _strip(j.get("abstract") or "")
    except Exception:
        return ""


def _epmc_core(resolved: dict) -> dict:
    """Europe PMC 'core' record for a DOI/PMID (covers most of PubMed). Carries
    the abstract, the PMCID (even when NCBI's converter didn't link it), and OA
    flags — so one lookup serves both PMCID recovery and the abstract fallback."""
    q = None
    if resolved.get("doi"):
        q = f'DOI:"{resolved["doi"]}"'
    elif resolved.get("pmid"):
        q = f'EXT_ID:{resolved["pmid"]} AND SRC:MED'
    if not q:
        return {}
    try:
        r = _get(EPMC, params={"query": q, "format": "json",
                               "resultType": "core"})
        results = r.json().get("resultList", {}).get("result", [])
        return results[0] if results else {}
    except Exception:
        return {}


def assemble(out: dict) -> dict:
    """Fill the derived fields of a fetched dict from its `sections` and
    `source`: sections_available, has_results, has_fulltext, and the capped
    `full_text` the judge reads (priority order, `## BUCKET` headers). Shared by
    fetch_text and the local-library ingest so both look identical downstream."""
    out["sections_available"] = [s for s in _PRIORITY
                                 if s in out["sections"] and out["sections"][s]]
    out["has_results"] = "results" in out["sections_available"]

    # "Full text (not just an abstract) obtained" — this, not has_results, is
    # what the judge's 0.5 confidence cap should key off. has_results is too
    # literal: physics papers, parsed PDFs, and some journals carry their
    # findings under non-"Results" headings or one unsectioned body, so keying
    # the cap off a literal RESULTS bucket wrongly pins real full-text reads at
    # 0.5. Accept an evidence-bearing section OR a substantial body of prose.
    _body = sum(len(out["sections"].get(b, ""))
                for b in ("results", "discussion", "conclusion", "methods", "other"))
    out["has_fulltext"] = (
        out["source"] not in ("none", "abstract_only", "supplied_text")
        and (any(out["sections"].get(b)
                 for b in ("results", "discussion", "conclusion", "methods"))
             or _body >= 5000))

    # Assemble capped full text in priority order with headers.
    parts, budget = [], config.MAX_TEXT_CHARS
    for bucket in _PRIORITY:
        txt = out["sections"].get(bucket)
        if not txt:
            continue
        header = f"\n## {bucket.upper()}\n"
        chunk = (header + txt)[:budget]
        parts.append(chunk)
        budget -= len(chunk)
        if budget <= 0:
            break
    out["full_text"] = "".join(parts).strip()
    return out


_FETCH_CACHE = {}


def clear_cache():
    _FETCH_CACHE.clear()


def fetch_text(resolved: dict) -> dict:
    """Given a resolved reference, return the paper text we could obtain.

    Returns: {sections: {bucket: text}, sections_available: [..],
              has_results(bool), open_access(bool), source: str,
              full_text: str (capped, priority order)}.

    Cached per process by DOI/PMCID: the same paper always returns the same
    text within a run (deterministic across self-consistency passes and repeated
    citations in an audit, and faster), instead of varying with which flaky OA
    endpoint happened to answer.
    """
    key = (resolved.get("doi") or resolved.get("pmcid") or "").lower()
    if key and key in _FETCH_CACHE:
        return _FETCH_CACHE[key]
    cached = _store.get(key)          # on-disk cache (sidecar only)
    if cached:
        _FETCH_CACHE[key] = cached
        return cached
    out = {"sections": {}, "sections_available": [], "has_results": False,
           "open_access": False, "source": "none", "full_text": ""}

    pmcid = resolved.get("pmcid")
    # Recover a PMCID when resolve() didn't get one: Europe PMC often knows the
    # PMC version of an OA paper whose DOI NCBI's converter didn't link. This
    # one lookup is reused for the abstract fallback below.
    core = {}
    if not pmcid:
        core = _epmc_core(resolved)
        cand = core.get("pmcid")
        if cand and (core.get("inEPMC") == "Y" or core.get("isOpenAccess") == "Y"):
            pmcid = cand

    if pmcid:
        # 1) PMC BioC — section-tagged JSON (retried inside _get).
        try:
            secs = _from_bioc(pmcid)
            if secs:
                out["sections"] = secs
                out["open_access"] = True
                out["source"] = "pmc_bioc"
        except Exception:
            pass
        # 2) Europe PMC full-text XML — alternate full-text source.
        if not out["sections"]:
            try:
                secs = _from_epmc_fulltext(pmcid)
                if secs:
                    out["sections"] = secs
                    out["open_access"] = True
                    out["source"] = "epmc_fulltext"
            except Exception:
                pass
        out["pmcid"] = pmcid

    # 2b) Preprint full text — "all text" lives off PMC: bioRxiv/medRxiv HTML,
    # then arXiv HTML / ar5iv for the physics / CS / math / quant-bio literature.
    if not out["sections"]:
        for fetcher, label in ((_biorxiv_fulltext, "preprint_html"),
                               (_arxiv_fulltext, "arxiv_html")):
            try:
                secs = fetcher(resolved)
            except Exception:
                secs = {}
            if secs:
                out["sections"] = secs
                out["open_access"] = True
                out["source"] = label
                break

    # 2b2) Publisher open-access APIs (key-gated) — the sanctioned machine path
    # to Springer/Nature/BMC and Elsevier OA full text, cleaner than scraping and
    # not bot-blocked. No-ops without their keys, so keyless behavior is unchanged.
    if not out["sections"]:
        for fetcher, label in ((_springer_fulltext, "springer_oa"),
                               (_elsevier_fulltext, "elsevier_oa")):
            try:
                secs = fetcher(resolved)
            except Exception:
                secs = {}
            if secs:
                out["sections"] = secs
                out["open_access"] = True
                out["source"] = label
                break

    # 2c) Open-access full text off PMC: publisher/repository HTML (Copernicus,
    # MDPI, Frontiers, PLOS off-PMC, ...) via OpenAlex/Crossref/Unpaywall links.
    if not out["sections"]:
        try:
            secs = _oa_fulltext(resolved)
            if secs:
                out["sections"] = secs
                out["open_access"] = True
                out["source"] = "oa_html"
        except Exception:
            pass

    # 2c2) DOAJ (keyless) — gold-OA journal full-text links OpenAlex/Crossref miss.
    if not out["sections"]:
        try:
            secs = _doaj_fulltext(resolved)
            if secs:
                out["sections"] = secs
                out["open_access"] = True
                out["source"] = "doaj"
        except Exception:
            pass

    # 2d) Open-access PDF (OpenAlex/Crossref/Semantic Scholar/Unpaywall), parsed.
    if not out["sections"]:
        try:
            secs = _pdf_fulltext(resolved)
            if secs:
                out["sections"] = secs
                out["open_access"] = True
                out["source"] = "oa_pdf"
        except Exception:
            pass

    # 2e) Open deposit repositories (figshare/Zenodo) — the author's openly
    # downloadable PDF when the publisher's own copy is bot-blocked. Keyless.
    if not out["sections"]:
        try:
            secs = _deposit_fulltext(resolved)
            if secs:
                out["sections"] = secs
                out["open_access"] = True
                out["source"] = "deposit_pdf"
        except Exception:
            pass

    # 2f) CORE aggregator (green-OA institutional-repository copies). Requires
    # WYR_CORE_API_KEY; a no-op when unset (CORE blocks anonymous access).
    if not out["sections"]:
        try:
            secs = _core_fulltext(resolved)
            if secs:
                out["sections"] = secs
                out["open_access"] = True
                out["source"] = "core"
        except Exception:
            pass

    # 3) Fallback: abstract only (Europe PMC, then Semantic Scholar).
    if not out["sections"]:
        if not core:
            core = _epmc_core(resolved)
        abstract = _strip(core.get("abstractText", "")) if core else ""
        if not abstract and resolved.get("doi"):
            abstract = _s2_abstract(resolved["doi"])
        if abstract:
            out["sections"] = {"abstract": abstract}
            out["source"] = "abstract_only"

    assemble(out)
    if key:
        _FETCH_CACHE[key] = out
        _store.put(key, out)
    return out
