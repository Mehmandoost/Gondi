"""Metadata encoded in shadow-library download filenames.

Anna's Archive names files:

    {title} -- {author} -- {edition or place, year} -- {publisher}
        -- isbn13 {isbn} -- {md5} -- Anna's Archive
"""

from __future__ import annotations

import re
from dataclasses import dataclass


_SEP = " -- "
_HASH = re.compile(r"^(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
_ATTRIBUTION = re.compile(
    r"^(?:anna['’]s archive|z-?library|libgen|library genesis|sci-hub)$",
    re.IGNORECASE,
)
_ISBN_LABELED = re.compile(
    r"^isbn(?:-?(?:10|13))?\s+([0-9Xx][0-9Xx\-]{8,20})$",
    re.IGNORECASE,
)
_ISBN_BARE = re.compile(r"^97[89][0-9\-]{10,17}$")
_YEAR = re.compile(r"(?:19|20)\d{2}")
_SERIES_EDITION = re.compile(
    r"^(?P<series>.+?),\s*\d+(?:st|nd|rd|th)\s+Edition\b",
    re.IGNORECASE,
)
_UNICODE_ESC = re.compile(r"_\{u([0-9a-fA-F]{4,6})\}")
_EMPTY = {"", "null", "none", "unknown", "n/a"}


@dataclass
class FilenameMeta:
    title: str
    authors: list[str]
    publisher: str | None = None
    published_date: str | None = None
    isbn: str | None = None
    series: str | None = None


def _blank(value: str | None) -> bool:
    return value is None or value.strip().casefold() in _EMPTY


def _unescape(value: str) -> str:
    def repl(match: re.Match[str]) -> str:
        try:
            return chr(int(match.group(1), 16))
        except ValueError:
            return match.group(0)

    text = _UNICODE_ESC.sub(repl, value)
    text = text.replace("\u200b", "").replace("\ufeff", "")
    text = re.sub(r"\s_,", ",", text)
    text = text.replace(" _ ", ": ")
    previous = None
    while previous != text:
        previous = text
        # "D_ J_ MacHale" -> "D.J. MacHale". Do not eat the space before a surname.
        text = re.sub(r"\b([A-Z])_ +([A-Z])_(?=\s|$)", r"\1.\2.", text)
        text = re.sub(r"\b([A-Z])_(?=[A-Z]|\s|$)", r"\1.", text)
    text = re.sub(r"\b([A-Z]{2,})_(?=\s)", r"\1.", text)
    text = re.sub(r"\s+_+\s*$", "", text)
    text = re.sub(r"\s+", " ", text).strip(" _")
    return text.strip()


def _isbn_digits(value: str) -> str | None:
    digits = re.sub(r"[^0-9Xx]", "", value)
    if len(digits) in (10, 13):
        return digits.upper()
    return None


def _isbn_from_part(part: str) -> str | None:
    labeled = _ISBN_LABELED.match(part.strip())
    if labeled:
        return _isbn_digits(labeled.group(1))
    if _ISBN_BARE.match(part.strip()):
        return _isbn_digits(part)
    return None


def _looks_like_edition(value: str) -> bool:
    if _YEAR.search(value):
        return True
    if value.casefold().startswith("null"):
        return True
    return bool(re.search(r"\bedition\b", value, re.IGNORECASE))


def _authors(value: str) -> list[str]:
    authors: list[str] = []
    for part in value.split(";"):
        name = _unescape(part)
        if _blank(name):
            continue
        authors.append(name)
    return authors


def _year_and_series(edition: str) -> tuple[str | None, str | None]:
    if _blank(edition):
        return None, None
    years = _YEAR.findall(edition)
    year = years[-1] if years else None
    series = None
    match = _SERIES_EDITION.match(edition.strip())
    if match:
        candidate = match.group("series").strip(" ,")
        if not _blank(candidate):
            series = candidate
    return year, series


def parse_archive_filename(stem: str) -> FilenameMeta | None:
    """Parse an Anna's Archive-style `` -- `` filename. Returns None otherwise."""
    if _SEP not in stem:
        return None
    parts = [part.strip() for part in stem.split(_SEP) if part.strip()]
    if len(parts) < 3:
        return None

    isbn: str | None = None
    while parts:
        tail = parts[-1].strip()
        if _ATTRIBUTION.match(tail) or _HASH.match(tail):
            parts.pop()
            continue
        found = _isbn_from_part(tail)
        if found:
            isbn = isbn or found
            parts.pop()
            continue
        break

    if len(parts) < 2:
        return None

    title = _unescape(parts[0])
    if _blank(title):
        return None
    authors = _authors(parts[1])
    if not authors:
        return None

    publisher: str | None = None
    published_date: str | None = None
    series: str | None = None
    rest = parts[2:]
    if rest and _looks_like_edition(rest[0]):
        published_date, series = _year_and_series(_unescape(rest[0]))
        if len(rest) > 1:
            publisher = _unescape(rest[1])
    elif rest:
        publisher = _unescape(rest[0])

    if _blank(publisher):
        publisher = None

    return FilenameMeta(
        title=title,
        authors=authors,
        publisher=publisher,
        published_date=published_date,
        isbn=isbn,
        series=series,
    )
