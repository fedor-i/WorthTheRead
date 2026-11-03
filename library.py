"""Read papers the user already has locally — a Zotero attachment or any PDF —
into the same `fetched` shape fetch.fetch_text returns, plus a page map so a
verified quote can point at the page it came from.

Text source, best first:
  1. Zotero's own full-text cache (`storage/<KEY>/.zotero-ft-cache`): already
     extracted by Zotero, with a form feed between pages.
  2. The PDF itself, page by page with pypdf.
Nothing here touches the network: this is what makes papers already in the
library work offline.
"""
import os

from . import fetch as _fetch
from . import judge as _judge

FT_CACHE = ".zotero-ft-cache"


def zotero_data_dir() -> str:
    """Zotero's data directory (default ~/Zotero; override with WYR_ZOTERO_DIR)."""
    return os.path.expanduser(os.environ.get("WYR_ZOTERO_DIR", "~/Zotero"))


def _pdf_pages(path: str) -> list:
    try:
        import pypdf
        reader = pypdf.PdfReader(path)
        return [(pg.extract_text() or "") for pg in reader.pages]
    except Exception:
        return []


def read_pages(pdf_path: str = "", attachment_key: str = "") -> tuple:
    """Return (pages, source) for a Zotero attachment key or a PDF path."""
    folder = ""
    if attachment_key:
        folder = os.path.join(zotero_data_dir(), "storage", attachment_key)
    elif pdf_path:
        folder = os.path.dirname(pdf_path)
    cache = os.path.join(folder, FT_CACHE) if folder else ""
    if cache and os.path.isfile(cache):
        with open(cache, encoding="utf-8", errors="replace") as fh:
            pages = fh.read().split("\f")
        if sum(len(p) for p in pages) > 200:
            return pages, "zotero_fulltext"
    if not pdf_path and folder and os.path.isdir(folder):
        pdfs = sorted(f for f in os.listdir(folder) if f.lower().endswith(".pdf"))
        pdf_path = os.path.join(folder, pdfs[0]) if pdfs else ""
    if pdf_path and os.path.isfile(pdf_path):
        pages = _pdf_pages(pdf_path)
        if sum(len(p) for p in pages) > 200:
            return pages, "local_pdf"
    return [], "none"


def load(pdf_path: str = "", attachment_key: str = "") -> dict:
    """A `fetched` dict for a local paper, plus `pages` (the raw page texts)."""
    pages, source = read_pages(pdf_path, attachment_key)
    out = {"sections": {}, "sections_available": [], "has_results": False,
           "open_access": False, "source": source, "full_text": "",
           "pages": pages}
    if pages:
        text = "\n".join(pages)
        out["sections"] = _fetch._text_sections(text) or {"other": text}
    _fetch.assemble(out)
    # A whole PDF is never "just an abstract", even when its headings don't
    # map to sections (letters, commentaries: the body can land in "supplement").
    if out["source"] != "none" and sum(len(t) for t in out["sections"].values()) >= 5000:
        out["has_fulltext"] = True
    return out


def page_of(quote: str, pages: list):
    """1-based page number holding the quote, or None. Uses the same matching
    as the quote guard, so any quote that verified can be located."""
    if not quote or not pages:
        return None
    for i, p in enumerate(pages):
        if _judge._quote_verified(quote, _judge._norm(p)):
            return i + 1
    # A quote can straddle a page break: try each adjacent pair.
    for i in range(len(pages) - 1):
        if _judge._quote_verified(quote, _judge._norm(pages[i] + " " + pages[i + 1])):
            return i + 1
    return None


def annotate_pages(result: dict, pages: list) -> dict:
    """Add a `page` to every verified quote in a judge/answer result."""
    for a in result.get("aspects", []) or []:
        if a.get("quote"):
            a["page"] = page_of(a["quote"], pages)
    for e in result.get("evidence", []) or []:
        if e.get("quote"):
            e["page"] = page_of(e["quote"], pages)
    for c in result.get("claims", []) or []:
        for q in c.get("quotes", []) or []:
            if q.get("verified"):
                q["page"] = page_of(q["text"], pages)
    return result


