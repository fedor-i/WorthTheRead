"""Focus the judge on the passages that matter for a claim.

A local model reading 48k characters is slow (tens of seconds per claim on a
laptop) and loses the thread. Here we split the paper text into short passages,
rank them against the claim with BM25, and hand the judge only the abstract
plus the best passages, in document order, under their section headers.

This only changes what the judge READS. The quote-verification guard still
checks every quote against the paper's full text, so focusing can make the
judge miss evidence (→ INSUFFICIENT / OVERSTATED) but can never let it cite
text that isn't in the paper.
"""
import math
import re
from collections import Counter

_HEAD_RE = re.compile(r"^## ([A-Z_ ]+)\n", re.M)
_WORD_RE = re.compile(r"[a-z0-9]+(?:[-'][a-z0-9]+)*")
_SENT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9(\[])")
_STOP = set("""a an and are as at be been being but by can could did do does for
from had has have how in into is it its may might more most of on or our such
than that the their them then there these they this those to was we were what
when which while who will with within without would""".split())


def tokens(text: str) -> list:
    return [w for w in _WORD_RE.findall((text or "").lower()) if w not in _STOP]


def split_sections(full_text: str) -> list:
    """`## BUCKET` headed text (as fetch.fetch_text builds it) -> [(bucket, body)]."""
    heads = list(_HEAD_RE.finditer(full_text or ""))
    if not heads:
        return [("text", (full_text or "").strip())]
    out = []
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(full_text)
        out.append((m.group(1).strip().lower(), full_text[m.end():end].strip()))
    return out


def passages(body: str, target: int = 700) -> list:
    """Split a section into ~target-char passages on sentence boundaries."""
    sents = [s.strip() for s in _SENT_RE.split(re.sub(r"\s+", " ", body)) if s.strip()]
    out, cur = [], ""
    for s in sents:
        if cur and len(cur) + len(s) > target:
            out.append(cur)
            cur = s
        else:
            cur = f"{cur} {s}".strip()
    if cur:
        out.append(cur)
    return out


def bm25_rank(query: str, docs: list, k1: float = 1.4, b: float = 0.75) -> list:
    """Return BM25 scores of each doc (a string) against the query."""
    q = set(tokens(query))
    toks = [tokens(d) for d in docs]
    if not q or not toks:
        return [0.0] * len(docs)
    avg = sum(len(t) for t in toks) / len(toks) or 1.0
    df = Counter(w for t in toks for w in set(t) if w in q)
    n = len(toks)
    scores = []
    for t in toks:
        tf = Counter(t)
        s = 0.0
        for w in q:
            if not tf[w]:
                continue
            idf = math.log(1 + (n - df[w] + 0.5) / (df[w] + 0.5))
            s += idf * tf[w] * (k1 + 1) / (tf[w] + k1 * (1 - b + b * len(t) / avg))
        scores.append(s)
    return scores


# Marks of a reference list, keyword line or citation block. Reference titles
# are dense with topic words, so BM25 ranks them high; they took about a sixth
# of the window. In-text citations like "(Wilson et al., 2006)" don't match.
_REFERENCE_MARK = re.compile(
    r"Google Scholar|PubMed|CrossRef|Article CAS|\[ DOI \]|\bdoi[:.]\s*10\.|doi\.org/|"
    r"Download citation|\bKW - |\b\d+\s*\(\d+\)\s*:\s*\d+|\b\d+\s*:\s*\d+\s*[–-]\s*\d+", re.I)


def is_reference(passage: str) -> bool:
    return len(_REFERENCE_MARK.findall(passage)) >= 2


def focus_text(claim: str, full_text: str, max_chars: int, skip: tuple = ()) -> str:
    """The abstract plus the claim's best-matching passages, within max_chars.
    Reference lists, and the sections named in `skip`, are left out.

    Returns full_text unchanged when it already fits (or max_chars <= 0)."""
    if max_chars <= 0 or len(full_text or "") <= max_chars:
        return full_text or ""
    secs = split_sections(full_text)
    keep, budget = [], max_chars
    # The abstract frames the paper; keep (the start of) it unconditionally.
    pool = []
    for si, (bucket, body) in enumerate(secs):
        parts, n_head = passages(body), 0
        if bucket == "abstract":
            # Keep the abstract's opening passages; anything past a quarter of
            # the budget (an unsectioned paper parsed as one long "abstract")
            # competes for space like any other passage.
            used = 0
            for p in parts:
                if used + len(p) > max_chars // 4:
                    break
                used += len(p) + 16
                n_head += 1
            keep.extend((si, pi, p) for pi, p in enumerate(parts[:n_head]))
            budget -= used
        pool.extend((si, pi, p) for pi, p in enumerate(parts)
                    if pi >= n_head and bucket != "references" and bucket not in skip
                    and not is_reference(p))
    scores = bm25_rank(claim, [p for _, _, p in pool])
    for (si, pi, p), _ in sorted(zip(pool, scores), key=lambda x: -x[1]):
        if len(p) + 16 > budget:
            continue
        keep.append((si, pi, p))
        budget -= len(p) + 16
        if budget < 200:
            break
    # Reassemble in document order under the original section headers, marking
    # gaps so the judge knows it is reading excerpts.
    out, last_sec, last_pi = [], None, None
    for si, pi, p in sorted(keep):
        if si != last_sec:
            out.append(f"\n## {secs[si][0].upper()} (excerpts)\n")
            last_sec, last_pi = si, None
        elif last_pi is not None and pi != last_pi + 1:
            out.append("[…]\n")
        out.append(p + "\n")
        last_pi = pi
    return "".join(out).strip()
