"""Grounded answers about one paper: "ask this paper" and the auto-summary.

The model answers as a list of short, atomic claims, each with the verbatim
passages it rests on. Then the same quote guard the judge uses checks every
passage against the paper's text: a claim with no verified passage is shown as
UNGROUNDED and never counts toward confidence. With check=True each grounded
claim is also put through the full judge, which catches a real quote stretched
into a claim it doesn't support (overstatement).
"""
import html
import json
import re

from . import config
from . import focus as _focus
from . import judge as _judge
from . import score as _score

_SYSTEM = """You answer a QUESTION about one scientific paper using ONLY the \
PAPER TEXT provided. Never use outside knowledge.

Answer as a list of short, ATOMIC claims — one fact per claim, in plain \
language, in the order a reader needs them. For EACH claim give the passage(s) \
from the paper that establish it, copied VERBATIM, word for word, including \
numbers. Prefer RESULTS and CONCLUSION passages over the abstract. Keep \
numbers, units, populations and caveats exactly as the paper states them; do \
not generalize beyond the passage. If the paper does not address part of the \
question, say so in "not_addressed" instead of guessing.

Return ONLY a JSON object: {"claims": [{"claim": "...", "quotes": ["..."]}], \
"not_addressed": ["..."]}"""

SUMMARY_QUESTION = (
    "Summarize this paper for a researcher: the question it asks, the study "
    "design and population or system, the main results with their numbers, and "
    "the limitations the authors state.")


def _complete(system: str, user: str) -> str:
    """One completion on the configured backend: local Ollama, else the
    Anthropic API / Bedrock."""
    if config.USE_OLLAMA:
        return _judge._ollama_chat(system, user, num_predict=1500)
    client, model = _judge._client_and_model()
    msg = client.messages.create(
        model=model, max_tokens=1500, system=system, temperature=0,
        messages=[{"role": "user", "content": user}])
    return "".join(getattr(b, "text", "") for b in msg.content
                   if getattr(b, "type", None) == "text")


def _parse(raw: str) -> dict:
    raw = (raw or "").strip()
    raw = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", raw).strip()
    s, e = raw.find("{"), raw.rfind("}")
    try:
        return json.loads(raw[s:e + 1]) if s != -1 and e != -1 else {}
    except Exception:
        return {}


def ask(question: str, fetched: dict, check: bool = False) -> dict:
    """Answer `question` from `fetched` (a fetch_text / library.load dict).

    Returns {question, claims: [{claim, status, quotes: [{text, verified}],
    verdict?, confidence?}], not_addressed, confidence, band, grounded,
    ungrounded, source}."""
    out = {"question": question, "claims": [], "not_addressed": [],
           "confidence": 0.0, "band": "Low", "grounded": 0, "ungrounded": 0,
           "source": fetched.get("source"), "abstract_only": not fetched.get("has_fulltext")}
    text = fetched.get("full_text") or ""
    if not text:
        out["not_addressed"] = ["No paper text could be read."]
        return out
    budget = config.OLLAMA_FOCUS_CHARS if config.USE_OLLAMA else 0
    shown = _focus.focus_text(question, text, budget)
    data = _parse(_complete(_SYSTEM, f"QUESTION:\n{question}\n\nPAPER TEXT:\n{shown}"))

    haystack = _judge._norm(text)
    for c in data.get("claims") or []:
        if not isinstance(c, dict) or not (c.get("claim") or "").strip():
            continue
        quotes = [{"text": q, "verified": _judge._quote_verified(q, haystack)}
                  for q in (c.get("quotes") or []) if isinstance(q, str) and q.strip()]
        grounded = any(q["verified"] for q in quotes)
        out["claims"].append({"claim": c["claim"].strip(),
                              "status": "GROUNDED" if grounded else "UNGROUNDED",
                              "quotes": quotes})
    out["not_addressed"] = [s for s in data.get("not_addressed") or []
                            if isinstance(s, str) and s.strip()]

    if check:
        for c in out["claims"]:
            if c["status"] == "GROUNDED":
                r = _judge.judge_claim(c["claim"], fetched)
                c["verdict"], c["confidence"] = r["verdict"], r["confidence"]
                c["verdict_note"] = r.get("distortion_note") or ""

    _score_answer(out, check)
    return out


def _score_answer(out: dict, checked: bool) -> None:
    claims = out["claims"]
    out["grounded"] = sum(1 for c in claims if c["status"] == "GROUNDED")
    out["ungrounded"] = len(claims) - out["grounded"]
    if not claims:
        return
    if checked:
        # Grounded claims count by their verdict; ungrounded ones count as 0.
        conf = sum(_score._VERDICT_WEIGHT.get(c.get("verdict"), 0.0) * (c.get("confidence") or 0)
                   for c in claims) / len(claims)
    else:
        # Unchecked: we only know the passages are real, not that each claim
        # follows from them, so cap below "High".
        conf = min(0.7, out["grounded"] / len(claims))
    if out["abstract_only"]:
        conf = min(conf, 0.5)
    out["confidence"] = round(conf, 2)
    out["band"] = _judge.band(out["confidence"])


def summarize(fetched: dict, check: bool = False) -> dict:
    return ask(SUMMARY_QUESTION, fetched, check=check)


def note_html(result: dict, title: str = "") -> str:
    """A Zotero-note-ready HTML rendering: grounded claims with their quotes
    and pages; ungrounded claims are listed separately, clearly marked."""
    esc = html.escape
    parts = [f"<h1>{esc(title or 'Worth Your Read summary')}</h1>",
             f"<p><b>Confidence:</b> {result.get('band')} ({result.get('confidence'):.2f}) · "
             f"{result.get('grounded')} grounded, {result.get('ungrounded')} ungrounded"
             + (" · abstract only" if result.get("abstract_only") else "") + "</p>"]
    grounded = [c for c in result["claims"] if c["status"] == "GROUNDED"]
    ungrounded = [c for c in result["claims"] if c["status"] != "GROUNDED"]
    if grounded:
        parts.append("<ul>")
        for c in grounded:
            v = f" <i>[{esc(c['verdict'])}]</i>" if c.get("verdict") else ""
            parts.append(f"<li>{esc(c['claim'])}{v}")
            for q in c["quotes"]:
                if q["verified"]:
                    pg = f" (p. {q['page']})" if q.get("page") else ""
                    parts.append(f"<blockquote>“{esc(q['text'])}”{pg}</blockquote>")
            parts.append("</li>")
        parts.append("</ul>")
    if ungrounded:
        parts.append("<p><b>Not verified against the text</b> (no passage matched):</p><ul>")
        parts.extend(f"<li>{esc(c['claim'])}</li>" for c in ungrounded)
        parts.append("</ul>")
    if result.get("not_addressed"):
        parts.append("<p><b>Not addressed by the paper:</b> "
                     + esc("; ".join(result["not_addressed"])) + "</p>")
    return "\n".join(parts)
