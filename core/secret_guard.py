# ============================================================
#   core/secret_guard.py
#   Make a leaked key a non-event instead of an incident.
#
#   Three layers, all dependency-free:
#     1. redact()        - masks anything that LOOKS like a secret (and every
#                          secret value the bot itself loaded) in a string.
#     2. install_log_redaction() - puts (1) on every log handler, so a stray
#                          f"{exception}" that contains the Telegram URL or an
#                          Authorization header is masked before it reaches the
#                          log file, the console or anything that ships logs.
#     3. scan_text() / tools/scan_secrets.py - the commit-time scanner: the same
#                          patterns, run on the staged diff by .githooks/pre-commit
#                          so a secret cannot be committed in the first place.
#
#   What this CANNOT do: un-leak a secret that is already public (the old
#   MT5 password / Telegram token in this repo's early commits). Those must be
#   rotated. See docs/SECURITY.md.
# ============================================================

import logging
import re

# name -> compiled pattern. Kept deliberately specific: a scanner that cries
# wolf on every hex string gets disabled, and then it protects nobody.
PATTERNS = {
    # Telegram URLs read ".../bot<id>:<token>/..." — the id is glued to the word
    # "bot", so a \b before the digits would never match the very text that
    # shows up in exception messages. Use "not preceded by a digit" instead.
    "telegram_bot_token": re.compile(r"(?<![0-9])\d{8,10}:[A-Za-z0-9_-]{30,}(?![A-Za-z0-9_-])"),
    "openai_style_key":   re.compile(r"\b(?:sk|gsk|xai)-[A-Za-z0-9_-]{16,}\b"),
    "anthropic_key":      re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}\b"),
    "google_api_key":     re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    "aws_access_key_id":  re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "github_token":       re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    "bearer_header":      re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{20,}"),
    "private_key_block":  re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
}

# `NAME = "literal"` / `NAME=literal` / os.getenv("NAME", "literal") with a
# NON-EMPTY, non-placeholder literal for a secret-looking name. This is the
# exact shape of the original leak (MT5_PASSWORD default baked into config.py).
_SECRET_NAMES = (r"(?:MT5_PASSWORD|MT5_LOGIN|TELEGRAM_TOKEN|AI_API_KEYS?|"
                 r"[A-Z0-9_]*(?:SECRET|PASSWORD|PASSWD|API_KEY|ACCESS_KEY|TOKEN)[A-Z0-9_]*)")
# NAME may be followed by a closing quote (dict keys, os.getenv("NAME", "x")).
ASSIGNMENT = re.compile(
    r"(?P<name>\b" + _SECRET_NAMES + r")\b[\"']?\s*(?:=|:|,)\s*"
    r"(?P<q>[\"']?)(?P<value>[^\s\"',)#]{4,})[\"']?")

# lower-case / Python-style: `password="x"`, `mt5.login(1, password="x")`,
# `for pw in ["x", "y"]`, `token = 'x'`. The upper-case pattern above never saw
# these (it is case-sensitive on purpose), which is how a throw-away
# `_mt5_diag.py` carrying a real password got past the hook. Here a QUOTED
# literal is required, so `password = getpass()` / `token = tokens[0]` stay quiet.
LOWER_ASSIGNMENT = re.compile(
    r"(?i)\b(?P<name>(?:[a-z0-9_]*(?:password|passwd|passphrase|secret|api_?key|"
    r"access_?key|token)[a-z0-9_]*)|pw|pwd|pass|pws|passwords?)\b[\"']?\s*"
    r"(?:=|:|\bin\b)\s*[\[(]?\s*(?P<q>[\"'])(?P<value>[^\"'\s]{4,})[\"']"
    r"(?P<more>(?:\s*,\s*[\"'][^\"'\s]*[\"'])*)")   # ["a", "b"]: every candidate

# values that are clearly not real secrets
PLACEHOLDER = re.compile(
    r"(?i)^(?:your[_-]?.*|<.*>|x{3,}|\*+|changeme|example.*|sk-key-\d+|"
    r"placeholder|none|null|true|false|\d{1,3}|\$\{?.*|%.*%|os\.getenv.*|"
    r"_?[a-z_]*env\(.*|config\..*|.*\.{3}.*)$")        # "sk-..." = elided example

MASK = "***REDACTED***"

# what may follow a bare (unquoted) value for the line to still be a
# `.env`-style assignment: nothing, a comment, or closing markdown/quote marks
_DOTENV_TAIL = re.compile(r"[\s`*\"'.,;)]*(?:#.*)?")


