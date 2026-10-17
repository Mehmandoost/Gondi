"""Read EPUB metadata from OPF when ebooklib cannot load the full archive."""

from __future__ import annotations

import re
import zipfile
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree as ET
from zipfile import ZipFile

_CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _text(element: ET.Element | None) -> str | None:
    if element is None:
        return None
    text = (element.text or "").strip()
    return text or None


def _isbn_from_identifiers(values: list[str]) -> str | None:
    for value in values:
        digits = re.sub(r"[^0-9]", "", value)
        if len(digits) in (10, 13):
            return digits
    return None


def _authors_from_creators(
    creators: list[tuple[str | None, str]], roles: dict[str, str]
) -> list[str]:
    if not creators:
        return []
    filter_by_role = bool(roles)
    authors: list[str] = []
    for creator_id, name in creators:
        if not name:
            continue
        cid = creator_id or ""
        if filter_by_role:
            if roles.get(cid) == "aut":
                authors.append(name)
        else:
            authors.append(name)
    return authors


def _opf_path(zf: ZipFile) -> tuple[str, str]:
    container = ET.fromstring(zf.read("META-INF/container.xml"))
    rootfile = container.find(f".//{{{_CONTAINER_NS}}}rootfile")
    if rootfile is None:
        raise ValueError("EPUB container.xml has no rootfile")
    opf_path = rootfile.get("full-path") or ""
    if not opf_path:
        raise ValueError("EPUB rootfile has no full-path")
    opf_dir = str(PurePosixPath(opf_path).parent)
    if opf_dir == ".":
        opf_dir = ""
    else:
        opf_dir += "/"
    return opf_path, opf_dir


def _zip_entry_path(opf_dir: str, href: str) -> str:
    return str(PurePosixPath(opf_dir, href))


def _read_cover(zf: ZipFile, opf_root: ET.Element, opf_dir: str) -> tuple[bytes | None, str]:
    cover_href: str | None = None
    metadata = None
    manifest = None
    for child in opf_root:
        name = _local_name(child.tag)
        if name == "metadata":
            metadata = child
        elif name == "manifest":
            manifest = child

    if metadata is not None:
        for el in metadata:
            if _local_name(el.tag) != "meta":
                continue
            if el.get("name") == "cover" and el.get("content"):
                cover_href = el.get("content")
                break

    if manifest is not None:
        for item in manifest:
            if _local_name(item.tag) != "item":
                continue
            props = item.get("properties") or ""
            href = item.get("href")
            if not href:
                continue
            if "cover-image" in props.split():
                cover_href = href
                break

    if not cover_href:
        return None, ".jpg"

    zip_path = _zip_entry_path(opf_dir, cover_href)
    if zip_path not in zf.namelist():
        return None, ".jpg"

    cover_ext = "." + cover_href.rsplit(".", 1)[-1].lower() if "." in cover_href else ".jpg"
    return zf.read(zip_path), cover_ext


def parse_epub_opf(path: Path) -> dict:
    """
    Extract Dublin Core and cover data from content.opf without loading manifest bodies.
    """
    with zipfile.ZipFile(path) as zf:
        opf_path, opf_dir = _opf_path(zf)
        opf_root = ET.fromstring(zf.read(opf_path))

        metadata = None
        for child in opf_root:
            if _local_name(child.tag) == "metadata":
                metadata = child
                break
        if metadata is None:
            raise ValueError("EPUB OPF has no metadata section")

        titles: list[str] = []
        creators: list[tuple[str | None, str]] = []
        roles: dict[str, str] = {}
        descriptions: list[str] = []
        publishers: list[str] = []
        dates: list[str] = []
        languages: list[str] = []
        subjects: list[str] = []
        identifiers: list[str] = []

        for el in metadata:
            local = _local_name(el.tag)
            if local == "meta":
                if el.get("property") == "role" and el.get("refines"):
                    creator_id = el.get("refines", "").lstrip("#")
                    roles[creator_id] = (_text(el) or "").strip()
                continue
            value = _text(el)
            if not value:
                continue
            if local == "title":
                titles.append(value)
            elif local == "creator":
                creators.append((el.get("id"), value))
            elif local == "description":
                descriptions.append(value)
            elif local == "publisher":
                publishers.append(value)
            elif local == "date":
                dates.append(value)
            elif local == "language":
                languages.append(value)
            elif local == "subject":
                subjects.append(value)
            elif local == "identifier":
                identifiers.append(value)

        cover_bytes, cover_ext = _read_cover(zf, opf_root, opf_dir)

        return {
            "title": titles[0] if titles else None,
            "authors": _authors_from_creators(creators, roles),
            "description": descriptions[0] if descriptions else None,
            "publisher": publishers[0] if publishers else None,
            "published_date": dates[0] if dates else None,
            "language": languages[0] if languages else None,
            "tags": subjects,
            "isbn": _isbn_from_identifiers(identifiers),
            "cover_bytes": cover_bytes,
            "cover_ext": cover_ext,
        }
