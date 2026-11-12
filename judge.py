"""The judge: decide whether a claim is supported by the paper's text, then
PROGRAMMATICALLY verify every quote is a real substring of the source. A
verdict with no verified quote is forced to INSUFFICIENT — this substring
check is the core anti-hallucination guard, and it runs no matter which
backend produced the raw judgement.

Three backends, same guard:
  - client sampling  -> the host's model (the Claude in this chat). No key.
  - anthropic api    -> ANTHROPIC_API_KEY.
  - bedrock          -> AWS credentials.
The server prefers sampling; this module exposes the prompt builders it needs
plus the api/bedrock path and the shared finalize()/quote-verification.
"""
import difflib
import json
import os
import re
import sys
import time

from . import config
from . import focus as _focus

VERDICTS = ("SUPPORTED", "OVERSTATED", "UNSUPPORTED", "INSUFFICIENT")
DISTORTIONS = ("none", "correlation_causation", "animal_to_human",
               "dropped_caveats", "effect_size_spin", "cherry_picking", "other")

# Human-readable labels for the distortion types (used in the verdict cards).
DISTORTION_LABELS = {
    "none": "none",
    "correlation_causation": "correlation→causation",
    "animal_to_human": "animal→human",
    "dropped_caveats": "dropped caveats",
    "effect_size_spin": "effect-size spin",
    "cherry_picking": "cherry-picking",
    "other": "other",
}

ASPECT_TYPES = ("population", "intervention", "outcome", "direction",
                "magnitude", "other")
ASPECT_STATUSES = ("ENTAILED", "CONTRADICTED", "ABSENT")

_SYSTEM = """You are a strict scientific evidence judge. You decide whether a \
CLAIM is supported by the PAPER TEXT provided, and nothing else.

First DECOMPOSE the claim into its atomic, independently checkable aspects. \
Use these types where they apply:
- "population": who or what the claim is about (species, patients, setting).
- "intervention": the exposure, treatment, or variable acted on.
- "outcome": what was measured or affected.
- "direction": the sign of the effect (increase / decrease / no effect).
- "magnitude": how large the effect is — ONLY include this if the claim \
actually asserts a size. A strength word the claim puts on an outcome \
("substantial", "major", "dramatic", "leading cause", "strongly", "profound") \
IS an asserted size: give it its OWN magnitude aspect, separate from the \
outcome's existence. The outcome aspect is ENTAILED when the text shows the \
outcome occurs; the magnitude aspect is ENTAILED ONLY when the text establishes \
that asserted strength — text that merely MENTIONS the outcome or says it "can \
occur" / "may lead to" does NOT entail an asserted "substantial"/"major" size, \
so that magnitude is ABSENT.
Only create an aspect for something the CLAIM actually asserts. A claim is only \
as strong as its weakest asserted aspect.
When the claim is a CONJUNCTION or LIST (several outcomes, populations, or \
effects joined by "and"/commas), decompose EACH conjunct into its own \
aspect(s). A claim that asserts three outcomes is SUPPORTED only if the text \
establishes all three.

Rule on EACH aspect separately, IN THE CLAIM'S OWN POLARITY, using ONLY the \
provided text:
- "ENTAILED": the text establishes what the aspect asserts — quote the SINGLE \
most decisive sentence (prefer RESULTS / CONCLUSION), VERBATIM.
- "CONTRADICTED": the text establishes the OPPOSITE of what the aspect asserts \
— give a VERBATIM quote. NEVER use CONTRADICTED when the text actually AGREES \
with the claim.
- "ABSENT": the text does not address this aspect — quote must be "".
If the claim is negative ("X is NOT equivalent to Y", "no effect", "does not \
increase"), the aspect IS that negative: ENTAILED when the text shows the \
negative holds, CONTRADICTED only when the text shows the positive.

Rules, in order:
1. Weigh the RESULTS and CONCLUSION sections above the title and abstract. \
Abstracts overstate; results qualify.
2. Use ONLY the provided text. Never use outside knowledge.
3. Quote VERBATIM. Every quote must be copied exactly from the paper text, \
word for word, including numbers and punctuation. Never paraphrase inside a \
quote.
4. Judge MEANING, not wording. An aspect is ENTAILED when the text states the \
same fact in ANY words — synonyms and paraphrases of the paper's own wording \
count. Quote the paper's wording; do not require it to echo the claim's words.
5. Numbers carry error. Treat quantities that agree within rounding, unit \
conversion, or overlapping confidence intervals as ENTAILED, not CONTRADICTED; \
mark a magnitude CONTRADICTED only when the values genuinely disagree.
6. Mind negation. If the claim asserts a negative ("X is NOT equivalent to Y", \
"no effect"), it is ENTAILED when the text establishes that negative, and \
CONTRADICTED only when the text establishes the opposite (positive) claim.
7. Give an overall verdict consistent with the aspects:
   - SUPPORTED: every asserted aspect is ENTAILED.
   - OVERSTATED: the core relationship holds, but a qualifier (magnitude, \
population, causality) is unestablished or exaggerated (correlation stated as \
causation, an animal result stated for humans, caveats dropped, a small effect \
stated as proven, a subgroup presented as the main finding).
   - UNSUPPORTED: the paper contradicts the claim's core.
   - INSUFFICIENT: the core relationship itself is absent from the text.
8. If OVERSTATED, name the distortion type and explain the gap."""

