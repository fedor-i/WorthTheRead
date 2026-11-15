# CONTRACT: what the website and Zotero may rely on (server.py builds these dicts).
# Every tool result has `summary` (plain English, one line; for ask_papers the whole
# markdown answer) and `card` (markdown detail; ask_papers adds 2 quotes per claim). Keys:
#  verify_claim: verdict, confidence, band, claim, reference, resolved{title,authors,year,journal,doi,pmcid},
#   aspects[{type,status,text,quote,close_match}], evidence[{quote,section,close_match}], rationale,
#   distortion_type, distortion_note, claim_aspects_unmet, retracted, concern, citation_ok, has_fulltext,
#   is_preprint, published_as, has_preprint, verified_from_preprint, judge_backend; html if html=True
#  score_answer: grade, overall_confidence, n_claims, counts, worth_your_read[{title,doi,verdict,confidence,reason}], claims[]
#  revise_claim: revised, original_claim, claim, verdict, strength, support_count, total_refs, added_refs,
#   revisions[{claim,verdict,strength,support_count,total_refs}], references[] (discovered=True if found), verification
#  suggest_sources: claim, checked, count, suggested[{doi,title,year,venue,verdict,confidence}]
#  audit_paper: paper, section, available, note, counts, claims[{claim,verdict,verified_against,rationale,suggestion}]
#  find_sources: query, count, sources[]; plan_search: mode "clarify"|"plan"; ask_papers: question, overview,
#   claims[{claim,status GROUNDED|UNGROUNDED,sources[{paper,quote,page}],against,unsourced_numbers}], papers[{n,label,used,abstract_only}], not_addressed
# Labels (copy verbatim): VERDICT_BADGE, VERDICT_MEANING, ASPECT_ICON, CLOSE_MATCH, NO_QUOTE, UNSOURCED_NUMBERS,
#  FULL_TEXT/ABSTRACT_ONLY, RETRACTED, CONCERN, WRONG_PAPER, FOUND_BY_SEARCH, NOTHING_USED, NO_SOURCES, PARTLY_SUPPORTS.
"""Render tool results for people and agents: a one-line `summary`, a tight
markdown `card`, and (opt-in) a self-contained HTML page for verify_claim and
score_answer with the verdict badge, verified quotes and full-text links.

All user-facing wording (verdict names, flags, warnings) lives in the
constants below so every renderer, and every surface that copies them, says
the same thing."""
import html as _html
import re

from .judge import DISTORTION_LABELS

# --- shared wording ----------------------------------------------------------

VERDICTS = ("SUPPORTED", "OVERSTATED", "UNSUPPORTED", "INSUFFICIENT")
VERDICT_BADGE = {"SUPPORTED": "✅ SUPPORTED", "OVERSTATED": "🟠 OVERSTATED",
                 "UNSUPPORTED": "❌ UNSUPPORTED", "INSUFFICIENT": "⚪ INSUFFICIENT"}
VERDICT_MEANING = {"SUPPORTED": "the source supports this claim",
                   "OVERSTATED": "the claim overstates what the source shows",
                   "UNSUPPORTED": "the source does not support this claim",
                   "INSUFFICIENT": "can't be verified from the available text"}
ASPECT_ICON = {"ENTAILED": "✓", "CONTRADICTED": "✗", "ABSENT": "—"}

# A quote that matched the paper only loosely (its start, a long run, or most
# of its words): it counts as evidence, but it isn't the paper's exact wording.
CLOSE_MATCH = "close match: wording differs from the paper"
NO_QUOTE = "no quote backs this"                 # ask_papers claim with status UNGROUNDED
UNSOURCED_NUMBERS = "numbers not in any quote"   # followed by ": 5, 12"
FULL_TEXT, ABSTRACT_ONLY = "full text", "abstract only"
ABSTRACT_LEVEL = "abstract-level"                # SUPPORTED read from the abstract only
RETRACTED = "🚩 RETRACTED"
RETRACTED_WARNING = "⚠ the cited paper is RETRACTED"
CONCERN = "concern"                              # expression of concern on the paper
WRONG_PAPER = "⚠ wrong-paper?"
WRONG_PAPER_WARNING = "⚠ the citation may be the wrong paper"
FOUND_BY_SEARCH = "🔎 found by search"           # a reference revise_claim discovered
NOTHING_USED = "nothing used"                    # a paper read but not cited by any claim
NO_SOURCES = "No sources cleared the bar."
PARTLY_SUPPORTS = "only partly supports the claim"   # an OVERSTATED suggestion

