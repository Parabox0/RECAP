"""Minimal Atom and RSS 2.0 parsing.

Documents with a DTD are rejected outright, which removes entity-expansion
and external-entity attacks; real feeds do not need one.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from xml.parsers import expat
from dataclasses import dataclass, field

from ..sanitize import plain_text
from ..timeutil import parse_time

ATOM = "{http://www.w3.org/2005/Atom}"
HISTORY = "{http://purl.org/syndication/history/1.0}"
_DTD = re.compile(rb"<!\s*(doctype|entity)", re.I)


class FeedError(ValueError):
    pass


@dataclass
class FeedEntry:
    id: str
    title: str
    published: int | None
    updated: int | None
    summary: str = ""
    link: str = ""


@dataclass
class FeedDoc:
    kind: str
    entries: list[FeedEntry] = field(default_factory=list)
    links: dict[str, str] = field(default_factory=dict)
    complete: bool = False


def _text(el: ET.Element | None) -> str:
    if el is None:
        return ""
    return "".join(el.itertext()).strip()


def _time(text: str) -> int | None:
    if not text:
        return None
    try:
        return parse_time(text)
    except ValueError:
        return None


class _DTDFound(Exception):
    pass


def _reject_dtd(body: bytes) -> None:
    """Refuse any DTD, whatever the document encoding (UTF-16 hides it from a byte scan)."""
    if _DTD.search(body):
        raise FeedError("documents with a DTD are not accepted")
    parser = expat.ParserCreate()

    def found(*_args):
        raise _DTDFound()

    parser.StartDoctypeDeclHandler = found
    parser.EntityDeclHandler = found
    try:
        parser.Parse(body, True)
    except _DTDFound:
        raise FeedError("documents with a DTD are not accepted") from None
    except expat.ExpatError as exc:
        raise FeedError(f"not well-formed XML: {exc}") from None


def parse_feed(body: bytes) -> FeedDoc:
    _reject_dtd(body)
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise FeedError(f"not well-formed XML: {exc}") from None
    if root.tag == f"{ATOM}feed":
        return _atom(root)
    if root.tag == "rss":
        return _rss(root)
    raise FeedError(f"unsupported feed root {root.tag!r}")


def _atom(root: ET.Element) -> FeedDoc:
    doc = FeedDoc(kind="atom", complete=root.find(f"{HISTORY}complete") is not None)
    for link in root.findall(f"{ATOM}link"):
        rel, href = link.get("rel", "alternate"), link.get("href")
        if href:
            doc.links.setdefault(rel, href)
    for entry in root.findall(f"{ATOM}entry"):
        link = ""
        for el in entry.findall(f"{ATOM}link"):
            if el.get("rel", "alternate") == "alternate" and el.get("href"):
                link = el.get("href")
                break
        content = _text(entry.find(f"{ATOM}content")) or _text(entry.find(f"{ATOM}summary"))
        doc.entries.append(
            FeedEntry(
                id=_text(entry.find(f"{ATOM}id")),
                title=plain_text(_text(entry.find(f"{ATOM}title")), 300),
                published=_time(_text(entry.find(f"{ATOM}published"))),
                updated=_time(_text(entry.find(f"{ATOM}updated"))),
                summary=plain_text(content, 4000),
                link=link,
            )
        )
    return doc


def _rss(root: ET.Element) -> FeedDoc:
    channel = root.find("channel")
    if channel is None:
        raise FeedError("RSS document without a channel")
    doc = FeedDoc(kind="rss")
    for link in channel.findall(f"{ATOM}link"):
        rel, href = link.get("rel", "alternate"), link.get("href")
        if href:
            doc.links.setdefault(rel, href)
    for item in channel.findall("item"):
        published = _time(_text(item.find("pubDate")))
        guid = _text(item.find("guid")) or _text(item.find("link"))
        doc.entries.append(
            FeedEntry(
                id=guid,
                title=plain_text(_text(item.find("title")), 300),
                published=published,
                updated=None,  # RSS has no revision field; never invent one from pubDate
                summary=plain_text(_text(item.find("description")), 4000),
                link=_text(item.find("link")),
            )
        )
    return doc
