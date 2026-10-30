"""Remove credentials from anything before it is written to disk.

Redaction favours privacy over fidelity: a false positive costs a few
characters of a recorded command, a false negative leaks a secret.
"""
from __future__ import annotations

import json
import re
from collections import Counter

SENSITIVE_FILE_PLACEHOLDER = "[REDACTED:sensitive_file]"

# `/充值` (or full-width `／充值`) opens the codex-cdk-recharge window; whatever follows may be a CDK.
RECHARGE_COMMANDS = ("/充值", "／充值")
RECHARGE_PLACEHOLDER = "[REDACTED:recharge_command]"

# Order matters: specific token formats first, then generic header/URL/assignment rules.
_VALUE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private_key", re.compile(
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{16,}")),
    ("openai_key", re.compile(r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{20,}")),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("session_key", re.compile(r"\bsess-[A-Za-z0-9]{20,}")),  # ChatGPT session keys
    # Recharge binding tokens (codex-cdk-recharge) and sync device tokens (docs/sync-api.md).
    ("app_token", re.compile(r"\b(?:cdkb|ctrd)_[A-Za-z0-9_\-]{8,}")),
]

# A CDK after a keyword: `兑换码：X`, `cdk="X"`, `{"cdk":"X"}`, `CDK码：「X」`, `兑换码是“X”`, `激活码X`.
# Without an explicit marker (`:` `=` 是 为 or a quote) the value must look like a code —
# upper-case letters and digits with a digit or dash, or letters mixed with digits — so that
# ordinary prose such as "the CDK window" is left alone.
_CDK_CONTEXT = re.compile(
    r"((?<![A-Za-z])(?i:cdk(?:ey)?|redeem[ _-]?code|activation[ _-]?code)(?![A-Za-z])|兑换码|激活码|卡密|充值码)"
    r"(码?[\"'”」』]?\s*(?:是|为)?\s*[:：=]?\s*[\"'“「『(（]?\s*)"
    r"([A-Za-z0-9][A-Za-z0-9_-]{3,127})")
_CDK_MARKERS = set(":：=是为\"'“「『(（")
_CODE_LIKE = re.compile(r"[A-Z0-9_-]+")
_PLACEHOLDER = "[REDACTED"
# Grouped codes such as AB12-CD34-EF56-GH78 (3–6 groups, letters and digits), keyword or not.
_GROUPED_CODE = re.compile(r"(?<![A-Za-z0-9-])[A-Z0-9]{4,6}(?:-[A-Z0-9]{4,6}){2,5}(?![A-Za-z0-9-])")

_AUTH_HEADER = re.compile(
    r"(?i)\b(authorization|proxy-authorization|set-cookie|cookie|x-api-key)(\s*:\s*)([^\r\n\"']+)")
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9\-._~+/]{16,}=*")
_URL_CREDENTIALS = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://[^/\s:@]+:)([^/\s@]+)(@)")
_ASSIGNMENT = re.compile(
    r"(?i)\b([A-Za-z0-9_.-]*(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key"
    r"|private[_-]?key|client[_-]?secret|credential)[A-Za-z0-9_.-]*)"
    r"(\s*[:=]\s*)(\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s\"',;()\[\]{}]+)")
_NOT_A_SECRET = {"str", "int", "bool", "none", "null", "true", "false", "string",
                 "required", "optional", "any", "bytes", "float", "undefined"}

_KEY_SUBSTRINGS = ("password", "passwd", "secret", "api_key", "apikey", "api-key", "access_key",
                   "accesskey", "private_key", "privatekey", "authorization", "cookie",
                   "credential", "session_key", "bearer", "兑换码", "激活码", "卡密", "充值码")
_KEY_EXACT = {"pwd", "token", "auth", "auth_token", "access_token", "refresh_token",
              "id_token", "session_token", "sessionid", "session_cookie"}
# Matched against the key with case, `_`, `-` and spaces removed: cdkCode, user_cdk, Redeem Code…
_KEY_NORMALISED = ("cdk", "redeemcode", "activationcode")
_KEY_SEPARATORS = re.compile(r"[\s_\-]")

_SENSITIVE_FILE = re.compile(
    r"(?i)(?:^|[\\/\s\"'=:(])"
    r"(auth\.json|\.env(?:\.[\w.-]+)?|id_(?:rsa|ed25519|ecdsa|dsa)[\w.-]*|[\w.-]+\.(?:pem|pfx|p12)"
    r"|cap_sid|credentials(?:\.json)?|\.netrc|\.git-credentials|\.npmrc|\.pypirc)"
    r"(?=$|[\s\"'\\/;|&),])")