# Appended for backends that can't use tool-use (MCP sampling): demand JSON.
_JSON_INSTRUCTIONS = (
    "\n\nReturn ONLY a single JSON object and no other text. Keys: "
    '"verdict" (one of SUPPORTED|OVERSTATED|UNSUPPORTED|INSUFFICIENT), '
    '"confidence" (number 0-1), '
    '"aspects" (array of objects {"type"(' + "|".join(ASPECT_TYPES) + '),'
    '"text","status"(ENTAILED|CONTRADICTED|ABSENT),"quote","note"}), '
    '"evidence" (array of objects {"quote","section","stance"(ENTAILS|CONTRADICTS|NEUTRAL),"reason"}), '
    '"distortion_type" (one of ' + "|".join(DISTORTIONS) + '), '
    '"distortion_note" (string), '
    '"claim_aspects_unmet" (array of strings), '
    '"rationale" (string).'
)

# Tool schema for the api/bedrock path (forced structured output).
_TOOL = {
    "name": "report_verdict",
    "description": "Report the evidence verdict for the claim.",
    "input_schema": {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": list(VERDICTS)},
            "confidence": {"type": "number"},
            "aspects": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "enum": list(ASPECT_TYPES)},
                        "text": {"type": "string"},
                        "status": {"type": "string", "enum": list(ASPECT_STATUSES)},
                        "quote": {"type": "string"},
                        "note": {"type": "string"},
                    },
                    "required": ["type", "text", "status"],
                },
            },
            "evidence": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "quote": {"type": "string"},
                        "section": {"type": "string"},
                        "stance": {"type": "string",
                                   "enum": ["ENTAILS", "CONTRADICTS", "NEUTRAL"]},
                        "reason": {"type": "string"},
                    },
                    "required": ["quote", "stance"],
                },
            },
            "distortion_type": {"type": "string", "enum": list(DISTORTIONS)},
            "distortion_note": {"type": "string"},
            "claim_aspects_unmet": {"type": "array", "items": {"type": "string"}},
            "rationale": {"type": "string"},
        },
        "required": ["verdict", "confidence", "aspects", "rationale"],
    },
}


_LIGATURES = {0xFB00: "ff", 0xFB01: "fi", 0xFB02: "fl", 0xFB03: "ffi",
              0xFB04: "ffl", 0xFB05: "st", 0xFB06: "st"}