_CLOSE_MD = f" *({CLOSE_MATCH})*"
_CLOSE_HTML = f' <small class="close">({CLOSE_MATCH})</small>'


# --- shared pieces -----------------------------------------------------------

def _first_sentence(text: str, cap: int = 220) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    text = text.split("\n", 1)[0].strip()          # lead line (drop list tails)
    s = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]
    return s if len(s) <= cap else text[:cap].rstrip() + "…"


def _has_fulltext(r: dict) -> bool:
    return bool(r.get("has_fulltext", r.get("has_results")))


def _wrong_paper(r: dict) -> bool:
    return r.get("citation_ok") is False and not r.get("verified_from_preprint")


def _flags(r: dict) -> list:
    """Status flags shown next to the verdict, most serious first."""
    flags = [FULL_TEXT if _has_fulltext(r) else ABSTRACT_ONLY]
    if r.get("retracted"):
        flags.insert(0, RETRACTED)
    elif r.get("concern"):
        flags.insert(0, CONCERN)
    if _wrong_paper(r):
        flags.append(WRONG_PAPER)
    if r.get("discovered"):
        flags.append(FOUND_BY_SEARCH)
    return flags


def _who(res: dict) -> str:
    """'First author, year' (either part may be missing)."""
    first = res.get("authors", "").split(",")[0] + ", " if res.get("authors") else ""
    return (first + str(res.get("year") or "")).strip(", ")


def _lead_quote(r: dict):
    """The quote a compact card shows: the first aspect quote, else the first
    evidence quote. Returns the quote dict or None."""
    asps = r.get("aspects") or []
    if asps:
        return next((a for a in asps if a.get("quote")), None)
    ev = r.get("evidence") or []
    return ev[0] if ev and ev[0].get("quote") else None


def _where(q: dict) -> str:
    """' — §results, p. 4' for a quote that says where it came from."""
    bits = [f"§{q['section']}" if q.get("section") else "",
            f"p. {q['page']}" if q.get("page") else ""]
    bits = [b for b in bits if b]
    return " — " + ", ".join(bits) if bits else ""


def _reason(r: dict):
    """(label, sentence) explaining the verdict, or None."""
    if r.get("verdict") == "OVERSTATED" and r.get("distortion_note"):
        d = DISTORTION_LABELS.get(r.get("distortion_type"), r.get("distortion_type") or "")
        return f"Overstatement ({d})", _first_sentence(r["distortion_note"])
    if r.get("rationale"):
        return "Why", _first_sentence(r["rationale"])
    return None


def _doi_md(doi) -> str:
    return f" ([{doi}](https://doi.org/{doi}))" if doi else ""


# --- verify_claim ------------------------------------------------------------

def summary(r: dict) -> str:
    """One scannable line: verdict + plain meaning + the key caveat."""
    v = r.get("verdict", "?")
    band = (r.get("band") or "?").lower()
    extra = ""
    if r.get("retracted"):
        extra = f" — {RETRACTED_WARNING}"
    elif _wrong_paper(r):
        extra = f" — {WRONG_PAPER_WARNING}"
    elif v == "SUPPORTED" and not _has_fulltext(r):
        extra = f" ({ABSTRACT_LEVEL})"
    return f"{v} · {band} confidence — {VERDICT_MEANING.get(v, v)}{extra}."


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
    """Markdown verdict card: badge line, claim, source, checked aspects, the
    lead quote, why, and what the source doesn't establish."""
    res = r.get("resolved") or {}
    lines = [f"**{VERDICT_BADGE.get(r['verdict'], r['verdict'])}** · "
             f"{r.get('band', '?')} ({r.get('confidence', 0):.2f}) · "
             + " · ".join(_flags(r))]
    link = _linkage_line(r)
    if link:
        lines.append(link)
    lines.append("")

    lines.append(f"**Claim.** {r.get('claim', '')}")
    if res.get("title"):
        who = _who(res)
        lines.append(f"**Source.** {res['title']}" + (f" — {who}" if who else "")
                     + _doi_md(res.get("doi")))

    asps = r.get("aspects") or []
    if asps:
        lines.append("**Checked.** " + " · ".join(
            f"{ASPECT_ICON.get(a.get('status'), '?')} {a.get('type', '')}" for a in asps))
    q = _lead_quote(r)
    if q:
        lines.append(f"> “{q['quote']}”{_where(q)}" + _CLOSE_MD * bool(q.get("close_match")))

    why = _reason(r)
    if why:
        lines.append(f"**{why[0]}.** {why[1]}")
    if r.get("claim_aspects_unmet"):
        lines.append(f"**Gap.** {'; '.join(r['claim_aspects_unmet'])[:220]}")
    return "\n".join(lines)


