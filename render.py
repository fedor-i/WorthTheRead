"""Render concise, scannable output — a one-line `summary` plus a tight verdict
card — so results read well both for an agent relaying them and a human looking
at them, instead of a wall of fields. Also renders a self-contained HTML view
(verdict badge, verified quotes, links to the full text) for verify_claim and
score_answer, so a result can be opened, shared, or embedded as a page."""
import html as _html
import re

from .judge import DISTORTION_LABELS

_BADGE = {"SUPPORTED": "✅ SUPPORTED", "OVERSTATED": "🟠 OVERSTATED",
          "UNSUPPORTED": "❌ UNSUPPORTED", "INSUFFICIENT": "⚪ INSUFFICIENT"}
_PLAIN = {"SUPPORTED": "the source supports this claim",
          "OVERSTATED": "the claim overstates what the source shows",
          "UNSUPPORTED": "the source does not support this claim",
          "INSUFFICIENT": "can't be verified from the available text"}


def _first_sentence(text: str, cap: int = 220) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    text = text.split("\n", 1)[0].strip()          # lead line (drop list tails)
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
    elif v == "SUPPORTED" and not r.get("has_fulltext", r.get("has_results")):
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
    flags = ["full text" if r.get("has_fulltext", r.get("has_results")) else "abstract only"]
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


# --- HTML view (opt-in: easy-to-see results with links to the full text) ----

_HTML_COLORS = {       # (text/border, soft background) per verdict
    "SUPPORTED":    ("#1a7f37", "#e6f4ea"),
    "OVERSTATED":   ("#b36b00", "#fdf2e2"),
    "UNSUPPORTED":  ("#c01c28", "#fce8ea"),
    "INSUFFICIENT": ("#5c6370", "#eceef1"),
}
_STATUS_ICON = {"ENTAILED": "✓", "CONTRADICTED": "✗", "ABSENT": "—"}

_PAGE_CSS = """
:root{--fg:#1b1b1f;--muted:#5c6370;--bg:#ffffff;--card:#f7f8fa;--border:#e3e6ea;--quote:#f0f2f5}
@media (prefers-color-scheme:dark){:root{--fg:#e6e6e8;--muted:#9aa0aa;--bg:#17181c;--card:#1f2127;--border:#2c2f36;--quote:#23262c}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
padding:24px 16px}.wrap{max-width:780px;margin:0 auto}
.vc{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:18px 20px;margin:0 0 16px}
.badge{display:inline-block;font-weight:700;font-size:13px;padding:3px 10px;border-radius:999px;border:1px solid}
.meta{color:var(--muted);font-size:13px;margin-left:8px}
.claim{font-size:16px;font-weight:600;margin:12px 0 4px}
.src{color:var(--muted);font-size:13px;margin:0 0 10px}
.links a{display:inline-block;margin:2px 10px 2px 0;font-size:13px;text-decoration:none;color:#2b6cb0}
.links a:hover{text-decoration:underline}
.aspects{margin:10px 0;padding:0;list-style:none;font-size:13.5px}
.aspects li{margin:2px 0}.ent{color:#1a7f37}.con{color:#c01c28}.abs{color:var(--muted)}
blockquote{margin:8px 0;padding:8px 12px;background:var(--quote);border-left:3px solid var(--border);
border-radius:0 6px 6px 0;font-size:14px}
.why{font-size:13.5px;color:var(--fg);margin:8px 0 0}.gap{font-size:13px;color:var(--muted);margin:6px 0 0}
h1{font-size:20px;margin:0 0 4px}.overall{font-size:14px;color:var(--muted);margin:0 0 18px}
.wyr{padding-left:20px}.wyr li{margin:6px 0}
"""


def _esc(s) -> str:
    return _html.escape(str(s or ""))


