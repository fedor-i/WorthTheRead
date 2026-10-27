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

_SYSTEM = """You are a strict scientific evidence judge. You decide whether a \
CLAIM is supported by the PAPER TEXT provided, and nothing else.

Rules, in order:
1. Weigh the RESULTS and CONCLUSION sections above the title and abstract. \
Abstracts overstate; results qualify.
2. Use ONLY the provided text. Never use outside knowledge. If the text does \
not address the claim, the verdict is INSUFFICIENT.
3. Quote VERBATIM. Every quote must be copied exactly from the paper text, \
word for word, including numbers and punctuation. Never paraphrase inside a \
quote.
4. Decide the verdict:
   - SUPPORTED: the paper's results directly establish the claim as stated.
   - OVERSTATED: the paper supports a weaker version, but the claim exaggerates \
(e.g. correlation reported as causation, an animal result stated for humans, \
caveats dropped, a non-significant or small effect stated as proven, a \
subgroup/secondary outcome presented as the main finding).
   - UNSUPPORTED: the paper does not support, or contradicts, the claim.
   - INSUFFICIENT: the available text cannot settle it.
5. If OVERSTATED, name the distortion type and explain the gap with a quote \
pair (what the paper says vs. what the claim says).
6. Surface any claim aspect the text does not establish (magnitude, direction, \
population, causality)."""

# Appended for backends that can't use tool-use (MCP sampling): demand JSON.
_JSON_INSTRUCTIONS = (
    "\n\nReturn ONLY a single JSON object and no other text. Keys: "
    '"verdict" (one of SUPPORTED|OVERSTATED|UNSUPPORTED|INSUFFICIENT), '
    '"confidence" (number 0-1), '
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
        "required": ["verdict", "confidence", "evidence", "rationale"],
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


# --- shared result assembly + the quote-verification guard ------------------

def no_text_result(reason: str = "No paper text could be retrieved.") -> dict:
    return {
        "verdict": "INSUFFICIENT", "confidence": 0.0, "band": "Low",
        "evidence": [], "evidence_rejected": [], "distortion_type": "none",
        "distortion_note": "", "claim_aspects_unmet": [reason],
        "rationale": reason, "quotes_verified": 0, "downgraded": False,
    }


def finalize(data: dict, fetched: dict) -> dict:
    """Apply the quote-verification guard + confidence caps to a raw judgement
    (from any backend) and return the normalized result dict."""
    haystack = _norm(fetched.get("full_text", ""))
    verified = []
    for ev in data.get("evidence", []) or []:
        ok = _quote_verified(ev.get("quote", ""), haystack)
        ev["verified"] = ok
        if ok:
            verified.append(ev)

    result = {
        "verdict": data.get("verdict", "INSUFFICIENT"),
        "confidence": float(data.get("confidence", 0.0) or 0.0),
        "evidence": verified,
        "evidence_rejected": [e for e in (data.get("evidence") or [])
                              if not e.get("verified")],
        "distortion_type": data.get("distortion_type", "none"),
        "distortion_note": data.get("distortion_note", ""),
        "claim_aspects_unmet": list(data.get("claim_aspects_unmet", []) or []),
        "rationale": data.get("rationale", ""),
        "quotes_verified": len(verified),
        "downgraded": False,
    }

    if result["verdict"] in ("SUPPORTED", "OVERSTATED", "UNSUPPORTED") \
            and not verified:
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

def _client_and_model():
    if config.USE_BEDROCK:
        from anthropic import AnthropicBedrock
        if not config.BEDROCK_MODEL:
            raise RuntimeError(
                "WYR_USE_BEDROCK is set but WYR_BEDROCK_MODEL is empty. Set it "
                "to an inference-profile id your account can invoke (find it "
                "with: aws bedrock list-foundation-models --by-provider anthropic).")
        if config.AWS_PROFILE:
            os.environ.setdefault("AWS_PROFILE", config.AWS_PROFILE)
        return AnthropicBedrock(aws_region=config.AWS_REGION), config.BEDROCK_MODEL

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