# --- score_answer ------------------------------------------------------------

def _n_supported(s: dict) -> int:
    return (s.get("counts") or {}).get("SUPPORTED", 0)


def score_summary(s: dict) -> str:
    return (f"Grade {s['grade']} ({s['overall_confidence']:.2f}): "
            f"{_n_supported(s)}/{s['n_claims']} claims supported, "
            f"{len(s.get('worth_your_read', []))} source(s) worth reading.")


def render_scorecard(s: dict) -> str:
    wyr = s.get("worth_your_read") or []
    lines = [f"**Overall: grade {s['grade']} ({s['overall_confidence']:.2f})** — "
             f"{_n_supported(s)}/{s['n_claims']} claims supported; "
             f"{len(wyr)} source(s) worth reading.", ""]
    if s.get("counts"):
        lines.append(" · ".join(f"{k}: {v}" for k, v in s["counts"].items()))
    if s.get("min_confidence"):
        lines.append(f"(threshold {s['min_confidence']:.2f} — "
                     f"{s.get('excluded_below_threshold', 0)} screened out)")
    lines.append("")
    lines.append("### Worth your read")
    if not wyr:
        lines.append(f"_{NO_SOURCES}_")
    for i, w in enumerate(wyr, 1):
        tag = (f" `{w['verdict']} {w.get('confidence') or 0:.2f}`"
               if w.get("verdict") else "")
        lines.append(f"{i}. **{w['title']}**{_doi_md(w.get('doi'))}{tag} — {w['reason']}")
    return "\n".join(lines)


# --- revise_claim ------------------------------------------------------------

def revise_summary(r: dict) -> str:
    start = (r.get("revisions") or [{}])[0].get("verdict", r["verdict"])
    if r.get("revised"):
        return (f"Rewrote the claim ({start} → {r['verdict']}); "
                f"now backed by {r['support_count']} source(s) ({r['strength']}).")
    return (f"Claim unchanged ({r['verdict']}); "
            f"{r['support_count']} supporting source(s).")


def revise_card(r: dict) -> str:
    """The loop's outcome on top (final claim, how many sources back it, the
    trail of rounds), then every reference's verdict card."""
    head = "Reworded" if r.get("revised") else "No rewording needed"
    lines = [f"**{VERDICT_BADGE.get(r['verdict'], r['verdict'])}** · supported by "
             f"{r['support_count']} of {r['total_refs']} reference(s) · strength {r['strength']}"
             + (f" · +{len(r['added_refs'])} found by search" if r.get("added_refs") else ""),
             "", f"**{head}.** {r['claim']}"]
    if r.get("revised"):
        lines.append(f"**Original.** {r['original_claim']}")
    trail = r.get("revisions") or []
    if len(trail) > 1:
        lines.append("**Rounds.** " + " → ".join(
            f"{t['verdict']} ({t['support_count']}/{t['total_refs']})" for t in trail))
    for ref in r.get("references") or []:
        lines += ["", "---", ref.get("card") or render_card(ref)]
    return "\n".join(lines)


# --- suggest_sources ---------------------------------------------------------

def suggest_summary(r: dict) -> str:
    sup = [s for s in r.get("suggested", []) if s["verdict"] == "SUPPORTED"]
    if sup:
        return (f"Found {len(sup)} supporting source(s) out of {r['checked']} checked — "
                f"best: {(sup[0].get('title') or sup[0].get('doi') or '?')[:60]}.")
    return f"No supporting source found in {r['checked']} candidates checked."


def suggest_card(r: dict) -> str:
    lines = [f"**Sources for:** {r.get('claim', '')}", ""]
    if not r.get("suggested"):
        lines.append(f"_No supporting source found in {r.get('checked', 0)} candidates checked._")
    for i, s in enumerate(r.get("suggested", []), 1):
        when = f" ({s['year']})" if s.get("year") else ""
        part = f" — {PARTLY_SUPPORTS}" if s["verdict"] == "OVERSTATED" else ""
        lines.append(f"{i}. **{VERDICT_BADGE.get(s['verdict'], s['verdict'])}** "
                     f"{s.get('confidence') or 0:.2f} · {s.get('title') or s.get('doi') or '?'}"
                     f"{when}{_doi_md(s.get('doi'))}{part}")
    return "\n".join(lines)