def _looks_real(value: str) -> bool:
    v = value.strip().strip("\"'")
    if not v or PLACEHOLDER.match(v):
        return False
    # code identifiers / attribute access / calls are references, not values
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*(\(.*\))?", v) and not re.search(r"\d", v):
        return False
    return True


def _is_real_assignment(text: str, m) -> bool:
    """Is this NAME=value match a real secret value rather than prose or code?

    * a QUOTED literal is a value (`os.getenv("X", "hunter2")`, `{"KEY": "..."}`)
    * a BARE value counts only when it is the whole rest of the line, i.e. a
      `.env` assignment — `KEY = KEYS[0] if KEYS else ""` is code, not a secret.
    """
    if not _looks_real(m.group("value")):
        return False
    if m.group("q"):
        return True
    rest = text[m.end():].split("\n", 1)[0]
    return _DOTENV_TAIL.fullmatch(rest) is not None


def scan_text(text: str, source: str = "") -> list:
    """Return [(line_no, kind, masked_excerpt)] for every probable secret.
    The excerpt is ALREADY masked - never print the raw match."""
    findings = []
    for no, line in enumerate((text or "").splitlines(), 1):
        if "secret-scan: allow" in line:
            continue
        for kind, pat in PATTERNS.items():
            if pat.search(line):
                findings.append((no, kind, redact(line.strip())[:140]))
        # EVERY assignment on the line, not just the first: in
        #   MT5_PASSWORD = os.getenv("MT5_PASSWORD", "<literal>")
        # the first match is the harmless left-hand side.
        if any(_is_real_assignment(line, m) for m in ASSIGNMENT.finditer(line)):
            findings.append((no, "secret_literal_assignment",
                             redact(line.strip())[:140]))
        elif any(_looks_real(m.group("value"))
                 for m in LOWER_ASSIGNMENT.finditer(line)):
            findings.append((no, "secret_literal_assignment",
                             redact(line.strip())[:140]))
    # de-duplicate (a line can match two patterns)
    seen, out = set(), []
    for f in findings:
        if (f[0], f[1]) not in seen:
            seen.add((f[0], f[1]))
            out.append(f)
    return out


_known_values = set()


def register_secret_values(*values):
    """Remember exact secret values (password, token, API keys) so they are
    masked even if they do not match any generic pattern."""
    for v in values:
        for item in (v if isinstance(v, (list, tuple, set)) else [v]):
            s = str(item or "").strip()
            if len(s) >= 6:
                _known_values.add(s)


def redact(text: str) -> str:
    """Mask known secret values and anything that matches a secret pattern."""
    if not text:
        return text
    out = str(text)
    for v in sorted(_known_values, key=len, reverse=True):
        if v in out:
            out = out.replace(v, MASK)
    for pat in PATTERNS.values():
        out = pat.sub(MASK, out)
    # NAME=value on one line: keep the name, hide the value
    def _assign(m):
        return m.group(0).replace(m.group("value"), MASK) \
            if _is_real_assignment(out, m) else m.group(0)
    out = ASSIGNMENT.sub(_assign, out)

    # lower-case / keyword-argument literals: password="x" -> password="***"
    def _assign_lower(m):
        if not _looks_real(m.group("value")):
            return m.group(0)
        more = re.sub(r"([\"'])[^\"'\s]*\1", lambda q: q.group(1) + MASK + q.group(1),
                      m.group("more"))
        head = m.group(0)[:len(m.group(0)) - len(m.group("more"))]
        return head.replace(m.group("value"), MASK) + more
    return LOWER_ASSIGNMENT.sub(_assign_lower, out)


class RedactingFilter(logging.Filter):
    """Masks secrets in every record that passes through a handler."""

    def filter(self, record):
        try:
            msg = record.getMessage()
        except Exception:
            return True
        clean = redact(msg)
        if clean != msg:
            record.msg = clean
            record.args = ()
        if record.exc_info and record.exc_text is None:
            # pre-render the traceback so its text is masked as well
            try:
                record.exc_text = redact(
                    logging.Formatter().formatException(record.exc_info))
            except Exception:
                pass
        elif record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


def install_log_redaction(config_module=None):
    """Attach the redacting filter to every root handler and register the
    secrets the bot loaded. Call once, right after logging.basicConfig()."""
    if config_module is not None:
        register_secret_values(
            getattr(config_module, "MT5_PASSWORD", ""),
            getattr(config_module, "TELEGRAM_TOKEN", ""),
            getattr(config_module, "AI_API_KEYS", []),
        )
    root = logging.getLogger()
    flt = RedactingFilter()
    for h in root.handlers:
        if not any(isinstance(f, RedactingFilter) for f in h.filters):
            h.addFilter(flt)
    return flt
