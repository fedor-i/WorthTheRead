"""Render concise, scannable output — a one-line `summary` plus a tight verdict
card — so results read well both for an agent relaying them and a human looking
at them, instead of a wall of fields."""
import re

from .judge import DISTORTION_LABELS

_BADGE = {"SUPPORTED": "✅ SUPPORTED", "OVERSTATED": "🟠 OVERSTATED",
          "UNSUPPORTED": "❌ UNSUPPORTED", "INSUFFICIENT": "⚪ INSUFFICIENT"}
_PLAIN = {"SUPPORTED": "the source supports this claim",
          "OVERSTATED": "the claim overstates what the source shows",
          "UNSUPPORTED": "the source does not support this claim",
          "INSUFFICIENT": "can't be verified from the available text"}


def _first_sentence(text: str, cap: int = 200) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    s = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]
    return s if len(s) <= cap else text[:cap].rstrip() + "…"


def summary(r: dict) -> str:
    """One scannable line: verdict + plain meaning + the key caveat."""
    v = r.get("verdict", "?")
    band = (r.get("band") or "?").lower()
    extra = ""
    if r.get("retracted"):
        extra = " — ⚠ the cited paper is RETRACTED"
    elif r.get("citation_ok") is False and not r.get("verified_from_preprint"):
        extra = " — ⚠ the citation may be the wrong paper"
    elif v == "SUPPORTED" and not r.get("has_results"):
        extra = " (abstract-level)"
    return f"{v} · {band} confidence — {_PLAIN.get(v, v)}{extra}."


def _linkage_line(r: dict):
    pub = r.get("published_as")
    if r.get("verified_from_preprint"):
        return "↳ verified against the published version of record (you cited a preprint)"
    if pub and pub.get("journal"):
        yr = f" {pub['year']}" if pub.get("year") else ""
        return f"↳ this is a preprint — later published in {pub['journal']}{yr}"
    if r.get("is_preprint"):
        return "↳ preprint — no peer-reviewed version found yet"
    if r.get("has_preprint") and r["has_preprint"].get("doi"):
        return f"↳ has a preprint ({r['has_preprint']['doi']})"
    return None


def render_card(r: dict) -> str:
    res = r.get("resolved", {})
    flags = ["full text" if r.get("has_results") else "abstract only"]
    if r.get("retracted"):
        flags.insert(0, "🚩 RETRACTED")
    elif r.get("concern"):
        flags.insert(0, "concern")
    if r.get("citation_ok") is False and not r.get("verified_from_preprint"):
        flags.append("⚠ wrong-paper?")
    lines = [f"**{_BADGE.get(r['verdict'], r['verdict'])}** · "
             f"{r.get('band', '?')} ({r.get('confidence', 0):.2f}) · "
             + " · ".join(flags)]
    link = _linkage_line(r)
    if link:
        lines.append(link)
    lines.append("")

    lines.append(f"**Claim.** {r.get('claim', '')}")
    if res.get("title"):
        who = (res.get("authors", "").split(",")[0] + ", "
               if res.get("authors") else "") + str(res.get("year") or "")
        who = who.strip(", ")
        lines.append(f"**Source.** {res['title']}" + (f" — {who}" if who else ""))

    asps = r.get("aspects") or []
    if asps:
        icon = {"ENTAILED": "✓", "CONTRADICTED": "✗", "ABSENT": "—"}
        lines.append("**Checked.** " + " · ".join(
            f"{icon.get(a.get('status'), '?')} {a.get('type', '')}" for a in asps))
        quote = next((a["quote"] for a in asps if a.get("quote")), None)
        if quote:
            lines.append(f"> “{quote}”")
    else:
        ev = r.get("evidence") or []
        if ev and ev[0].get("quote"):
            lines.append(f"> “{ev[0]['quote']}”")

    if r.get("verdict") == "OVERSTATED" and r.get("distortion_note"):
        d = DISTORTION_LABELS.get(r.get("distortion_type"),
                                  r.get("distortion_type") or "")
        lines.append(f"**Overstatement ({d}).** {_first_sentence(r['distortion_note'])}")
    elif r.get("rationale"):
        lines.append(f"**Why.** {_first_sentence(r['rationale'])}")

    if r.get("claim_aspects_unmet"):
        lines.append(f"**Gap.** {'; '.join(r['claim_aspects_unmet'])[:220]}")
    return "\n".join(lines)


def render_scorecard(s: dict) -> str:
    sup = s.get("counts", {}).get("SUPPORTED", 0)
    lines = [f"**Overall: grade {s['grade']} ({s['overall_confidence']:.2f})** — "
             f"{sup}/{s['n_claims']} claims supported; "
             f"{len(s.get('worth_your_read', []))} source(s) worth reading.", ""]
    if s.get("counts"):
        lines.append(" · ".join(f"{k}: {v}" for k, v in s["counts"].items()))
    if s.get("min_confidence"):
        lines.append(f"(threshold {s['min_confidence']:.2f} — "
                     f"{s.get('excluded_below_threshold', 0)} screened out)")
    lines.append("")
    lines.append("### Worth your read")
    if not s.get("worth_your_read"):
        lines.append("_No sources cleared the bar._")
    for i, w in enumerate(s["worth_your_read"], 1):
        doi = f" ([{w['doi']}](https://doi.org/{w['doi']}))" if w.get("doi") else ""
        lines.append(f"{i}. **{w['title']}**{doi} — {w['reason']}")
    return "\n".join(lines)