# --- where a quote sits on the page (for highlighting) ------------------------
# Zotero highlights are rectangles in PDF user space (origin bottom-left, in
# points), one per line of text. We find the quote's words with pdfplumber,
# matching on the same alphanumeric normalization as the quote guard, and
# return one rectangle per line they occupy.

def pdf_path_for(attachment_key: str = "", pdf_path: str = "") -> str:
    if pdf_path:
        return pdf_path
    folder = os.path.join(zotero_data_dir(), "storage", attachment_key)
    if os.path.isdir(folder):
        pdfs = sorted(f for f in os.listdir(folder) if f.lower().endswith(".pdf"))
        if pdfs:
            return os.path.join(folder, pdfs[0])
    return ""


def _page_words(pdf):
    """[(token, page_index, word)] over the document, joining words split by a
    line-break hyphen ("re-" / "search") into one token like the guard does."""
    out = []
    for pi, page in enumerate(pdf.pages):
        words = page.extract_words(use_text_flow=True, keep_blank_chars=False)
        i = 0
        while i < len(words):
            w = words[i]
            text, parts = w["text"], [w]
            if text.endswith("-") and i + 1 < len(words) and words[i + 1]["top"] > w["bottom"] - 1:
                text = text[:-1] + words[i + 1]["text"]
                parts.append(words[i + 1])
                i += 1
            for tok in _judge._norm_alnum(text).split():
                out.append((tok, pi, parts))
            i += 1
    return out


def _find_span(qtoks, toks):
    """(start, end) token span of the quote in the document, exact first, then
    the best fuzzy window (>= 80% of the quote's words in order)."""
    n = len(qtoks)
    if not n:
        return None
    first = qtoks[0]
    for i, t in enumerate(toks):
        if t == first and toks[i:i + n] == qtoks:
            return i, i + n
    import difflib
    best, best_score = None, 0.0
    anchors = {qtoks[0], qtoks[min(1, n - 1)], qtoks[-1]}
    for i, t in enumerate(toks):
        if t not in anchors:
            continue
        lo, hi = max(0, i - n), min(len(toks), i + 2 * n)
        sm = difflib.SequenceMatcher(None, toks[lo:hi], qtoks, autojunk=False)
        blocks = [b for b in sm.get_matching_blocks() if b.size]
        score = sum(b.size for b in blocks) / n
        if blocks and score > best_score:
            best_score = score
            best = (lo + blocks[0].a, lo + blocks[-1].a + blocks[-1].size)
    return best if best_score >= 0.8 else None


def locate_quotes(quotes: list, attachment_key: str = "", pdf_path: str = "") -> list:
    """For each quote: {text, found, page (1-based), page_label, rects, sort_top}.
    rects are [x1, y1, x2, y2] in PDF points, one per text line."""
    path = pdf_path_for(attachment_key, pdf_path)
    if not path or not os.path.isfile(path):
        return [{"text": q, "found": False} for q in quotes]
    import pdfplumber
    out = []
    with pdfplumber.open(path) as pdf:
        index = _page_words(pdf)
        toks = [t for t, _, _ in index]
        for q in quotes:
            span = _find_span(_judge._norm_alnum(q).split(), toks)
            if not span:
                out.append({"text": q, "found": False})
                continue
            seen, words = set(), []
            for _, pi, parts in index[span[0]:span[1]]:
                for w in parts:
                    if id(w) not in seen:
                        seen.add(id(w))
                        words.append((pi, w))
            pi0 = words[0][0]
            words = [(pi, w) for pi, w in words if pi == pi0]   # first page only
            page = pdf.pages[pi0]
            x0m, y0m, _, y1m = page.mediabox
            lines = {}
            for _, w in words:
                key = round(w["top"] / 3)          # words on one line share a top
                lines.setdefault(key, []).append(w)
            rects = []
            for key in sorted(lines):
                ws = lines[key]
                top, bottom = min(w["top"] for w in ws), max(w["bottom"] for w in ws)
                rects.append([round(x0m + min(w["x0"] for w in ws), 3),
                              round(y1m - bottom, 3),
                              round(x0m + max(w["x1"] for w in ws), 3),
                              round(y1m - top, 3)])
            out.append({"text": q, "found": True, "page": pi0 + 1,
                        "page_label": str(pi0 + 1), "rects": rects,
                        "sort_top": round(min(w["top"] for _, w in words))})
    return out
