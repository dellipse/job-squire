"""PII/SPI redaction and rehydration for every AI transmission path.

Design (see docs/PLAN-ai-privacy.md):

- Identifiers (names, emails, phones, addresses, SSNs, LinkedIn URLs, work
  authorization / clearance statements) are replaced with deterministic
  placeholders like ``{{PII:EMAIL_3f2a1c8b}}`` before text is sent to any AI
  provider, and swapped back ("rehydrated") in the results.
- Substance (employers, titles, work history) is sent intact by default.
  Optional strict mode additionally pseudonymizes organization names and
  locations known to the app.
- SPI/PHI that should not reach employers at all (health information, age
  signals, marital status) is *stripped* from outbound text and surfaced to
  the user as coaching flags — it is never tokenized-and-rehydrated because
  it should be removed from the source documents entirely.

Placeholder IDs are HMAC-SHA256 digests of the value, keyed with the app
SECRET_KEY and truncated. This makes redaction deterministic across the three
processes (web, worker, MCP) with no shared counter: the same value always
maps to the same placeholder. The encrypted vault (DATA_DIR/privacy_vault.json)
is only needed to *reverse* placeholders whose values were discovered by the
pattern pass; placeholders for known values can always be recomputed.

Detection is local-only: exact matching of values the app already knows
(candidate account, SMTP settings, contacts) plus regexes for common
identifier shapes. No NER/ML dependencies — see the plan doc for why.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import logging
import os
import re
from dataclasses import dataclass, field

from flask import current_app

from .extensions import db

log = logging.getLogger(__name__)

_VAULT_FILENAME = "privacy_vault.json"
_DIGEST_LEN = 8

# Matches {{PII:KIND_digest}} leniently: 1-2 braces on each side, optional
# whitespace, case-insensitive, tolerant of -/space instead of _ — models
# occasionally mangle placeholder tokens and rehydration must not silently
# fail when they do.
PLACEHOLDER_RE = re.compile(
    r"\{{1,2}\s*PII\s*:\s*([A-Z]+)\s*[_\- ]\s*([0-9a-f]{6,16})\s*\}{1,2}",
    re.IGNORECASE,
)

SPI_REMOVED_MARKER = "[sensitive information removed]"

# Name fragments we never tokenize on their own — either generic account
# names or words too common in English to replace safely.
_NAME_STOPWORDS = {
    "admin", "user", "candidate", "test", "demo", "recruiter", "contact",
    "will", "grant", "amber", "june", "april", "may", "rose", "dawn",
    "summer", "hunter", "mark", "bill", "frank", "jack", "art", "gene",
    "chase", "dean", "drew", "earl", "grace", "hope", "lane", "miles",
    "ray", "reed", "rich", "wade", "young", "long", "little", "brown",
    "white", "green", "black", "gray", "stone", "wood", "field", "hill",
    "price", "sharp", "smart", "strong", "swift", "west", "north", "south",
    "east", "page", "law", "day", "week", "case", "cook", "baker", "carter",
}

# ---------------------------------------------------------------------------
# Pattern pass — identifier shapes found in free text (e.g. inside
# candidate_profile.md, job notes, interview debriefs).
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
# Requires separators/parens so bare 10-digit numbers (ids, salaries) don't match.
_PHONE_RE = re.compile(
    r"(?<![\d.])(?:\+?1[\s.\-]?)?\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}(?![\d.])"
)
_SSN_RE = re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")
_LINKEDIN_RE = re.compile(
    r"(?:https?://)?(?:www\.)?linkedin\.com/in/[A-Za-z0-9\-_%.]+/?", re.IGNORECASE
)
_ADDRESS_RE = re.compile(
    r"\b\d{1,6}\s+(?:[A-Z][A-Za-z]+\s+){1,3}"
    r"(?:St(?:reet)?|Ave(?:nue)?|Blvd|Boulevard|Dr(?:ive)?|Ln|Lane|Rd|Road|"
    r"Ct|Court|Way|Cir(?:cle)?|Pl(?:ace)?|Terrace|Trail|Pkwy|Parkway)\.?"
    r"(?:,?\s*(?:Apt|Apartment|Suite|Ste|Unit|#)\.?\s*\w+)?\b"
)
# Work authorization / clearance: legitimately belongs in applications
# (federal roles ask for it), so it is tokenized and rehydrated, not stripped.
_WORKAUTH_RE = re.compile(
    r"\b(?:TS/SCI|Top\s+Secret(?:/SCI)?(?:\s+clearance)?|Secret\s+clearance|"
    r"Security\s+clearance|U\.?S\.?\s+citizen(?:ship)?|Green\s+card|"
    r"Permanent\s+resident|H-?1B(?:\s+visa)?)\b",
    re.IGNORECASE,
)

_PATTERN_PASS = [
    ("EMAIL", _EMAIL_RE),
    ("SSN", _SSN_RE),
    ("LINKEDIN", _LINKEDIN_RE),
    ("PHONE", _PHONE_RE),
    ("ADDRESS", _ADDRESS_RE),
    ("WORKAUTH", _WORKAUTH_RE),
]

# ---------------------------------------------------------------------------
# SPI/PHI strip pass — content that should not reach employers at all.
# Matched sentences are removed from outbound text and reported as coaching
# flags (warn + coach; see plan doc "SPI rule").
# ---------------------------------------------------------------------------

_SPI_HEALTH_RE = re.compile(
    r"\b(?:cancer|diabet(?:es|ic)|depression|anxiety|adhd|autis(?:m|tic)|"
    r"bipolar|epilep(?:sy|tic)|disabilit(?:y|ies)|disabled|chronic\s+(?:illness|pain|fatigue)|"
    r"medical\s+(?:condition|leave)|ptsd|hiv|aids|pregnan(?:t|cy)|in\s+therapy|"
    r"medication|surgery|diagnos(?:is|ed)|wheelchair)\b",
    re.IGNORECASE,
)
_SPI_AGE_RE = re.compile(
    r"\b(?:born\s+in\s+(?:19|20)\d{2}|\d{1,2}\s+years\s+old|"
    r"date\s+of\s+birth|DOB\b)",
    re.IGNORECASE,
)
_SPI_MARITAL_RE = re.compile(
    r"\b(?:married|divorced|widowed|marital\s+status|my\s+(?:spouse|husband|wife))\b",
    re.IGNORECASE,
)

SPI_CATEGORIES = {
    "health": (
        _SPI_HEALTH_RE,
        "Health or medical information was found. It should not be shared "
        "with AI providers or employers — recommend removing it from your "
        "profile and documents.",
    ),
    "age": (
        _SPI_AGE_RE,
        "An age signal (birth date or age) was found. Modern resume practice "
        "is to omit anything that reveals age — recommend removing it.",
    ),
    "marital": (
        _SPI_MARITAL_RE,
        "Marital or family status was found. It is not relevant to "
        "applications and can invite bias — recommend removing it.",
    ),
}


@dataclass
class RedactionResult:
    """Outcome of a redact() call."""

    text: str
    mapping: dict = field(default_factory=dict)   # placeholder -> original value
    spi_flags: list = field(default_factory=list)  # [{category, snippet, guidance}]

    @property
    def replaced_count(self) -> int:
        return len(self.mapping)


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _cfg():
    from .models import AIConfig
    return db.session.get(AIConfig, 1)


def redaction_enabled() -> bool:
    cfg = _cfg()
    # Default ON: a missing row or missing column must fail closed (redact).
    return bool(getattr(cfg, "redaction_enabled", True)) if cfg else True


def strict_mode() -> bool:
    cfg = _cfg()
    return bool(getattr(cfg, "redact_strict", False)) if cfg else False


def redact_local() -> bool:
    cfg = _cfg()
    return bool(getattr(cfg, "redact_local", False)) if cfg else False


_LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "host.docker.internal", "::1"}


def is_local_provider(provider_row) -> bool:
    """True when the provider serves from this machine (no data egress)."""
    ptype = getattr(provider_row, "provider", "") or ""
    base_url = (getattr(provider_row, "base_url", "") or "").strip()
    if ptype in ("ollama", "litellm"):
        # Default base URLs for these are local; a remote base URL makes them non-local.
        if not base_url:
            return True
    if base_url:
        host = re.sub(r"^[a-z+]+://", "", base_url, flags=re.IGNORECASE)
        host = host.split("/", 1)[0].split("@")[-1].rsplit(":", 1)[0].strip("[]")
        return host.lower() in _LOCAL_HOSTS
    return False


def should_redact_for(provider_row) -> bool:
    """Per-provider decision used inside the call_with_fallback choke point."""
    if not redaction_enabled():
        return False
    if is_local_provider(provider_row) and not redact_local():
        return False
    return True


# ---------------------------------------------------------------------------
# Placeholders and the vault
# ---------------------------------------------------------------------------

def _secret() -> str:
    return current_app.config["SECRET_KEY"]


def make_placeholder(kind: str, value: str) -> str:
    digest = hmac.new(
        _secret().encode("utf-8"),
        (kind.upper() + "\x00" + value.strip().casefold()).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:_DIGEST_LEN]
    return "{{PII:%s_%s}}" % (kind.upper(), digest)


def _vault_path() -> str:
    return os.path.join(current_app.config["DATA_DIR"], _VAULT_FILENAME)


def _load_vault() -> dict:
    from .crypto import load_encrypted_json
    return load_encrypted_json(_vault_path(), _secret(), default={}) or {}


def _persist_mappings(mapping: dict) -> None:
    """Append new placeholder->value pairs to the vault (cross-process safe)."""
    if not mapping:
        return
    from .crypto import load_encrypted_json, dump_encrypted_json
    lock_path = _vault_path() + ".lock"
    try:
        with open(lock_path, "w") as lock_f:
            fcntl.flock(lock_f, fcntl.LOCK_EX)
            vault = load_encrypted_json(_vault_path(), _secret(), default={}) or {}
            new = {ph: v for ph, v in mapping.items() if ph not in vault}
            if new:
                vault.update(new)
                dump_encrypted_json(_vault_path(), _secret(), vault)
    except OSError as exc:
        log.warning("privacy: could not persist vault mappings: %s", exc)


# ---------------------------------------------------------------------------
# Known values — data the app already holds about people
# ---------------------------------------------------------------------------

def _name_variants(full_name: str):
    """A full name plus its individually-safe parts."""
    full_name = (full_name or "").strip()
    if not full_name or full_name.casefold() in _NAME_STOPWORDS:
        return
    yield full_name
    parts = full_name.split()
    if len(parts) > 1:
        for part in parts:
            p = part.strip(".,")
            if (len(p) >= 4 and p.isalpha()
                    and p.casefold() not in _NAME_STOPWORDS
                    # Pure-hex names ("Dead", "Cafe") could match inside a
                    # placeholder digest and corrupt it — never emit them.
                    and not re.fullmatch(r"[0-9a-fA-F]+", p)):
                yield p


def collect_known_values() -> list:
    """(kind, value) pairs from candidate accounts, SMTP config, and contacts.

    Longest values first so full names replace before their parts.
    """
    from .models import User, SmtpConfig, Contact

    out: list[tuple[str, str]] = []

    for user in User.query.all():
        for variant in _name_variants(user.display_name or ""):
            out.append(("NAME", variant))
        uname = (user.username or "").strip()
        if uname and uname.casefold() not in _NAME_STOPWORDS and len(uname) >= 4:
            out.append(("NAME", uname))

    smtp = db.session.get(SmtpConfig, 1)
    if smtp:
        for addr in (smtp.to_addr, smtp.from_addr, smtp.admin_email, smtp.username):
            addr = (addr or "").strip()
            if addr and "@" in addr:
                out.append(("EMAIL", addr))

    for c in Contact.query.all():
        for variant in _name_variants(c.name or ""):
            out.append(("NAME", variant))
        if (c.email or "").strip():
            out.append(("EMAIL", c.email.strip()))
        if (c.phone or "").strip():
            out.append(("PHONE", c.phone.strip()))
        if (c.linkedin_url or "").strip():
            out.append(("LINKEDIN", c.linkedin_url.strip()))

    # De-duplicate (case-insensitive), longest first.
    seen: set[tuple[str, str]] = set()
    unique = []
    for kind, value in out:
        key = (kind, value.casefold())
        if key not in seen:
            seen.add(key)
            unique.append((kind, value))
    unique.sort(key=lambda kv: len(kv[1]), reverse=True)
    return unique


def _strict_values() -> list:
    """Organizations and locations for strict mode."""
    from .models import Job, Contact, SearchConfig

    out: list[tuple[str, str]] = []
    for (company,) in db.session.query(Job.company).distinct():
        if company and len(company.strip()) >= 3:
            out.append(("ORG", company.strip()))
    for (agency,) in db.session.query(Contact.agency).distinct():
        if agency and len(agency.strip()) >= 3:
            out.append(("ORG", agency.strip()))
    locations = {loc for (loc,) in db.session.query(Job.location).distinct() if loc}
    sc = db.session.get(SearchConfig, 1)
    if sc and (sc.location or "").strip():
        locations.add(sc.location.strip())
    for loc in locations:
        loc = loc.strip()
        if len(loc) >= 3 and loc.casefold() not in ("remote", "hybrid", "unknown"):
            out.append(("LOC", loc))
    seen: set[tuple[str, str]] = set()
    unique = []
    for kind, value in out:
        key = (kind, value.casefold())
        if key not in seen:
            seen.add(key)
            unique.append((kind, value))
    unique.sort(key=lambda kv: len(kv[1]), reverse=True)
    return unique


def _phone_pattern(value: str):
    """Regex matching a known phone number under any common US formatting."""
    digits = re.sub(r"\D", "", value)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return re.compile(re.escape(value))
    a, b, c = digits[:3], digits[3:6], digits[6:]
    return re.compile(
        r"(?:\+?1[\s.\-]?)?\(?" + a + r"\)?[\s.\-]?" + b + r"[\s.\-]?" + c + r"(?!\d)"
    )


# ---------------------------------------------------------------------------
# SPI scan / strip
# ---------------------------------------------------------------------------

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n")


def scan_spi(text: str) -> list:
    """Coaching flags for SPI/PHI content: [{category, snippet, guidance}]."""
    flags = []
    if not text:
        return flags
    for category, (pattern, guidance) in SPI_CATEGORIES.items():
        for m in pattern.finditer(text):
            start = max(0, m.start() - 40)
            end = min(len(text), m.end() + 40)
            snippet = text[start:end].replace("\n", " ").strip()
            flags.append({
                "category": category,
                "match": m.group(0),
                "snippet": ("…" if start else "") + snippet + ("…" if end < len(text) else ""),
                "guidance": guidance,
            })
    return flags


def _strip_spi(text: str) -> tuple:
    """Remove sentences containing SPI matches. Returns (text, flags)."""
    flags = scan_spi(text)
    if not flags:
        return text, flags
    out_lines = []
    for line in text.split("\n"):
        sentences = re.split(r"(?<=[.!?])\s+", line)
        kept = []
        for sentence in sentences:
            if any(p.search(sentence) for p, _ in SPI_CATEGORIES.values()):
                kept.append(SPI_REMOVED_MARKER)
            else:
                kept.append(sentence)
        out_lines.append(" ".join(kept))
    return "\n".join(out_lines), flags


# ---------------------------------------------------------------------------
# redact / rehydrate
# ---------------------------------------------------------------------------

def _redact_core(text: str, known: list, strict_vals: list | None,
                 strip_spi: bool, mapping: dict, spi_flags: list) -> str:
    """One-string redaction against precollected value lists. No persistence."""
    if strip_spi:
        text, flags = _strip_spi(text)
        spi_flags.extend(flags)

    def _sub_value(kind: str, value: str, src: str) -> str:
        ph = make_placeholder(kind, value)
        if kind == "PHONE":
            pattern = _phone_pattern(value)
        else:
            # Word-boundary lookarounds so short names can't match inside
            # longer words ("Ann" must not hit "Announcement").
            pattern = re.compile(r"(?<!\w)" + re.escape(value) + r"(?!\w)",
                                 re.IGNORECASE)
        new_src, n = pattern.subn(ph, src)
        if n:
            mapping[ph] = value
        return new_src

    # 1. Pattern pass FIRST: whole emails/URLs/addresses must be tokenized as
    #    units before the known-values pass can replace a name fragment inside
    #    them (jordan.ellison@… must become one EMAIL placeholder, not two NAME
    #    placeholders glued around an @).
    for kind, pattern in _PATTERN_PASS:
        def _repl(m, _kind=kind):
            value = m.group(0)
            ph = make_placeholder(_kind, value)
            mapping[ph] = value
            return ph
        text = pattern.sub(_repl, text)

    # 2. Known values (longest first — collect_known_values guarantees order).
    for kind, value in known:
        text = _sub_value(kind, value, text)

    # 3. Strict mode: pseudonymize organizations and locations.
    if strict_vals:
        for kind, value in strict_vals:
            text = _sub_value(kind, value, text)

    return text


def redact(text: str, strict: bool | None = None, strip_spi: bool = True) -> RedactionResult:
    """Replace identifiers with placeholders; strip SPI. Returns RedactionResult.

    ``strict=None`` reads the configured strict mode; pass True/False to force.
    New mappings are persisted to the encrypted vault for later rehydration.
    """
    if not text:
        return RedactionResult(text=text or "")
    if strict is None:
        strict = strict_mode()

    mapping: dict[str, str] = {}
    spi_flags: list = []
    out = _redact_core(text, collect_known_values(),
                       _strict_values() if strict else None,
                       strip_spi, mapping, spi_flags)
    _persist_mappings(mapping)
    return RedactionResult(text=out, mapping=mapping, spi_flags=spi_flags)


def _rehydration_lookup(extra_mapping: dict | None = None) -> dict:
    """digest-keyed lookup: (KIND, digest) -> value.

    Combines the vault, freshly recomputed placeholders for current known
    values (so a lost vault write never breaks known-value rehydration), and
    any per-call mapping.
    """
    lookup: dict[tuple, str] = {}

    def _add(ph: str, value: str) -> None:
        m = PLACEHOLDER_RE.fullmatch(ph) or PLACEHOLDER_RE.match(ph)
        if m:
            lookup[(m.group(1).upper(), m.group(2).lower())] = value

    for ph, value in _load_vault().items():
        _add(ph, value)
    for kind, value in collect_known_values():
        _add(make_placeholder(kind, value), value)
    for kind, value in _strict_values():
        _add(make_placeholder(kind, value), value)
    for ph, value in (extra_mapping or {}).items():
        _add(ph, value)
    return lookup


def rehydrate(text: str, mapping: dict | None = None) -> tuple:
    """Replace placeholders with real values. Returns (text, unresolved).

    ``unresolved`` lists placeholder strings that could not be mapped back —
    callers should surface these rather than fail silently.
    """
    if not text or "pii" not in text.casefold():
        return text or "", []
    lookup = _rehydration_lookup(mapping)
    unresolved: list[str] = []

    def _repl(m):
        key = (m.group(1).upper(), m.group(2).lower())
        if key in lookup:
            return lookup[key]
        unresolved.append(m.group(0))
        return m.group(0)

    out = PLACEHOLDER_RE.sub(_repl, text)
    if unresolved:
        log.warning("privacy: %d unresolved placeholder(s) left in AI output: %s",
                    len(unresolved), unresolved[:5])
    return out, unresolved


def contains_placeholders(text: str) -> bool:
    return bool(text and PLACEHOLDER_RE.search(text))


# ---------------------------------------------------------------------------
# Structured payload helpers (MCP tool responses, export dicts)
# ---------------------------------------------------------------------------

def redact_obj(obj, strict: bool | None = None, strip_spi: bool = True):
    """Recursively redact every string in a dict/list structure.

    Value lists are collected once for the whole structure and the vault is
    written once — safe for large payloads like the full pipeline export.
    """
    if strict is None:
        strict = strict_mode()
    known = collect_known_values()
    strict_vals = _strict_values() if strict else None
    mapping: dict[str, str] = {}
    spi_flags: list = []

    def _walk(node):
        if isinstance(node, str):
            return _redact_core(node, known, strict_vals, strip_spi, mapping, spi_flags)
        if isinstance(node, dict):
            return {k: _walk(v) for k, v in node.items()}
        if isinstance(node, (list, tuple)):
            return [_walk(v) for v in node]
        return node

    out = _walk(obj)
    _persist_mappings(mapping)
    return out


def rehydrate_obj(obj, mapping: dict | None = None):
    """Recursively rehydrate every string in a dict/list structure.

    The rehydration lookup is built once for the whole structure.
    """
    lookup: dict | None = None  # built lazily — only if a placeholder is seen

    def _walk(node):
        nonlocal lookup
        if isinstance(node, str):
            if "pii" not in node.casefold():
                return node
            if lookup is None:
                lookup = _rehydration_lookup(mapping)
            unresolved: list[str] = []

            def _repl(m):
                key = (m.group(1).upper(), m.group(2).lower())
                if key in lookup:
                    return lookup[key]
                unresolved.append(m.group(0))
                return m.group(0)

            out = PLACEHOLDER_RE.sub(_repl, node)
            if unresolved:
                log.warning("privacy: %d unresolved placeholder(s): %s",
                            len(unresolved), unresolved[:5])
            return out
        if isinstance(node, dict):
            return {k: _walk(v) for k, v in node.items()}
        if isinstance(node, (list, tuple)):
            return [_walk(v) for v in node]
        return node

    return _walk(obj)