# --- audit_paper -------------------------------------------------------------

def audit_summary(out: dict) -> str:
    if not out.get("available"):
        return out.get("note", "Could not audit this paper.")
    claims = out.get("claims", [])
    nsup = sum(1 for c in claims if c.get("verdict") == "SUPPORTED")
    nfix = sum(1 for c in claims if c.get("suggestion"))
    return (f"Audited {len(claims)} cited claims in the {out.get('section')}: "
            f"{nsup} supported, {len(claims) - nsup} not fully supported"
            + (f"; suggested a replacement for {nfix}." if nfix else "."))


# --- find_sources / plan_search ----------------------------------------------

def sources_summary(r: dict) -> str:
    if not r.get("sources"):
        return f"No papers found for “{r.get('query', '')}”."
    top = r["sources"][0]
    return (f"Found {r['count']} candidate paper(s) — top: "
            f"{(top.get('title') or top.get('doi') or '?')[:60]}. Verify before citing.")


def sources_card(r: dict) -> str:
    lines = [f"**Candidate papers for:** {r.get('query', '')}", ""]
    for i, s in enumerate(r.get("sources", []), 1):
        meta = " · ".join(str(x) for x in (s.get("venue"), s.get("year")) if x)
        cites = f" · {s['citations']} citations" if s.get("citations") is not None else ""
        lines.append(f"{i}. **{s.get('title') or s.get('doi') or '?'}**"
                     + (f" — {meta}" if meta else "") + cites
                     + (" · open access" if s.get("open_access") else "")
                     + _doi_md(s.get("doi")))
    if not r.get("sources"):
        lines.append("_No papers found._")
    return "\n".join(lines)


def plan_summary(r: dict) -> str:
    if r.get("mode") == "clarify":
        return (f"{len(r.get('clarifying_questions', []))} quick question(s) to "
                "scope the search; answer them and call plan_search again.")
    return (f"Search plan: {len(r.get('queries', []))} query variant(s), gather "
            f"~{r.get('target_sources')} candidates, keep confidence ≥ {r.get('min_confidence')}.")


# --- ask_papers --------------------------------------------------------------

def _paper_refs(c: dict) -> str:
    return "".join(f" [{n}]" for n in sorted({s["paper"] for s in c.get("sources", [])}))


def _claim_flags(c: dict) -> str:
    out = f" *({NO_QUOTE})*" if c.get("status") != "GROUNDED" else ""
    if c.get("unsourced_numbers"):
        out += f" *({UNSOURCED_NUMBERS}: {', '.join(c['unsourced_numbers'])})*"
    return out


def ask_summary(r: dict, quotes: int = 0) -> str:
    """Markdown for an ask-across-papers result: the short answer, the merged
    claims with the papers behind them, and the papers read. quotes=N also
    shows up to N verified quotes under each claim (the `card`)."""
    if r.get("error"):
        return ask_error(r["error"])
    lines = [f"**{r.get('question', '')}**", ""]
    if r.get("overview"):
        lines += [" ".join(c["claim"] + _paper_refs(c) for c in r["overview"]), ""]
    for c in r.get("claims", []):
        against = sorted({s["paper"] for s in c.get("against", [])})
        lines.append(f"- {c['claim']}{_paper_refs(c)}{_claim_flags(c)}"
                     + (f" — disputed by {', '.join(f'[{n}]' for n in against)}" if against else ""))
        for s in c.get("sources", [])[:quotes]:
            lines.append(f"  > “{s['quote']}” — [{s['paper']}]"
                         + (f", p. {s['page']}" if s.get("page") else ""))
    if r.get("not_addressed"):
        lines += ["", "**Not addressed by these papers:** " + "; ".join(r["not_addressed"])]
    if r.get("papers"):
        lines += ["", "**Papers read**"] + [
            f"{p['n']}. {p['label']}" + (f" ({ABSTRACT_ONLY})" if p.get("abstract_only") else "")
            + ("" if p.get("used") else f" ({NOTHING_USED})") for p in r["papers"]]
    if r.get("skipped"):
        lines += ["", f"Not read: {len(r['skipped'])} paper(s)."]
    return "\n".join(lines)


def ask_card(r: dict) -> str:
    return ask_summary(r, quotes=2)


def ask_error(err) -> str:
    return f"Couldn't answer: {err}"


# --- HTML view (opt-in: easy-to-see results with links to the full text) ----

