# ============================================================
#   core/ai_provider.py
#   Smallest suitable AI provider layer for the BOSS SMC bot.
#
#   A single OpenAI-compatible chat-completions client (works with OpenAI,
#   Groq, Together, a local vLLM/Ollama "/v1" endpoint, ...). Uses only the
#   Python standard library (urllib) — no new dependency, no framework.
#
#   Responsibilities:
#     1. Bounded strict JSON parsing (never eval/repair; reject NaN/inf,
#        control chars, duplicate keys, oversized or over-deep payloads).
#     2. A hand-reviewed STRICT entry-decision contract + a local validator
#        that runs REGARDLESS of provider enforcement. Anything that fails
#        local validation is an INVALID reply → the caller fails closed.
#     3. A bounded HTTP client: fixed owner-configured origin, hard timeout,
#        no redirects, no proxy auto-discovery, bounded response size.
#
#   This module NEVER logs secrets, API keys, or raw provider/exception
#   bodies. Failures are surfaced only as short internal codes.
# ============================================================

import json
import math
import urllib.request
import urllib.error

# ── Failure signals (short internal codes only — never raw bodies) ──

class AIUnavailable(Exception):
    """Provider could not be reached / answered (network, timeout, 429/5xx,
    auth, missing key). Availability-style: the caller may retry/circuit."""
    def __init__(self, code: str = "provider_unavailable"):
        self.code = code
        super().__init__("AI provider unavailable; entry approval withheld")


class AIInvalidResponse(Exception):
    """Provider answered but the content is not exactly the reviewed
    contract (bad JSON, schema mismatch, oversized, wrong envelope)."""
    def __init__(self, code: str = "invalid_response"):
        self.code = code
        super().__init__("AI response rejected by strict contract validation")


# ── Bounded strict JSON ──────────────────────────────────────

def strict_json(raw, *, max_bytes: int = 16384, max_depth: int = 8,
                max_nodes: int = 512, max_string: int = 512,
                max_array: int = 16) -> dict:
    """Parse a JSON OBJECT with hard bounds. Raises AIInvalidResponse on
    anything unexpected. Never evaluates, repairs, or scans for a convenient
    sub-object."""
    try:
        if not isinstance(raw, (str, bytes)):
            raise ValueError("not text")
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        if len(text.encode("utf-8")) > max_bytes:
            raise ValueError("too large")

        def _unique(pairs):
            seen = {}
            for k, v in pairs:
                if k in seen:
                    raise ValueError("duplicate key")
                seen[k] = v
            return seen

        def _no_const(_):
            raise ValueError("NaN/Infinity constant")

        result = json.loads(text.strip(), object_pairs_hook=_unique,
                            parse_constant=_no_const)
        if not isinstance(result, dict):
            raise ValueError("not an object")

        nodes = 0

        def _walk(value, depth=0):
            nonlocal nodes
            nodes += 1
            if depth > max_depth or nodes > max_nodes:
                raise ValueError("too deep/large")
            if isinstance(value, dict):
                if len(value) > 32:
                    raise ValueError("too many keys")
                for key, child in value.items():
                    if not isinstance(key, str) or len(key) > 64:
                        raise ValueError("bad key")
                    _walk(child, depth + 1)
            elif isinstance(value, list):
                if len(value) > max_array:
                    raise ValueError("too many items")
                for child in value:
                    _walk(child, depth + 1)
            elif isinstance(value, str):
                if len(value) > max_string:
                    raise ValueError("string too long")
                if any(ord(c) < 32 and c not in "\n\t\r" for c in value):
                    raise ValueError("control char")
            elif isinstance(value, bool):
                pass  # bool is handled explicitly by the contract validator
            elif isinstance(value, float) and not math.isfinite(value):
                raise ValueError("non-finite number")

        _walk(result)
        return result
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise AIInvalidResponse("invalid_json") from None


# ── Strict entry-decision contract ───────────────────────────

# Enum of the AI action. APPROVE only means "allow the candidate to continue
# to the existing deterministic risk checks" — it is NOT an order.
AI_ACTIONS = ("APPROVE", "REJECT", "WAIT")

# Optional bounded risk flags the model may raise (data, never instructions).
AI_RISK_FLAGS = (
    "HIGH_SPREAD", "NEWS_WINDOW", "LOW_LIQUIDITY", "HIGH_VOLATILITY",
    "COUNTER_TREND", "THIN_STOP", "WIDE_STOP", "OTHER",
)

# OpenAI/Groq strict json_schema form (all properties required, no extras).
ENTRY_SCHEMA = {
    "type": "object",
    "properties": {
        "action":     {"type": "string", "enum": list(AI_ACTIONS)},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reason_code": {"type": "string", "minLength": 1, "maxLength": 32},
        "reason":     {"type": "string", "minLength": 1, "maxLength": 200},
        "risk_flags": {
            "type": "array",
            "items": {"type": "string", "enum": list(AI_RISK_FLAGS)},
            "maxItems": 6,
        },
    },
    "required": ["action", "confidence", "reason_code", "reason", "risk_flags"],
    "additionalProperties": False,
}


