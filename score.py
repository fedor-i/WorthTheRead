"""Aggregate per-claim verdicts into one confidence score and the ranked
"worth your read" source list."""
from .judge import DISTORTION_LABELS

_VERDICT_WEIGHT = {"SUPPORTED": 1.0, "OVERSTATED": 0.4,
                   "INSUFFICIENT": 0.0, "UNSUPPORTED": 0.0}
_VERDICT_RANK = {"SUPPORTED": 0, "OVERSTATED": 1,
                 "INSUFFICIENT": 2, "UNSUPPORTED": 3}


def score_answer(results: list, min_confidence: float = 0.0) -> dict:
    """results: list of per-claim dicts from verify_claim.

    min_confidence: drop any source whose supporting verdict is below this
    confidence from worth_your_read entirely ("don't even show me these").

    Returns overall_confidence (0-1), a letter grade, counts by verdict, the
    threshold used, how many sources it screened out, and worth_your_read:
    sources that genuinely support claims at or above the threshold, best first.
    """
    if not results:
        return {"overall_confidence": 0.0, "grade": "N/A", "counts": {},
                "worth_your_read": [], "n_claims": 0,
                "min_confidence": min_confidence, "excluded_below_threshold": 0}

    counts = {}
    score_sum = 0.0
    for r in results:
        v = r.get("verdict", "INSUFFICIENT")
        counts[v] = counts.get(v, 0) + 1
        w = _VERDICT_WEIGHT.get(v, 0.0) * float(r.get("confidence", 0.0))
        score_sum += w
    overall = score_sum / len(results)

    # Build the ranked list, deduped by source (a paper that supports several
    # claims appears once, keeping its strongest verdict + a claim count).
    by_key = {}
    excluded = 0
    for r in results:
        if r.get("verdict") not in ("SUPPORTED", "OVERSTATED"):
            continue
        if r.get("retracted"):
            continue
        if float(r.get("confidence", 0.0)) < min_confidence:
            excluded += 1  # below the caller's threshold: don't surface it
            continue
        res = r.get("resolved", {})
        key = res.get("doi") or res.get("title") or r.get("reference")
        entry = {
            "title": res.get("title") or r.get("reference"),
            "doi": res.get("doi"),
            "verdict": r.get("verdict"),
            "confidence": r.get("confidence", 0.0),
            "has_results": r.get("has_results", False),
            "reason": _why(r),
            "claims_supported": 1,
            "_sort": (_VERDICT_RANK.get(r.get("verdict"), 9),
                      -float(r.get("confidence", 0.0)),
                      0 if r.get("has_fulltext", r.get("has_results")) else 1),
        }
        if key not in by_key:
            by_key[key] = entry
        else:
            prev = by_key[key]
            count = prev["claims_supported"] + 1
            # keep the stronger verdict/evidence for display
            best = entry if entry["_sort"] < prev["_sort"] else prev
            best["claims_supported"] = count
            by_key[key] = best

    worth = list(by_key.values())
    worth.sort(key=lambda x: x.pop("_sort"))
    for w in worth:
        if w["claims_supported"] > 1:
            w["reason"] += f" Supports {w['claims_supported']} of the claims."

    return {
        "overall_confidence": round(overall, 2),
        "grade": _grade(overall),
        "counts": counts,
        "n_claims": len(results),
        "min_confidence": min_confidence,
        "excluded_below_threshold": excluded,
        "worth_your_read": worth,
    }


def _why(r: dict) -> str:
    v = r.get("verdict")
    src = "full-text" if r.get("has_fulltext", r.get("has_results")) else "abstract-only"
    if v == "SUPPORTED":
        return f"Directly supported at {src} with verified quotes."
    if v == "OVERSTATED":
        d = DISTORTION_LABELS.get(r.get("distortion_type"), "overstated")
        return f"Supports a weaker claim ({src}); read for context ({d})."
    return "Read with caution."


def _grade(x: float) -> str:
    if x >= 0.85:
        return "A"
    if x >= 0.7:
        return "B"
    if x >= 0.5:
        return "C"
    if x >= 0.3:
        return "D"
    return "F"