def _norm(s: str) -> str:
    s = (s or "").translate(_LIGATURES)
    s = re.sub(r"[‘’‛′]", "'", s)
    s = re.sub(r"[“”″]", '"', s)
    s = re.sub(r"[‐-―−]", "-", s)
    s = re.sub(r"[   ]", " ", s)
    # Join words split by a line-break hyphen (PDF/column extraction artifact):
    # "func-\ntion" -> "function"; genuine compounds ("cell-free") are untouched.
    s = re.sub(r"(\w)-\s+(\w)", r"\1\2", s)
    return re.sub(r"\s+", " ", s).strip().lower()


_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def _norm_alnum(s: str) -> str:
    """Normalize to lowercase alphanumeric words only — drops punctuation,
    ligatures, dashes and spacing noise that HTML/PDF extraction introduces."""
    return _ALNUM_RE.sub(" ", _norm(s)).strip()


def _quote_verified(quote: str, haystack_norm: str) -> bool:
    q = _norm(quote)
    if len(q) < 12:
        return False
    if q in haystack_norm:
        return True
    if len(q) >= 60 and q[:60] in haystack_norm:
        return True
    # A long contiguous verbatim run (10+ words) still present exactly.
    words = [w for w in q.split(" ") if w]
    if len(words) >= 10:
        for i in range(0, len(words) - 10 + 1):
            if " ".join(words[i:i + 10]) in haystack_norm:
                return True
    # Fuzzy fallback: compare on alphanumeric words only, so a real excerpt that
    # differs from the source by extraction noise (ligatures, spacing), number
    # formatting, or a few synonyms/dropped words still verifies — while a
    # fabricated quote (little in-order overlap) does not.
    qa = _norm_alnum(quote).split()
    if len(qa) >= 6:
        ha = _norm_alnum(haystack_norm).split()
        if " ".join(qa) in " ".join(ha):
            return True
        sm = difflib.SequenceMatcher(None, ha, qa, autojunk=False)
        if sum(b.size for b in sm.get_matching_blocks()) / len(qa) >= 0.8:
            return True
    return False


def verbatim(quote: str, haystack_alnum: str) -> bool:
    """The quote is in the paper word for word (ignoring case, punctuation and
    spacing); pieces joined with "..." must each be. Stricter than
    _quote_verified, which also accepts a quote whose start, a ten-word run or
    80% of its words match: such a quote counts as evidence but is shown as a
    close match, not as the paper's own words."""
    pieces = [_norm_alnum(p) for p in re.split(r"\.\.\.|…", quote or "")]
    pieces = [p for p in pieces if p]
    return bool(pieces) and len(" ".join(pieces)) >= 12 and all(f" {p} " in f" {haystack_alnum} " for p in pieces)


def band(conf: float) -> str:
    return "High" if conf >= 0.75 else "Med" if conf >= 0.45 else "Low"


# --- prompt builders (shared by every backend) -----------------------------

def build_user(claim: str, fetched: dict) -> str:
    return (f"CLAIM:\n{claim}\n\n"
            f"SECTIONS AVAILABLE: {', '.join(fetched['sections_available']) or 'none'}\n"
            f"(open_access={fetched['open_access']}, source={fetched['source']})\n\n"
            f"PAPER TEXT:\n{fetched.get('full_text', '')}")


def sampling_system() -> str:
    return _SYSTEM + _JSON_INSTRUCTIONS


# --- the revise step of the self-improvement loop --------------------------

_REVISE_SYSTEM = (
    "You tighten a scientific CLAIM so that every part of it is directly "
    "supported by the cited paper(s). Using ONLY the verified findings "
    "provided, rewrite the claim to remove or correct anything the papers do "
    "not establish — an unsupported magnitude, the wrong or over-broad "
    "population, an unsupported direction, causal language the evidence doesn't "
    "justify, or any over-generalized scope. Stay specific and faithful; do not "
    "introduce new assertions and do not weaken it beyond what the evidence "
    "requires. Return ONLY the rewritten claim as one plain sentence — no "
    "preamble, no quotation marks."
)


def revise_system() -> str:
    return _REVISE_SYSTEM


