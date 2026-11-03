"""Central configuration, read from the environment with sane defaults."""
import os

# Model used by the judge. Overridable so you can flip to a stronger model for
# hard cases without touching code.
MODEL = os.environ.get("WYR_MODEL", "claude-sonnet-5-5")

# --- Auth backend -----------------------------------------------------------
# Two ways to reach Claude:
#   1) a plain ANTHROPIC_API_KEY (default), or
#   2) AWS Bedrock, when WYR_USE_BEDROCK is truthy (we also honor Claude Code's
#      CLAUDE_CODE_USE_BEDROCK). Bedrock uses the standard AWS credential chain
#      (env vars, ~/.aws, instance role); set WYR_AWS_PROFILE to pick a profile.
def _truthy(v: str) -> bool:
    return str(v).strip().lower() not in ("", "0", "false", "no")

USE_BEDROCK = _truthy(os.environ.get("WYR_USE_BEDROCK",
                                     os.environ.get("CLAUDE_CODE_USE_BEDROCK", "")))
AWS_REGION = (os.environ.get("AWS_REGION")
              or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1")
AWS_PROFILE = os.environ.get("WYR_AWS_PROFILE", "").strip()

# Bedrock uses inference-profile model ids, which differ from the public API id
# above (e.g. "us.anthropic.claude-sonnet-4-5-20250929-v1:0"). This MUST be set
# to one your account can invoke; find yours with:
#   aws bedrock list-foundation-models --by-provider anthropic
BEDROCK_MODEL = os.environ.get("WYR_BEDROCK_MODEL", "").strip()

# --- Local judge via Ollama (optional) -------------------------------------
# When WYR_USE_OLLAMA is truthy, the judge runs on a LOCAL Ollama model instead
# of Claude. The Python quote-verification guard (judge.finalize) is identical
# whichever backend produced the raw judgement, so a small local model stays
# safe against fabricated support. NOTE: gemma "thinking" models MUST run with
# think=false (judge._ollama_chat sets it) or they spend the whole token budget
# on hidden reasoning and return empty content.
USE_OLLAMA = _truthy(os.environ.get("WYR_USE_OLLAMA", ""))
OLLAMA_URL = os.environ.get("WYR_OLLAMA_URL", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("WYR_OLLAMA_MODEL", "gemma4:e4b")
OLLAMA_NUM_CTX = int(os.environ.get("WYR_OLLAMA_NUM_CTX", "20000"))
OLLAMA_NUM_PREDICT = int(os.environ.get("WYR_OLLAMA_NUM_PREDICT", "1500"))
OLLAMA_TIMEOUT = float(os.environ.get("WYR_OLLAMA_TIMEOUT", "600"))
# Characters of paper text the LOCAL judge reads: the abstract plus the claim's
# best BM25-matched passages (see focus.py). Smaller is faster; 0 = read it all.
# Quotes are still verified against the full text either way.
OLLAMA_FOCUS_CHARS = int(os.environ.get("WYR_OLLAMA_FOCUS_CHARS", "12000"))

# Contact email for the Crossref / NCBI "polite pools". Optional — we only send
# it when the user explicitly sets it, never a personal address by default.
CONTACT_EMAIL = os.environ.get("WYR_CONTACT_EMAIL", "").strip()

# OpenAlex API key (optional, free at openalex.org). Without one OpenAlex allows
# a small daily request budget; research mode's citation snowball uses many.
OPENALEX_API_KEY = os.environ.get("WYR_OPENALEX_API_KEY", "").strip()
# Research stage-1 filter by semantic closeness (TF-IDF + LSA, needs scikit-learn)
# instead of word overlap. Off by default: on the metformin benchmark it found
# more direct hits (40 vs 29) but didn't cut drift or model calls.
SEMANTIC_STAGE1 = os.environ.get("WYR_SEMANTIC_STAGE1", "") == "1"

# Semantic Scholar API key (optional). Keyless works but is heavily rate-limited;
# a key makes the S2 discovery provider and PDF/abstract fallback reliable.
S2_API_KEY = os.environ.get("WYR_S2_API_KEY", os.environ.get("S2_API_KEY", "")).strip()

# CORE (core.ac.uk) API key (optional, free for academics at core.ac.uk/services/api).
# CORE aggregates green-OA institutional-repository copies and serves their
# extracted fullText via an authenticated JSON API. Its anonymous download links
# are bot-blocked, so the CORE full-text tier is INACTIVE without this key.
CORE_API_KEY = os.environ.get("WYR_CORE_API_KEY", "").strip()

# Publisher open-access full-text APIs (optional, free keys). These are the
# sanctioned machine path to each publisher's OA content — not bot-blocked like
# the public web pages. Each tier is INACTIVE without its key.
#   Springer Nature OA API  -> JATS full text for Springer/Nature/BMC OA
#                              (free key: dev.springernature.com)
#   Elsevier Article API    -> full text for Elsevier OA articles; subscription
#                              content too IF an institutional token is set
#                              (free key: dev.elsevier.com)
SPRINGER_API_KEY = os.environ.get("WYR_SPRINGER_API_KEY", "").strip()
ELSEVIER_API_KEY = os.environ.get("WYR_ELSEVIER_API_KEY", "").strip()
ELSEVIER_INSTTOKEN = os.environ.get("WYR_ELSEVIER_INSTTOKEN", "").strip()

# User-Agent all outbound requests carry (good web citizenship + polite pool).
_ua = "WorthYourRead/0.1 (https://github.com/; mailto:{})"
USER_AGENT = _ua.format(CONTACT_EMAIL) if CONTACT_EMAIL else "WorthYourRead/0.1"

# Where the sidecar keeps its on-disk cache and auth token.
DATA_DIR = os.path.expanduser(os.environ.get(
    "WYR_DATA_DIR", "~/Library/Application Support/WorthYourRead"
    if os.uname().sysname == "Darwin" else "~/.worth_your_read"))

# Network timeout (seconds) for every outbound HTTP call.
HTTP_TIMEOUT = float(os.environ.get("WYR_HTTP_TIMEOUT", "20"))

# Hard cap on how much full text we hand the judge (characters). We truncate
# rather than chunk/embed — fine for an MVP.
MAX_TEXT_CHARS = int(os.environ.get("WYR_MAX_TEXT_CHARS", "48000"))
