"""Render human-readable verdict cards (markdown) so the output is
comprehensible inline in any MCP host, not just machine JSON."""
from .judge import DISTORTION_LABELS

_BADGE = {
    "SUPPORTED": "✓ SUPPORTED",
    "OVERSTATED": "⚠ OVERSTATED",
    "UNSUPPORTED": "✗ UNSUPPORTED",
    "INSUFFICIENT": "— INSUFFICIENT EVIDENCE",
}


def render_card(r: dict) -> str:
    res = r.get("resolved", {})
    lines = [f"**{_BADGE.get(r['verdict'], r['verdict'])}** · "
             f"confidence: {r.get('band', '?')} "
             f"({r.get('confidence', 0):.2f})"]

    # citation / link status line
    status = []
    status.append("link ✓" if r.get("citation_ok") else "link ⚠")
    if r.get("retracted"):
        status.append("**RETRACTED**")
    elif r.get("concern"):
        status.append("expression of concern")
    status.append("full text ✓" if r.get("has_results")
                  else "abstract only ⚠")
    lines.append(" · ".join(status))

    # Preprint <-> published-journal linkage: peer-review status is a trust cue.
    pub = r.get("published_as")
    if pub and pub.get("journal"):
        yr = f" ({pub['year']})" if pub.get("year") else ""
        link = f" ([{pub['doi']}](https://doi.org/{pub['doi']}))" if pub.get("doi") else ""
        lines.append(f"📄 preprint — later published in *{pub['journal']}*{yr}{link}")
    elif r.get("is_preprint"):
        lines.append("📄 preprint — no peer-reviewed journal version found yet")
    elif r.get("has_preprint") and r["has_preprint"].get("doi"):
        d = r["has_preprint"]["doi"]
        lines.append(f"📄 has a preprint ([{d}](https://doi.org/{d}))")
    lines.append("")

    lines.append(f"**Claim:** {r.get('claim', '')}")
    if res.get("title"):
        cite = res["title"]
        meta = ", ".join(x for x in (res.get("authors", "").split(",")[0]
                                     if res.get("authors") else None,
                                     str(res.get("year") or "")) if x)
        lines.append(f"**Paper:** {cite}" + (f" ({meta})" if meta else ""))

    asps = r.get("aspects") or []
    if asps:
        icon = {"ENTAILED": "✓", "CONTRADICTED": "✗", "ABSENT": "—"}
        lines.append("")
        lines.append("**Claim breakdown** (each piece checked separately):")
        for a in asps:
            mark = icon.get(a.get("status"), "?")
            lines.append(f"- {mark} **{a.get('type', '')}** — {a.get('text', '')}")
            if a.get("quote"):
                lines.append(f"  > “{a['quote']}”")

    # Aspects already carry the verified quotes; show the standalone evidence
    # block only when there's no decomposition.
    ev = r.get("evidence") or []
    if ev and not asps:
        lines.append("")
        lines.append("**Verified evidence:**")
        for e in ev[:3]:
            sec = e.get("section", "")
            lines.append(f"> “{e['quote']}”" + (f" — §{sec}" if sec else ""))

    if r.get("rationale"):
        lines.append("")
        lines.append(f"**Why:** {r['rationale']}")

    if r.get("verdict") == "OVERSTATED" and r.get("distortion_note"):
        d = DISTORTION_LABELS.get(r.get("distortion_type"),
                                  r.get("distortion_type") or "")
        lines.append("")
        lines.append(f"**Distortion ({d}):** {r['distortion_note']}")

    if r.get("claim_aspects_unmet"):
        lines.append("")
        lines.append("**Not established:** "
                     + "; ".join(r["claim_aspects_unmet"]))

    if r.get("downgraded"):
        lines.append("")
        lines.append("_Downgraded to insufficient: no quote could be verified "
                     "against the source text._")
    return "\n".join(lines)


def render_scorecard(s: dict) -> str:
    lines = [f"## Overall confidence: {s['overall_confidence']:.2f} "
             f"(grade {s['grade']}) across {s['n_claims']} claim(s)"]
    if s.get("counts"):
        lines.append(" · ".join(f"{k}: {v}" for k, v in s["counts"].items()))
    if s.get("min_confidence"):
        lines.append(f"Confidence threshold: {s['min_confidence']:.2f} — "
                     f"{s.get('excluded_below_threshold', 0)} source(s) screened out below it")
    lines.append("")
    lines.append("### Worth your read")
    if not s.get("worth_your_read"):
        lines.append("_No sources cleared the bar._")
    for i, w in enumerate(s["worth_your_read"], 1):
        doi = f" ([{w['doi']}](https://doi.org/{w['doi']}))" if w.get("doi") else ""
        lines.append(f"{i}. **{w['title']}**{doi} — {w['reason']}")
    return "\n".join(lines)
