"""Text sanitisation for anything that reaches a packet or the review queue.

Packet text must never contain markup, control or bidirectional-override
characters, or the bracket characters used by citations and the packet
wrapper. Values are rejected rather than truncated, because a shortened value
can change what a fact says.
"""

from __future__ import annotations

import html
import re
import unicodedata

_CONTROL = re.compile(
    "[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f­؜᠎​-‏"
    " -‮⁠-⁯﻿￹-￻]"
)
_SCRIPT = re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>", re.I | re.S)
_TAG = re.compile(r"<[^>]{0,4000}>")
_FORBIDDEN = re.compile(r"[\[\]<>{}`|\\]")
_WS = re.compile(r"\s+")

VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+_-]{0,39}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._/-]{0,39}$")


_FORBIDDEN_ASCII = frozenset("[]<>{}`|\\")
_EXTRA_QUOTES = frozenset("\u2039\u203a\u00ab\u00bb")  # single and double guillemets


def _unsafe(ch: str) -> bool:
    """Characters that are invisible, unassigned, private, bracket-like, or a
    compatibility lookalike of a forbidden ASCII character (fullwidth ［ ］ ＜ ＞ ...)."""
    if ch in "\t\n\r" or ch.isascii() and ch.isprintable():
        return False
    cat = unicodedata.category(ch)
    if cat[0] == "C":  # Cc, Cf (incl. bidi, zero-width, tag characters), Cs, Co, Cn
        return True
    if cat in ("Ps", "Pe") or ch in _EXTRA_QUOTES:
        return True
    folded = unicodedata.normalize("NFKC", ch)
    return any(c in _FORBIDDEN_ASCII for c in folded)


def _strip_unsafe(text: str) -> str:
    if text.isascii():
        return _CONTROL.sub("", text)
    return "".join(ch for ch in _CONTROL.sub("", text) if not _unsafe(ch))


def plain_text(raw: str, max_len: int) -> str:
    """Strip markup and control characters, collapse whitespace, cut to ``max_len``.

    Only for review-queue input, which a person reads before anything is
    promoted. Packet values use :func:`inline` instead.
    """
    text = _SCRIPT.sub(" ", raw)
    text = _TAG.sub(" ", text)
    text = html.unescape(text)
    text = _strip_unsafe(text)
    text = _WS.sub(" ", text).strip()
    return text[:max_len]


def inline(raw: object, max_len: int, what: str = "value") -> str:
    """A single-line value safe to place in a packet line.

    Raises ``ValueError`` if the value is empty or longer than ``max_len``.
    """
    if not isinstance(raw, str):
        raise ValueError(f"{what} must be a string")
    text = _TAG.sub(" ", raw)
    text = html.unescape(text)
    text = _strip_unsafe(text)
    text = _FORBIDDEN.sub("", text)
    text = _WS.sub(" ", text).strip()
    if not text:
        raise ValueError(f"{what} is empty")
    if len(text) > max_len:
        raise ValueError(f"{what} is longer than {max_len} characters")
    return text


def version(raw: object) -> str:
    if not isinstance(raw, str) or not VERSION_RE.match(raw.strip()):
        raise ValueError(f"invalid version {raw!r}")
    return raw.strip()


def token(raw: object, what: str = "token") -> str:
    if not isinstance(raw, str) or not TOKEN_RE.match(raw.strip()):
        raise ValueError(f"invalid {what} {raw!r}")
    return raw.strip()