def build_revise_user(claim: str, result: dict) -> str:
    res = result.get("resolved", {}) or {}
    quotes = []
    for a in result.get("aspects", []) or []:
        if a.get("quote"):
            quotes.append(f"• [{a.get('status')} {a.get('type')}] “{a['quote']}”")
    for e in result.get("evidence", []) or []:
        if e.get("quote"):
            quotes.append(f"• “{e['quote']}”")
    unmet = "; ".join(result.get("claim_aspects_unmet", []) or [])
    dist = result.get("distortion_note") or ""
    findings = (f"PAPER: {res.get('title') or result.get('reference')} — "
                f"verdict {result.get('verdict')}."
                + (f" Distortion: {dist}" if dist else "") + "\n"
                + "Verified from the text:\n"
                + ("\n".join(quotes[:6]) or "(none)")
                + f"\nNot established by the text: {unmet or '(none)'}")
    return (f"CLAIM:\n{claim}\n\nVERIFIED FINDINGS FROM THE CITED PAPER(S):\n"
            f"{findings}\n\nRewrite the claim so it is fully supported by these "
            "findings.")


def _clean_revision(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
    text = re.sub(r"\n?```$", "", text).strip()
    # Some backends (Ollama with format=json, or a chatty model) wrap the
    # sentence in a JSON object like {"rewritten_claim": "..."}; unwrap it.
    if text.startswith("{") and text.endswith("}"):
        try:
            obj = json.loads(text)
            vals = [v for v in obj.values() if isinstance(v, str) and v.strip()]
            if vals:
                text = max(vals, key=len)
        except Exception:
            pass
    return text.strip().strip('"').strip("“”").strip()


def revise_claim_api(claim: str, result: dict) -> str:
    """Propose a tightened claim via the api/bedrock backend."""
    client, model = _client_and_model()
    msg = client.messages.create(
        model=model, max_tokens=400, system=_REVISE_SYSTEM, temperature=0,
        messages=[{"role": "user", "content": build_revise_user(claim, result)}],
    )
    text = "".join(getattr(b, "text", "") for b in msg.content
                   if getattr(b, "type", None) == "text")
    return _clean_revision(text)


# --- shared result assembly + the quote-verification guard ------------------

def no_text_result(reason: str = "No paper text could be retrieved.") -> dict:
    return {
        "verdict": "INSUFFICIENT", "confidence": 0.0, "band": "Low",
        "evidence": [], "evidence_rejected": [], "aspects": [],
        "distortion_type": "none",
        "distortion_note": "", "claim_aspects_unmet": [reason],
        "rationale": reason, "quotes_verified": 0, "downgraded": False,
    }


# The verdict is DERIVED from grounded aspects, not trusted from the model —
# the anti-"confident-wrong" guard (e.g. SUPPORTED with an ABSENT magnitude).
_CORE_TYPES = {"outcome", "direction"}
_VERDICT_CEIL = {"SUPPORTED": 1.0, "OVERSTATED": 0.6,
                 "UNSUPPORTED": 0.9, "INSUFFICIENT": 0.4}


def _check_aspects(aspects: list, haystack_norm: str) -> list:
    checked, hay_alnum = [], _norm_alnum(haystack_norm)
    for a in aspects or []:
        if not isinstance(a, dict):
            continue  # the model occasionally emits a bare string in the list
        st = (a.get("status") or "").upper()
        quote = a.get("quote") or ""
        verified = _quote_verified(quote, haystack_norm) if quote else False
        if st in ("ENTAILED", "CONTRADICTED") and not verified:
            st = "ABSENT"
        checked.append({
            "type": (a.get("type") or "other").lower(),
            "text": a.get("text", ""),
            "status": st,
            "quote": quote if verified else "",
            "close_match": bool(verified) and not verbatim(quote, hay_alnum),
            "note": a.get("note", ""),
        })
    return checked


def _derive_from_aspects(checked: list):
    if not checked:
        return None
    core = [a for a in checked if a["type"] in _CORE_TYPES]
    any_core_contra = any(a["status"] == "CONTRADICTED" for a in core)
    any_contra = any(a["status"] == "CONTRADICTED" for a in checked)
    all_entailed = all(a["status"] == "ENTAILED" for a in checked)
    core_ok = bool(core) and all(a["status"] == "ENTAILED" for a in core)
    if any_core_contra:
        verdict = "UNSUPPORTED"
    elif any_contra:
        verdict = "OVERSTATED"
    elif all_entailed:
        verdict = "SUPPORTED"
    elif core_ok:
        verdict = "OVERSTATED"
    else:
        # Some aspects established but not all — e.g. a compound claim whose one
        # conjunct is supported and another is absent: the claim asserts more
        # than the text shows, so it OVERSTATES. Nothing established at all is
        # INSUFFICIENT (the text simply doesn't address the claim).
        verdict = ("OVERSTATED"
                   if any(a["status"] == "ENTAILED" for a in checked)
                   else "INSUFFICIENT")
    unmet = [f"{a['type']}{(' — ' + a['text']) if a['text'] else ''} "
             "(not established in the text)"
             for a in checked if a["status"] == "ABSENT"]
    return verdict, unmet


# A claim that asserts universal scope ("regardless of design", "in all
# patients") needs a quote that asserts the same generality. Models often rule
# such a claim SUPPORTED off a quote that says "most" — the scope backstop
# below catches that deterministically, whatever the backend.
_UNIVERSAL_CLAIM_RE = re.compile(
    r"\b(regardless of|irrespective of|independent(?:ly)? of|in all|across all|"
    r"for all|in every|every|always|universal(?:ly)?|without exception)\b", re.I)
_UNIVERSAL_TEXT_RE = re.compile(
    r"\b(regardless|irrespective|independent(?:ly)? of|all|every|each|always|"
    r"any|universal(?:ly)?|consistent(?:ly)?|without exception|across)\b", re.I)


_SCOPE_STOP = set("a an and the of or in on for to is are was were be by with "
                  "its their this that these those as at from".split())


def _scope_backstop(claim: str, aspects: list, verified: list):
    """Return a scope aspect to record when a universal claim lacks a quote
    asserting that generality, else None. Demotes a model's ENTAILED scope
    aspect whose quote doesn't state the generality."""
    m = _UNIVERSAL_CLAIM_RE.search(claim or "")
    if not m:
        # No universal wording in the claim: a model-invented scope aspect left
        # unmet (e.g. "essential" read as scope) is noise, not overstatement.
        # A CONTRADICTED one is real evidence against the claim, so it stays.
        if claim:
            aspects[:] = [a for a in aspects
                          if not (a["type"] == "scope" and a["status"] == "ABSENT")]
        return None
    for a in aspects:
        if a["type"] != "scope":
            continue
        if a["status"] in ("CONTRADICTED", "ABSENT"):
            return None          # the model already ruled the scope unmet
        if _UNIVERSAL_TEXT_RE.search(a["quote"]):
            return None          # an ENTAILED scope with a universal quote
        a["status"], a["quote"] = "ABSENT", ""
        a["note"] = (a.get("note") or "") + " (quote does not state this generality)"
        return a
    # A quote only answers the scope if it states generality ABOUT what the
    # claim generalizes over: the words right after the qualifier ("regardless
    # of study DESIGN or FIELD"), or the claim's own words for a bare "always".
    words = lambda t: set(_norm_alnum(t).split()) - _SCOPE_STOP
    after = words(" ".join(claim[m.end():].split()[:5]))
    target, need = (after, 1) if after else (words(claim), 2)
    quotes = [a["quote"] for a in aspects if a["quote"]] + \
              [e.get("quote", "") for e in verified]
    if any(_UNIVERSAL_TEXT_RE.search(q) and len(words(q) & target) >= need
           for q in quotes):
        return None
    a = {"type": "scope", "text": m.group(0), "status": "ABSENT", "quote": "",
         "note": "the claim asserts universal scope; no verified quote states it"}
    aspects.append(a)
    return a


def finalize(data: dict, fetched: dict, claim: str = "") -> dict:
    """Apply the quote-verification guard + aspect-derived verdict + confidence
    caps to a raw judgement (from any backend) and return the normalized dict."""
    haystack = _norm(fetched.get("full_text", ""))
    verified, hay_alnum = [], _norm_alnum(haystack)
    for ev in data.get("evidence", []) or []:
        ok = _quote_verified(ev.get("quote", ""), haystack)
        ev["verified"] = ok
        if ok:
            ev["close_match"] = not verbatim(ev.get("quote", ""), hay_alnum)
            verified.append(ev)

    aspects = _check_aspects(data.get("aspects"), haystack)
    scope_gap = _scope_backstop(claim, aspects, verified)
    aspect_quotes = sum(1 for a in aspects if a["quote"])

    result = {
        "verdict": data.get("verdict", "INSUFFICIENT"),
        "confidence": float(data.get("confidence", 0.0) or 0.0),
        "evidence": verified,
        "evidence_rejected": [e for e in (data.get("evidence") or [])
                              if not e.get("verified")],
        "aspects": aspects,
        "distortion_type": data.get("distortion_type", "none"),
        "distortion_note": data.get("distortion_note", ""),
        "claim_aspects_unmet": list(data.get("claim_aspects_unmet", []) or []),
        "rationale": data.get("rationale", ""),
        "quotes_verified": len(verified) + aspect_quotes,
        "downgraded": False,
    }

    derived = _derive_from_aspects(aspects)
    if derived:
        verdict, unmet = derived
        if verdict != result["verdict"]:
            result["verdict_model"] = data.get("verdict")
        result["verdict"] = verdict
        for u in unmet:
            if u not in result["claim_aspects_unmet"]:
                result["claim_aspects_unmet"].append(u)
        result["confidence"] = min(result["confidence"],
                                   _VERDICT_CEIL.get(verdict, 1.0))

    # Without aspects to derive from (a terse model), still apply the scope
    # backstop: a universal claim backed only by non-universal quotes is
    # OVERSTATED, not SUPPORTED.
    if scope_gap and result["verdict"] == "SUPPORTED":
        result.setdefault("verdict_model", data.get("verdict"))
        result["verdict"] = "OVERSTATED"
        result["confidence"] = min(result["confidence"], _VERDICT_CEIL["OVERSTATED"])
    if scope_gap:
        note = (f"The claim says it holds \"{scope_gap['text']}\", but no verified "
                "quote states that generality.")
        if result["distortion_type"] in ("none", "", None):
            result["distortion_type"] = "other"
        if note not in result["distortion_note"]:
            result["distortion_note"] = (result["distortion_note"] + " " + note).strip()

    if result["verdict"] in ("SUPPORTED", "OVERSTATED", "UNSUPPORTED") \
            and not verified and not aspect_quotes:
        result["verdict"] = "INSUFFICIENT"
        result["downgraded"] = True
        result["confidence"] = min(result["confidence"], 0.3)
        result["claim_aspects_unmet"].append(
            "No quoted evidence could be verified against the source text.")

    # Cap confidence when we only saw an abstract. Key off has_fulltext (real
    # body text, however its sections are named) rather than a literal RESULTS
    # bucket, so full-text reads under non-standard headings aren't pinned at
    # 0.5. Fall back to has_results for callers that predate has_fulltext.
    has_ft = fetched.get("has_fulltext")
    if has_ft is None:
        has_ft = fetched.get("has_results", False)
    if not has_ft:
        result["confidence"] = min(result["confidence"], 0.5)

    result["band"] = band(result["confidence"])
    return result


def parse_json_verdict(raw_text: str, fetched: dict, claim: str = "") -> dict:
    """Backend-agnostic: parse a JSON completion (e.g. from sampling), then
    finalize with the quote guard."""
    if not fetched.get("full_text"):
        return no_text_result()
    raw = (raw_text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw).strip()
    s, e = raw.find("{"), raw.rfind("}")
    if s != -1 and e != -1:
        raw = raw[s:e + 1]
    try:
        data = json.loads(raw)
    except Exception as ex:
        r = no_text_result(f"Could not parse the judge's output as JSON: {ex}")
        return r
    return finalize(data, fetched, claim)


# --- api / bedrock backend (fallback when sampling is unavailable) ----------

class _BedrockClient:
    """Minimal Bedrock transport over boto3, exposing the one method the judge
    uses (`messages.create`). We call bedrock-runtime InvokeModel directly and
    set `anthropic_version: bedrock-2023-05-31` ourselves, rather than go through
    anthropic's AnthropicBedrock — which (as of SDK 2.3.0) sends a dated API
    version Bedrock rejects with "Invalid API version". boto3 picks up either a
    Bedrock API key (AWS_BEARER_TOKEN_BEDROCK) or the standard credential chain.
    """

    def __init__(self, region):
        import boto3
        self._rt = boto3.client("bedrock-runtime", region_name=region)

    class _Messages:
        def __init__(self, rt):
            self._rt = rt

        def create(self, model, max_tokens, messages, system=None,
                   tools=None, tool_choice=None, temperature=None, **_):
            from types import SimpleNamespace
            body = {"anthropic_version": "bedrock-2023-05-31",
                    "max_tokens": max_tokens, "messages": messages}
            if system is not None:
                body["system"] = system
            if temperature is not None:
                body["temperature"] = temperature
            if tools is not None:
                body["tools"] = tools
            if tool_choice is not None:
                body["tool_choice"] = tool_choice
            resp = self._rt.invoke_model(modelId=model, body=json.dumps(body))
            data = json.loads(resp["body"].read())
            content = [SimpleNamespace(type=b.get("type"), input=b.get("input"),
                                       text=b.get("text"), name=b.get("name"))
                       for b in data.get("content", [])]
            return SimpleNamespace(content=content)

    @property
    def messages(self):
        return _BedrockClient._Messages(self._rt)


def _client_and_model():
    if config.USE_BEDROCK:
        if not config.BEDROCK_MODEL:
            raise RuntimeError(
                "WYR_USE_BEDROCK is set but WYR_BEDROCK_MODEL is empty. Set it "
                "to an inference-profile id your account can invoke (find it "
                "with: aws bedrock list-foundation-models --by-provider anthropic).")
        if config.AWS_PROFILE:
            os.environ.setdefault("AWS_PROFILE", config.AWS_PROFILE)
        return _BedrockClient(config.AWS_REGION), config.BEDROCK_MODEL

    from anthropic import Anthropic
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("No API/Bedrock credentials configured.")
    return Anthropic(), config.MODEL


# --- local backend: Ollama (same prompt, same guard) -----------------------

def _ollama_chat(system: str, user: str, json_mode: bool = True,
                 num_predict: int = None) -> str:
    """Call a local Ollama model's /api/chat and return the message content.

    `think: false` is the default for gemma "thinking" models: with thinking
    on and no extra budget they spend all of num_predict on hidden reasoning
    and return empty content. WYR_OLLAMA_THINK turns it on (for every call,
    or only the steps it lists) and adds OLLAMA_THINK_TOKENS to the budget. `format: "json"` asks for a bare JSON object; parse_json_verdict is
    tolerant of stray prose either way. num_ctx must be large enough to hold the
    paper text (Ollama's default 4096 would truncate it)."""
    import urllib.request
    step = _step()
    think = config.OLLAMA_THINK or step in config.OLLAMA_THINK_STEPS
    body = {
        "model": config.OLLAMA_MODEL,
        "stream": False,
        "think": think,
        "options": {"temperature": 0, "seed": 0,     # the same input gives the same answer
                    "num_ctx": config.OLLAMA_NUM_CTX,
                    "num_predict": (num_predict or config.OLLAMA_NUM_PREDICT)
                                   + (config.OLLAMA_THINK_TOKENS if think else 0)},
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
    }
    if json_mode:
        body["format"] = "json"
    req = urllib.request.Request(
        config.OLLAMA_URL + "/api/chat",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    t = time.time()
    with urllib.request.urlopen(req, timeout=config.OLLAMA_TIMEOUT) as r:
        reply = json.loads(r.read())
    if config.CALL_LOG:
        _log_call(reply, time.time() - t, step)
    return reply.get("message", {}).get("content", "") or ""


def _step() -> str:
    """The step that is calling the model: the first caller outside the call helpers."""
    f = sys._getframe(1)
    while f:
        if f.f_code.co_name not in ("_step", "_ollama_chat", "_complete"):
            return f.f_code.co_name
        f = f.f_back
    return "?"


def _log_call(reply: dict, seconds: float, step: str) -> None:
    """One line in config.CALL_LOG: which step called the model (the first
    caller outside the call helpers), how long it took, the tokens read and
    written, and the reply. Never lets a logging failure break the call."""
    try:
        msg = reply.get("message") or {}
        row = {"step": step, "seconds": round(seconds, 2),
               "prompt_tokens": reply.get("prompt_eval_count"),
               "prompt_s": round((reply.get("prompt_eval_duration") or 0) / 1e9, 2),
               "out_tokens": reply.get("eval_count"),
               "out_s": round((reply.get("eval_duration") or 0) / 1e9, 2),
               "load_s": round((reply.get("load_duration") or 0) / 1e9, 2),
               "out_chars": len(msg.get("content") or ""), "reply": msg.get("content") or ""}
        if msg.get("thinking"):
            row["thinking"] = msg["thinking"]
        with open(config.CALL_LOG, "a") as fh:
            fh.write(json.dumps(row) + "\n")
    except Exception:
        pass


def judge_claim_ollama(claim: str, fetched: dict) -> dict:
    """Judge via a LOCAL Ollama model, then apply the identical Python guard
    (quote verification + aspect-derived verdict + confidence caps). The model
    only proposes; finalize() is what makes the verdict trustworthy, so a weak
    local model cannot fabricate support past the substring check."""
    if not fetched.get("full_text"):
        return no_text_result()
    # Read only the abstract + the claim's best passages (much faster on a
    # local model); finalize still verifies quotes against the FULL text.
    focused = dict(fetched, full_text=_focus.focus_text(
        claim, fetched["full_text"], config.OLLAMA_FOCUS_CHARS))
    raw = _ollama_chat(sampling_system(), build_user(claim, focused))
    return parse_json_verdict(raw, fetched, claim)


def revise_claim_ollama(claim: str, result: dict) -> str:
    """Propose a tightened claim via the LOCAL Ollama model (plain text, not
    JSON mode, so the model returns the sentence itself)."""
    return _clean_revision(_ollama_chat(_REVISE_SYSTEM,
                                        build_revise_user(claim, result),
                                        json_mode=False, num_predict=300))


def judge_claim(claim: str, fetched: dict) -> dict:
    """Direct judgement. Routes to the local Ollama backend when WYR_USE_OLLAMA
    is set; otherwise api/bedrock via forced tool-use."""
    if config.USE_OLLAMA:
        return judge_claim_ollama(claim, fetched)
    if not fetched.get("full_text"):
        return no_text_result()
    client, model = _client_and_model()
    msg = client.messages.create(
        model=model, max_tokens=1500, system=_SYSTEM, temperature=0,
        tools=[_TOOL], tool_choice={"type": "tool", "name": "report_verdict"},
        messages=[{"role": "user", "content": build_user(claim, fetched)}],
    )
    data = None
    for block in msg.content:
        if getattr(block, "type", None) == "tool_use":
            data = block.input
            break
    if not data:
        raise RuntimeError("Judge returned no structured verdict.")
    return finalize(data, fetched, claim)