def _assignment_value_is_secret(raw: str) -> bool:
    value = raw.strip("\"'")
    if len(value) < 4 or value.startswith(("[REDACTED", "$", "%", "${")):
        return False
    if value.isdigit() or value.lower() in _NOT_A_SECRET:
        return False
    return True


def redact_text(text: str) -> tuple[str, Counter]:
    counts: Counter = Counter()
    if text.lstrip().startswith(RECHARGE_COMMANDS):
        counts["recharge_command"] += 1
        return RECHARGE_PLACEHOLDER, counts
    for kind, pattern in _VALUE_PATTERNS:
        text, n = pattern.subn(f"[REDACTED:{kind}]", text)
        if n:
            counts[kind] += n

    def cdk(m: re.Match[str]) -> str:
        value = m.group(3)
        explicit = any(c in _CDK_MARKERS for c in m.group(2))
        upper_code = _CODE_LIKE.fullmatch(value) and any(c.isdigit() or c == "-" for c in value)
        mixed_code = len(value) >= 6 and any(c.isdigit() for c in value) and any(c.isalpha() for c in value)
        if not (explicit or upper_code or mixed_code):
            return m.group(0)
        counts["cdk"] += 1
        return f"{m.group(1)}{m.group(2)}[REDACTED:cdk]"

    def grouped(m: re.Match[str]) -> str:
        code = m.group(0)
        if not (any(c.isdigit() for c in code) and any(c.isalpha() for c in code)):
            return code
        counts["cdk_like"] += 1
        return "[REDACTED:cdk_like]"

    text = _CDK_CONTEXT.sub(cdk, text)
    text = _GROUPED_CODE.sub(grouped, text)

    def header(m: re.Match[str]) -> str:
        if m.group(3).startswith(_PLACEHOLDER):  # already redacted: don't count it again
            return m.group(0)
        counts["auth_header"] += 1
        return f"{m.group(1)}{m.group(2)}[REDACTED:auth_header]"

    def url(m: re.Match[str]) -> str:
        if m.group(2).startswith(_PLACEHOLDER):
            return m.group(0)
        counts["url_password"] += 1
        return f"{m.group(1)}[REDACTED:url_password]{m.group(3)}"

    text = _AUTH_HEADER.sub(header, text)
    text, n = _BEARER.subn("Bearer [REDACTED:bearer]", text)
    if n:
        counts["bearer"] += n
    text = _URL_CREDENTIALS.sub(url, text)

    def assignment(m: re.Match[str]) -> str:
        if not _assignment_value_is_secret(m.group(3)):
            return m.group(0)
        counts["assignment"] += 1
        return f"{m.group(1)}{m.group(2)}[REDACTED:assignment]"

    text = _ASSIGNMENT.sub(assignment, text)
    return text, counts


def _sensitive_key(key: str) -> bool:
    k = key.lower()
    norm = _KEY_SEPARATORS.sub("", k)
    return (k in _KEY_EXACT or k.endswith("_token") or k.endswith("-token")
            or any(s in k for s in _KEY_SUBSTRINGS) or any(s in norm for s in _KEY_NORMALISED))


def redact_obj(obj: object) -> tuple[object, Counter]:
    """Return a redacted deep copy of a JSON-like value and per-kind counts."""
    counts: Counter = Counter()

    def walk(value: object) -> object:
        if isinstance(value, str):
            out, c = redact_text(value)
            counts.update(c)
            return out
        if isinstance(value, dict):
            result = {}
            for k, v in value.items():
                if isinstance(k, str) and isinstance(v, str) and v and _sensitive_key(k):
                    if not v.startswith(_PLACEHOLDER):
                        counts["key"] += 1
                    result[k] = "[REDACTED:key]"
                else:
                    result[k] = walk(v)
            return result
        if isinstance(value, (list, tuple)):
            return [walk(v) for v in value]
        return value

    return walk(obj), counts


def mentions_sensitive_file(obj: object) -> bool:
    """True when a tool input refers to a credential file (its output is then dropped)."""
    if obj is None:
        return False
    text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, default=str)
    return _SENSITIVE_FILE.search(text) is not None
