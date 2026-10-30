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

# Contact email for the Crossref / NCBI "polite pools". Optional — we only send
# it when the user explicitly sets it, never a personal address by default.
CONTACT_EMAIL = os.environ.get("WYR_CONTACT_EMAIL", "").strip()

# Semantic Scholar API key (optional). Keyless works but is heavily rate-limited;
# a key makes the S2 discovery provider and PDF/abstract fallback reliable.
S2_API_KEY = os.environ.get("WYR_S2_API_KEY", os.environ.get("S2_API_KEY", "")).strip()

# User-Agent all outbound requests carry (good web citizenship + polite pool).
_ua = "WorthYourRead/0.1 (https://github.com/; mailto:{})"
USER_AGENT = _ua.format(CONTACT_EMAIL) if CONTACT_EMAIL else "WorthYourRead/0.1"

# Network timeout (seconds) for every outbound HTTP call.
HTTP_TIMEOUT = float(os.environ.get("WYR_HTTP_TIMEOUT", "20"))

# Hard cap on how much full text we hand the judge (characters). We truncate
# rather than chunk/embed — fine for an MVP.
MAX_TEXT_CHARS = int(os.environ.get("WYR_MAX_TEXT_CHARS", "48000"))
