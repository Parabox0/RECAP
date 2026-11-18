"""Typed, deterministic rendering of packet lines (spec section 3, "Packing").

Each fact becomes one self-contained line that keeps the qualifiers needed to
avoid a wrong answer. Lines never drop a qualifier or round a value: a fact
that cannot be rendered in full raises :class:`RenderError` and is not used.
"""

from __future__ import annotations

from . import sanitize
from .index import ANNOUNCEMENT
from .model import REVIEWED, FactVersion
from .timeutil import date_str

PHRASE_MAX = 80
SENTENCE_MAX = 160

_VERBS = {
    "deprecation": ("deprecated", "scheduled for deprecation on"),
    "removal": ("removed", "scheduled for removal on"),
    "end_of_support": ("reached end of support on", "scheduled to reach end of support on"),
}


class RenderError(ValueError):
    pass


def wrapper(as_of: int) -> tuple[str, str]:
    return (f'<recap as_of="{date_str(as_of)}">', "</recap>")


def _date(ts: int | None, what: str) -> str:
    if ts is None:
        raise RenderError(f"missing {what}")
    return date_str(ts)


def _sentence(text: str) -> str:
    return text if text.endswith((".", "!", "?")) else text + "."


def render_body(v: FactVersion, cls: str) -> str:
    """The line text without the citation tag."""
    try:
        label = sanitize.inline(v.entity_label, 60, "entity label")
        scope = "" if v.scope == "global" else f" Scope: {sanitize.token(v.scope, 'scope')}."
        pred = v.predicate
        announce = cls == ANNOUNCEMENT
        if pred == "release":
            version = sanitize.version(v.value.get("version"))
            channel = sanitize.token(v.qualifiers.get("channel") or "stable", "channel")
            when = _date(v.effective_at if announce else v.change_time, "release date")
            verb = "scheduled for release" if announce else "released"
            body = f"{label} {version} ({channel}) {verb} {when}."
        elif pred == "release_highlights":
            version = sanitize.version(v.qualifiers.get("version") or v.subject)
            text = sanitize.inline(v.value.get("text"), SENTENCE_MAX, "highlights")
            tag = "reviewed" if v.origin == REVIEWED else "official"
            body = f"{label} {version} highlights ({tag}): {_sentence(text)}"
        elif pred in _VERBS:
            item = sanitize.inline(v.value.get("item") or v.subject, PHRASE_MAX, "item")
            active_verb, scheduled_verb = _VERBS[pred]
            when = _date(v.effective_at if announce else v.change_time, "effective date")
            body = f"{label}: {item} {scheduled_verb if announce else active_verb} {when}."
        elif pred == "status":
            status = sanitize.inline(v.value.get("status"), PHRASE_MAX, "status")
            component = v.value.get("component")
            subject = f"{label} {sanitize.inline(component, PHRASE_MAX, 'component')}" if component else label
            when = _date(v.effective_at if announce else v.change_time, "status date")
            if announce:
                body = f"{subject} status scheduled: {status} from {when}"
            else:
                body = f"{subject} status: {status} since {when}"
            if v.valid_to is not None:
                body += f" until {date_str(v.valid_to)}"
            body += "."
        elif pred == "observation":
            metric = sanitize.inline(v.value.get("metric"), PHRASE_MAX, "metric")
            number = v.value.get("value")
            if isinstance(number, bool) or not isinstance(number, (str, int, float)):
                raise RenderError("observation value must be a number or numeric string")
            number = sanitize.inline(str(number), 32, "observation value")
            try:
                float(number)
            except ValueError:
                raise RenderError("observation value is not numeric") from None
            unit = sanitize.inline(v.value.get("unit"), 32, "unit")
            period = sanitize.inline(v.value.get("period"), 40, "reference period")
            body = f"{label} {metric}: {number} {unit} ({period})."
        elif pred == "notice":
            text = sanitize.inline(v.value.get("text"), SENTENCE_MAX, "notice")
            tag = "reviewed" if v.origin == REVIEWED else "official"
            body = f"{label} notice {_date(v.change_time, 'notice date')} ({tag}): {_sentence(text)}"
        else:
            raise RenderError(f"no template for predicate {pred!r}")
    except ValueError as exc:
        if isinstance(exc, RenderError):
            raise
        raise RenderError(str(exc)) from None
    return body + scope


def render_line(v: FactVersion, cls: str, ref: str) -> str:
    return f"[{ref}] {render_body(v, cls)}"


def render_packet(open_tag: str, close_tag: str, lines: list[str]) -> str:
    return open_tag + "\n" + "".join(line + "\n" for line in lines) + close_tag
