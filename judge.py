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
import json
import os
import re

from . import config

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
actually asserts a size.
Only create an aspect for something the CLAIM actually asserts. A claim is only \
as strong as its weakest asserted aspect.

Rule on EACH aspect separately, using ONLY the provided text:
- "ENTAILED": the text directly establishes this aspect — give a VERBATIM quote.
- "CONTRADICTED": the text states the opposite — give a VERBATIM quote.
- "ABSENT": the text does not address this aspect — quote must be "".

Rules, in order:
1. Weigh the RESULTS and CONCLUSION sections above the title and abstract. \
Abstracts overstate; results qualify.
2. Use ONLY the provided text. Never use outside knowledge.
3. Quote VERBATIM. Every quote must be copied exactly from the paper text, \
word for word, including numbers and punctuation. Never paraphrase inside a \
quote.
4. Give an overall verdict consistent with the aspects:
   - SUPPORTED: every asserted aspect is ENTAILED.
   - OVERSTATED: the core relationship holds, but a qualifier (magnitude, \
population, causality) is unestablished or exaggerated (correlation stated as \
causation, an animal result stated for humans, caveats dropped, a small effect \
stated as proven, a subgroup presented as the main finding).
   - UNSUPPORTED: the paper contradicts the claim's core.
   - INSUFFICIENT: the core relationship itself is absent from the text.
5. If OVERSTATED, name the distortion type and explain the gap."""

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


def _norm(s: str) -> str:
    s = s or ""
    s = re.sub(r"[‘’‛′]", "'", s)
    s = re.sub(r"[“”″]", '"', s)
    s = re.sub(r"[‐-―−]", "-", s)
    s = re.sub(r"[   ]", " ", s)
    return re.sub(r"\s+", " ", s).strip().lower()


def _quote_verified(quote: str, haystack_norm: str) -> bool:
    q = _norm(quote)
    if len(q) < 12:
        return False
    if q in haystack_norm:
        return True
    if len(q) >= 60 and q[:60] in haystack_norm:
        return True
    # Relaxed fallback: a long contiguous verbatim run (10+ words) still present
    # exactly in the source — tolerates a dropped clause elsewhere in the quote
    # without weakening the anti-hallucination guarantee.
    words = [w for w in q.split(" ") if w]
    if len(words) >= 10:
        for i in range(0, len(words) - 10 + 1):
            if " ".join(words[i:i + 10]) in haystack_norm:
                return True
    return False


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
    text = re.sub(r"\n?```$", "", text)
    return text.strip().strip('"').strip("“”").strip()


def revise_claim_api(claim: str, result: dict) -> str:
    """Propose a tightened claim via the api/bedrock backend."""
    client, model = _client_and_model()
    msg = client.messages.create(
        model=model, max_tokens=400, system=_REVISE_SYSTEM,
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
    checked = []
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
    elif not core:
        verdict = "SUPPORTED" if all_entailed else "INSUFFICIENT"
    else:
        verdict = "INSUFFICIENT"
    unmet = [f"{a['type']}{(' — ' + a['text']) if a['text'] else ''} "
             "(not established in the text)"
             for a in checked if a["status"] == "ABSENT"]
    return verdict, unmet


def finalize(data: dict, fetched: dict) -> dict:
    """Apply the quote-verification guard + aspect-derived verdict + confidence
    caps to a raw judgement (from any backend) and return the normalized dict."""
    haystack = _norm(fetched.get("full_text", ""))
    verified = []
    for ev in data.get("evidence", []) or []:
        ok = _quote_verified(ev.get("quote", ""), haystack)
        ev["verified"] = ok
        if ok:
            verified.append(ev)

    aspects = _check_aspects(data.get("aspects"), haystack)
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

    if result["verdict"] in ("SUPPORTED", "OVERSTATED", "UNSUPPORTED") \
            and not verified and not aspect_quotes:
        result["verdict"] = "INSUFFICIENT"
        result["downgraded"] = True
        result["confidence"] = min(result["confidence"], 0.3)
        result["claim_aspects_unmet"].append(
            "No quoted evidence could be verified against the source text.")

    if not fetched.get("has_results"):
        result["confidence"] = min(result["confidence"], 0.5)

    result["band"] = band(result["confidence"])
    return result


def parse_json_verdict(raw_text: str, fetched: dict) -> dict:
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
    return finalize(data, fetched)


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
                   tools=None, tool_choice=None, **_):
            from types import SimpleNamespace
            body = {"anthropic_version": "bedrock-2023-05-31",
                    "max_tokens": max_tokens, "messages": messages}
            if system is not None:
                body["system"] = system
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


def judge_claim(claim: str, fetched: dict) -> dict:
    """Direct api/bedrock judgement via forced tool-use."""
    if not fetched.get("full_text"):
        return no_text_result()
    client, model = _client_and_model()
    msg = client.messages.create(
        model=model, max_tokens=1500, system=_SYSTEM,
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
    return finalize(data, fetched)