def source_links(r: dict) -> list:
    """(label, url) links to the cited paper's full text, most direct first."""
    res = r.get("resolved") or {}
    doi = res.get("doi") or r.get("verified_reference") or ""
    pmcid = res.get("pmcid") or ""
    out = []
    if pmcid:
        pid = pmcid if str(pmcid).upper().startswith("PMC") else "PMC" + str(pmcid)
        out.append(("Full text (PMC)",
                    f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pid}/"))
    if doi:
        out.append(("DOI", f"https://doi.org/{doi}"))
    return out


def verdict_html(r: dict, fragment: bool = False) -> str:
    """Self-contained HTML for one verdict. fragment=True returns just the card
    (for embedding in a scorecard); otherwise a full standalone page."""
    v = r.get("verdict", "INSUFFICIENT")
    fg, bg = _HTML_COLORS.get(v, _HTML_COLORS["INSUFFICIENT"])
    res = r.get("resolved") or {}
    flags = ["full text" if r.get("has_fulltext", r.get("has_results")) else "abstract only"]
    if r.get("retracted"):
        flags.insert(0, "🚩 RETRACTED")
    if r.get("citation_ok") is False and not r.get("verified_from_preprint"):
        flags.append("⚠ wrong-paper?")
    conf = r.get("confidence", 0) or 0

    p = [f'<div class="vc">']
    p.append(f'<span class="badge" style="color:{fg};border-color:{fg};'
             f'background:{bg}">{_BADGE.get(v, v)}</span>')
    p.append(f'<span class="meta">{r.get("band","?")} confidence '
             f'{conf:.2f} · {" · ".join(_esc(f) for f in flags)}</span>')
    p.append(f'<div class="claim">{_esc(r.get("claim",""))}</div>')
    if res.get("title"):
        who = (res.get("authors", "").split(",")[0] + ", "
               if res.get("authors") else "") + str(res.get("year") or "")
        p.append(f'<div class="src">{_esc(res["title"])}'
                 + (f' — {_esc(who.strip(", "))}' if who.strip(", ") else "")
                 + (f' · <i>{_esc(res["journal"])}</i>' if res.get("journal") else "")
                 + "</div>")
    links = source_links(r)
    if links:
        p.append('<div class="links">' + "".join(
            f'<a href="{_esc(u)}" target="_blank" rel="noopener">{_esc(lbl)} ↗</a>'
            for lbl, u in links) + "</div>")

    asps = r.get("aspects") or []
    if asps:
        p.append('<ul class="aspects">')
        for a in asps:
            st = a.get("status", "")
            cls = {"ENTAILED": "ent", "CONTRADICTED": "con"}.get(st, "abs")
            line = (f'<span class="{cls}">{_STATUS_ICON.get(st,"?")} '
                    f'{_esc(a.get("type",""))}</span> — {_esc(a.get("text",""))}')
            if a.get("quote"):
                line += f'<blockquote>“{_esc(a["quote"])}”</blockquote>'
            p.append(f"<li>{line}</li>")
        p.append("</ul>")
    else:
        ev = r.get("evidence") or []
        if ev and ev[0].get("quote"):
            p.append(f'<blockquote>“{_esc(ev[0]["quote"])}”</blockquote>')

    if v == "OVERSTATED" and r.get("distortion_note"):
        d = DISTORTION_LABELS.get(r.get("distortion_type"),
                                  r.get("distortion_type") or "")
        p.append(f'<p class="why"><b>Overstatement ({_esc(d)}).</b> '
                 f'{_esc(_first_sentence(r["distortion_note"]))}</p>')
    elif r.get("rationale"):
        p.append(f'<p class="why"><b>Why.</b> '
                 f'{_esc(_first_sentence(r["rationale"]))}</p>')
    if r.get("claim_aspects_unmet"):
        p.append(f'<p class="gap"><b>Gap.</b> '
                 f'{_esc("; ".join(r["claim_aspects_unmet"])[:240])}</p>')
    p.append("</div>")
    card = "\n".join(p)
    if fragment:
        return card
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>Worth Your Read — verdict</title><style>{_PAGE_CSS}</style>"
            f"</head><body><div class='wrap'>{card}</div></body></html>")


def scorecard_html(s: dict, cards: list = None) -> str:
    """Self-contained HTML report for score_answer: overall grade, the 'worth
    your read' ranked list with links, then each claim's verdict card."""
    sup = s.get("counts", {}).get("SUPPORTED", 0)
    body = ["<div class='wrap'>",
            f"<h1>Worth Your Read — scorecard</h1>",
            f"<p class='overall'><b>Grade {_esc(s.get('grade','?'))} "
            f"({s.get('overall_confidence',0):.2f})</b> — {sup}/{s.get('n_claims',0)} "
            f"claims supported · "
            + " · ".join(f"{_esc(k)}: {v}" for k, v in (s.get("counts") or {}).items())
            + "</p>"]
    body.append("<h2 style='font-size:16px'>Worth your read</h2>")
    wyr = s.get("worth_your_read") or []
    if not wyr:
        body.append("<p class='gap'>No sources cleared the bar.</p>")
    else:
        body.append("<ol class='wyr'>")
        for w in wyr:
            link = (f" — <a href='https://doi.org/{_esc(w['doi'])}' target='_blank' "
                    f"rel='noopener'>{_esc(w['doi'])} ↗</a>") if w.get("doi") else ""
            body.append(f"<li><b>{_esc(w.get('title',''))}</b>{link}"
                        f"<br><span class='gap'>{_esc(w.get('reason',''))}</span></li>")
        body.append("</ol>")
    for c in (cards or []):
        body.append(verdict_html(c, fragment=True))
    body.append("</div>")
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>Worth Your Read — scorecard</title><style>{_PAGE_CSS}</style>"
            f"</head><body>{''.join(body)}</body></html>")


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