def _is_number(value):
    # A JSON number only — bool (an int subclass) must NOT satisfy a number.
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_entry_decision(obj: dict) -> dict:
    """Locally validate a decoded reply against the strict contract.
    Returns a normalized dict, or raises AIInvalidResponse. This is the single
    source of truth and runs even when the provider enforces the schema."""
    if not isinstance(obj, dict):
        raise AIInvalidResponse("not_object")
    allowed = {"action", "confidence", "reason_code", "reason", "risk_flags"}
    extra = set(obj) - allowed
    if extra:
        raise AIInvalidResponse("unknown_fields")

    action = obj.get("action")
    if action not in AI_ACTIONS:
        raise AIInvalidResponse("bad_action")

    conf = obj.get("confidence")
    if not _is_number(conf) or not math.isfinite(float(conf)):
        raise AIInvalidResponse("bad_confidence")
    conf = float(conf)
    if not (0.0 <= conf <= 1.0):
        raise AIInvalidResponse("confidence_out_of_range")

    reason_code = obj.get("reason_code")
    if not isinstance(reason_code, str) or not (1 <= len(reason_code) <= 32):
        raise AIInvalidResponse("bad_reason_code")
    if any(ord(c) < 32 or ord(c) == 127 for c in reason_code):
        raise AIInvalidResponse("bad_reason_code")

    reason = obj.get("reason")
    if not isinstance(reason, str) or not (1 <= len(reason) <= 200):
        raise AIInvalidResponse("bad_reason")
    if any(ord(c) < 32 and c not in "\n\t\r" for c in reason):
        raise AIInvalidResponse("bad_reason")

    flags = obj.get("risk_flags")
    if not isinstance(flags, list) or len(flags) > 6:
        raise AIInvalidResponse("bad_risk_flags")
    for f in flags:
        if not isinstance(f, str) or f not in AI_RISK_FLAGS:
            raise AIInvalidResponse("bad_risk_flag")

    return {
        "action": action,
        "confidence": conf,
        "reason_code": reason_code,
        "reason": reason,
        "risk_flags": list(dict.fromkeys(flags)),  # de-dupe, keep order
    }


# ── OpenAI-compatible client (stdlib urllib, bounded) ────────

_AVAIL_STATUS = {408, 425, 429, 500, 502, 503, 504}


class OpenAICompatClient:
    """Minimal OpenAI-compatible chat-completions client.

    complete(messages, schema) -> (content_str, model, provider_name).
    Raises AIUnavailable on availability problems and AIInvalidResponse on a
    malformed envelope/content. Never logs the API key or raw bodies.
    """

    def __init__(self, *, base_url, api_key, model, timeout_seconds,
                 max_output_tokens, response_format="json_object"):
        self.base_url = (base_url or "").rstrip("/")
        self._api_key = api_key or ""
        self.model = model
        self.timeout = float(timeout_seconds)
        self.max_output_tokens = int(max_output_tokens)
        self.response_format = response_format
        self.name = "openai_compat"

    @property
    def configured(self) -> bool:
        return bool(self._api_key) and bool(self.base_url)

    def _response_format(self, schema):
        if self.response_format == "json_schema":
            return {"type": "json_schema",
                    "json_schema": {"name": "boss_ai_entry_review_v1",
                                    "strict": True, "schema": schema}}
        return {"type": "json_object"}

    def complete(self, messages, schema):
        if not self.configured:
            raise AIUnavailable("missing_api_key")
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": self.max_output_tokens,
            "stream": False,
            "response_format": self._response_format(schema),
        }
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": "Bearer " + self._api_key,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read(65536)
        except urllib.error.HTTPError as e:
            code = getattr(e, "code", 0)
            if code == 429:
                raise AIUnavailable("http_429") from None
            if code in _AVAIL_STATUS:
                raise AIUnavailable("http_5xx") from None
            if code in (401, 403):
                raise AIUnavailable("http_auth") from None
            # 400 (e.g. unsupported response_format) and anything else: the
            # provider answered but we can't use it — fail closed, no retry.
            raise AIInvalidResponse("http_status_%d" % code) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            # Includes socket timeouts surfaced as URLError/TimeoutError.
            raise AIUnavailable("network_or_timeout") from None

        return self._parse_envelope(raw)

    def _parse_envelope(self, raw: bytes):
        envelope = strict_json(raw, max_bytes=16384, max_depth=10,
                               max_nodes=2048, max_string=16384, max_array=64)
        try:
            choices = envelope["choices"]
            model = envelope["model"]
            if not isinstance(choices, list) or len(choices) != 1:
                raise ValueError("choices")
            if not isinstance(model, str) or not model:
                raise ValueError("model")
            choice = choices[0]
            if choice.get("finish_reason") != "stop" or choice.get("index") != 0:
                raise ValueError("finish")
            message = choice["message"]
            if message.get("role") != "assistant" or message.get("refusal") \
               or message.get("tool_calls") or message.get("function_call"):
                raise ValueError("message")
            text = message["content"]
            if not isinstance(text, str) or not text.strip():
                raise ValueError("content")
        except (KeyError, TypeError, ValueError, AttributeError):
            raise AIInvalidResponse("invalid_envelope") from None
        return text, model, self.name