_HTML_COLORS = {       # (text/border, soft background) per verdict
    "SUPPORTED":    ("#1a7f37", "#e6f4ea"),
    "OVERSTATED":   ("#b36b00", "#fdf2e2"),
    "UNSUPPORTED":  ("#c01c28", "#fce8ea"),
    "INSUFFICIENT": ("#5c6370", "#eceef1"),
}

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
blockquote .close{color:var(--muted)}
.why{font-size:13.5px;color:var(--fg);margin:8px 0 0}.gap{font-size:13px;color:var(--muted);margin:6px 0 0}
h1{font-size:20px;margin:0 0 4px}.overall{font-size:14px;color:var(--muted);margin:0 0 18px}
.wyr{padding-left:20px}.wyr li{margin:6px 0}
"""


def _esc(s) -> str:
    return _html.escape(str(s or ""))


def _page(title: str, body: str) -> str:
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>Worth Your Read — {title}</title><style>{_PAGE_CSS}</style>"
            f"</head><body>{body}</body></html>")


def _quote_html(q: dict) -> str:
    return (f"<blockquote>“{_esc(q['quote'])}”{_esc(_where(q))}"
            f"{_CLOSE_HTML * bool(q.get('close_match'))}</blockquote>")


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
    conf = r.get("confidence", 0) or 0

    p = ['<div class="vc">',
         f'<span class="badge" style="color:{fg};border-color:{fg};'
         f'background:{bg}">{VERDICT_BADGE.get(v, v)}</span>',
         f'<span class="meta">{r.get("band", "?")} confidence '
         f'{conf:.2f} · {" · ".join(_esc(f) for f in _flags(r))}</span>',
         f'<div class="claim">{_esc(r.get("claim", ""))}</div>']
    link = _linkage_line(r)
    if link:
        p.append(f'<div class="src">{_esc(link)}</div>')
    if res.get("title"):
        who = _who(res)
        p.append(f'<div class="src">{_esc(res["title"])}'
                 + (f' — {_esc(who)}' if who else "")
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
            line = (f'<span class="{cls}">{ASPECT_ICON.get(st, "?")} '
                    f'{_esc(a.get("type", ""))}</span> — {_esc(a.get("text", ""))}')
            if a.get("quote"):
                line += _quote_html(a)
            p.append(f"<li>{line}</li>")
        p.append("</ul>")
    else:
        q = _lead_quote(r)
        if q:
            p.append(_quote_html(q))

    why = _reason(r)
    if why:
        p.append(f'<p class="why"><b>{_esc(why[0])}.</b> {_esc(why[1])}</p>')
    if r.get("claim_aspects_unmet"):
        p.append(f'<p class="gap"><b>Gap.</b> '
                 f'{_esc("; ".join(r["claim_aspects_unmet"])[:240])}</p>')
    p.append("</div>")
    card = "\n".join(p)
    if fragment:
        return card
    return _page("verdict", f"<div class='wrap'>{card}</div>")


def scorecard_html(s: dict, cards: list = None) -> str:
    """Self-contained HTML report for score_answer: overall grade, the 'worth
    your read' ranked list with links, then each claim's verdict card."""
    body = ["<div class='wrap'>",
            "<h1>Worth Your Read — scorecard</h1>",
            f"<p class='overall'><b>Grade {_esc(s.get('grade', '?'))} "
            f"({s.get('overall_confidence', 0):.2f})</b> — {_n_supported(s)}/{s.get('n_claims', 0)} "
            f"claims supported · "
            + " · ".join(f"{_esc(k)}: {v}" for k, v in (s.get("counts") or {}).items())
            + "</p>",
            "<h2 style='font-size:16px'>Worth your read</h2>"]
    wyr = s.get("worth_your_read") or []
    if not wyr:
        body.append(f"<p class='gap'>{NO_SOURCES}</p>")
    else:
        body.append("<ol class='wyr'>")
        for w in wyr:
            link = (f" — <a href='https://doi.org/{_esc(w['doi'])}' target='_blank' "
                    f"rel='noopener'>{_esc(w['doi'])} ↗</a>") if w.get("doi") else ""
            body.append(f"<li><b>{_esc(w.get('title', ''))}</b>{link}"
                        f"<br><span class='gap'>{_esc(w.get('reason', ''))}</span></li>")
        body.append("</ol>")
    for c in (cards or []):
        body.append(verdict_html(c, fragment=True))
    body.append("</div>")
    return _page("scorecard", "".join(body))
