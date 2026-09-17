"""Keep secrets out of error strings, health records and pages.

Exceptions raised by HTTP clients often quote the URL they called or the
header they sent; a key passed as a query parameter would otherwise end up
verbatim in ``connections.json`` and on the dashboard. Every error that is
stored or displayed goes through :func:`describe_error` / :func:`redact_secrets`
first. The transport code also avoids putting keys in URLs in the first
place, so this is belt and braces rather than the only line of defence.
"""

from __future__ import annotations

import os
import re
from urllib.parse import quote

MASK = "•••"

_KEY_PARAM = re.compile(r"([?&](?:key|api[_-]?key|token|access[_-]?token|secret|password)=)[^&\s'\"]+", re.I)
_BEARER = re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]{6,}")
_KEY_SHAPES = re.compile(
    r"\b(?:AIza[0-9A-Za-z_\-]{20,}"  # Google API keys
    r"|AQ\.[0-9A-Za-z_\-]{20,}"  # newer Google AI Studio keys
    r"|sk-(?:or-|proj-|ant-)?[0-9A-Za-z_\-]{16,}"  # OpenAI / OpenRouter / Anthropic
    r"|xox[abprs]-[0-9A-Za-z\-]{10,}"  # Slack
    r"|\d{8,10}:[0-9A-Za-z_\-]{30,})"  # Telegram bot tokens
)


def secret_values() -> list[str]:
    """Every saved / exported secret value worth hiding, longest first."""
    from .settings import SECRET_NAMES

    from .providers import registry

    vals: set[str] = set()
    for v in [os.environ.get(name) for name in SECRET_NAMES] + registry().secret_values():
        if v and len(v) >= 6:
            vals.add(v)
            q = quote(v, safe="")
            if q != v:
                vals.add(q)
    return sorted(vals, key=len, reverse=True)


def redact_secrets(text: str | None) -> str | None:
    """Mask saved secret values and anything that looks like a key or token."""
    if not text:
        return text
    out = str(text)
    for v in secret_values():
        out = out.replace(v, MASK)
    out = _KEY_PARAM.sub(r"\1" + MASK, out)
    out = _BEARER.sub(r"\1" + MASK, out)
    out = _KEY_SHAPES.sub(MASK, out)
    return out


def describe_error(exc: BaseException) -> str:
    """``TypeName: message`` with secrets masked - the only way errors should be stringified for storage."""
    return redact_secrets(f"{type(exc).__name__}: {exc}") or type(exc).__name__
