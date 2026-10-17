import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import ebooklib
from ebooklib import epub
from pypdf import PdfReader

from app import db
from app.epub_opf import parse_epub_opf
from app.epub_text import normalize_published_date, strip_html
from app.filename_meta import parse_archive_filename
from app.languages import normalize_language


EBOOK_EXTENSIONS = {".epub", ".pdf", ".mobi", ".azw3"}

_VOLUME_SUFFIX = re.compile(r":\s*Volume\s+(\d+)\s*$", re.IGNORECASE)
_OPF_NS = "{http://www.idpf.org/2007/opf}"
_TOOL_PUBLISHER = re.compile(
    r"(adobe|acrobat|ghostscript|itext|princexml|latex|xelatex|libreoffice|"
    r"openoffice|microsoft\s+(?:word|print)|quartz|cairo|skia|"
    r"pdf\s*converter|pdf\s*library|google\s+books|macos|mac\s*os|cups)",
    re.IGNORECASE,
)
_TRUNCATED_END = re.compile(
    r"\b(?:a|an|the|of|on|and|or|for|to|in|with)$", re.IGNORECASE
)
_FIRST_PUBLISHED = re.compile(
    r"First published\s+(\d{4})\s*(?:\n+\s*)?by\s+([^\n]{2,80})",
    re.IGNORECASE,
)
_CIP_SERIES = re.compile(r"Series:\s*([^|\n]+)", re.IGNORECASE)
_CIP_SUBJECTS = re.compile(
    r"LCSH:\s*(.+?)(?:\n\s*Classification:|\Z)",
    re.IGNORECASE | re.DOTALL,
)
_TEXT_ISBN = re.compile(
    r"ISBN(?:-1[03])?[:\s]+([0-9][0-9\-]{8,22}[0-9Xx])",
    re.IGNORECASE,
)


@dataclass
class ParsedBook:
    title: str
    authors: list[str]
    description: str | None = None
    publisher: str | None = None
    published_date: str | None = None
    language: str | None = None
    isbn: str | None = None
    tags: list[str] = field(default_factory=list)
    series: str | None = None
    series_index: float | None = None
    cover_bytes: bytes | None = None
    cover_ext: str = ".jpg"


@dataclass
class ImportResult:
    added: int = 0
    merged: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


def format_from_path(path: Path) -> str | None:
    ext = path.suffix.lower().lstrip(".")
    if ext in db.SUPPORTED_FORMATS:
        return ext
    return None


def _first(items: list[str] | None) -> str | None:
    if not items:
        return None
    for item in items:
        if item and item.strip():
            return item.strip()
    return None


def _parse_filename(path: Path) -> ParsedBook:
    archive = parse_archive_filename(path.stem)
    if archive:
        return ParsedBook(
            title=archive.title,
            authors=archive.authors,
            publisher=archive.publisher,
            published_date=archive.published_date,
            isbn=archive.isbn,
            series=archive.series,
        )

    stem = path.stem
    authors: list[str] = []
    title = stem
    if " - " in stem:
        parts = stem.split(" - ", 1)
        if len(parts) == 2:
            left, right = parts[0].strip(), parts[1].strip()
            # Heuristic: "Author - Title" vs "Title - Author"
            if "," in left or len(left.split()) <= 4:
                authors = [a.strip() for a in re.split(r",|&", left) if a.strip()]
                title = right
            else:
                title = left
                authors = [right]
    if not authors:
        authors = ["Unknown"]
    return ParsedBook(title=title or "Unknown", authors=authors)


def _epub_creator_roles(book: epub.EpubBook) -> dict[str, str]:
    roles: dict[str, str] = {}
    for _name, attrs in book.get_metadata("DC", "creator"):
        if not attrs:
            continue
        creator_id = attrs.get("id")
        role = attrs.get(f"{_OPF_NS}role") or attrs.get("role")
        if creator_id and role:
            roles[creator_id] = str(role).strip()
    for value, attrs in book.get_metadata("OPF", "meta"):
        if not attrs:
            continue
        if attrs.get("property") != "role":
            continue
        refines = attrs.get("refines")
        if not refines:
            continue
        creator_id = refines.lstrip("#")
        roles[creator_id] = (value or "").strip()
    return roles


def _authors_from_creator_meta(
    author_meta: list[tuple[str, dict]], roles: dict[str, str]
) -> list[str]:
    creators = [
        ((attrs or {}).get("id"), (name or "").strip())
        for name, attrs in author_meta
    ]
    return _authors_from_creators(creators, roles)


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


def _epub_authors(book: epub.EpubBook) -> list[str]:
    author_meta = book.get_metadata("DC", "creator")
    if not author_meta:
        return []
    return _authors_from_creator_meta(author_meta, _epub_creator_roles(book))


def _epub_isbn(book: epub.EpubBook) -> str | None:
    identifiers = book.get_metadata("DC", "identifier")
    preferred: list[str] = []
    fallback: list[str] = []
    for value, attrs in identifiers:
        if not value:
            continue
        attrs = attrs or {}
        scheme = attrs.get("{http://www.idpf.org/2007/opf}scheme") or attrs.get(
            "scheme", ""
        )
        digits = re.sub(r"[^0-9]", "", value)
        if len(digits) not in (10, 13):
            continue
        if scheme.upper() == "ISBN" or attrs.get("id") == "PrimaryID":
            preferred.append(digits)
        else:
            fallback.append(digits)
    for digits in preferred + fallback:
        return digits
    return None


def _series_from_title(title: str) -> tuple[str | None, float | None]:
    match = _VOLUME_SUFFIX.search(title)
    if not match:
        return None, None
    series = title[: match.start()].strip()
    return (series or None, float(match.group(1)))


def _cover_bytes_from_item(item) -> tuple[bytes | None, str]:
    cover_ext = ".jpg"
    cover_bytes = item.get_content()
    name = item.get_name() or item.get_id() or ""
    if "." in name:
        cover_ext = "." + name.rsplit(".", 1)[-1].lower()
    return cover_bytes, cover_ext


def _extract_epub_cover(book: epub.EpubBook) -> tuple[bytes | None, str]:
    cover_ext = ".jpg"
    try:
        for item in book.get_items():
            if item.get_type() == ebooklib.ITEM_COVER:
                return _cover_bytes_from_item(item)

        by_id = {item.get_id(): item for item in book.get_items() if item.get_id()}
        if "cover-image" in by_id:
            return _cover_bytes_from_item(by_id["cover-image"])

        cover_file: str | None = None
        for _value, attrs in book.get_metadata("OPF", "meta"):
            if attrs and attrs.get("name") == "cover" and attrs.get("content"):
                cover_file = str(attrs["content"]).strip()
                break

        if cover_file:
            cover_base = Path(cover_file).name
            for item in book.get_items():
                if item.get_type() != ebooklib.ITEM_IMAGE:
                    continue
                name = item.get_name() or ""
                if name.endswith(cover_file) or Path(name).name == cover_base:
                    return _cover_bytes_from_item(item)

        for item in book.get_items():
            if item.get_type() != ebooklib.ITEM_IMAGE:
                continue
            item_id = (item.get_id() or "").lower()
            if "cover" in item_id:
                return _cover_bytes_from_item(item)
    except Exception:
        pass
    return None, cover_ext


def _build_parsed_epub(
    *,
    fallback: ParsedBook,
    title: str | None,
    authors: list[str],
    description: str | None,
    publisher: str | None,
    published_date: str | None,
    language: str | None,
    isbn: str | None,
    tags: list[str],
    cover_bytes: bytes | None,
    cover_ext: str,
) -> ParsedBook:
    resolved_title = title or fallback.title
    series, series_index = _series_from_title(resolved_title) if resolved_title else (None, None)
    return ParsedBook(
        title=resolved_title,
        authors=authors or fallback.authors,
        description=strip_html(description),
        publisher=publisher,
        published_date=normalize_published_date(published_date),
        language=normalize_language(language),
        isbn=isbn,
        tags=tags,
        series=series,
        series_index=series_index,
        cover_bytes=cover_bytes,
        cover_ext=cover_ext,
    )


def _parse_epub_ebooklib(path: Path, fallback: ParsedBook) -> ParsedBook:
    book = epub.read_epub(str(path))
    meta = book.get_metadata("DC", "title")
    title = _first([m[0] for m in meta]) if meta else None
    authors = _epub_authors(book)
    desc_meta = book.get_metadata("DC", "description")
    description = _first([m[0] for m in desc_meta]) if desc_meta else None
    pub_meta = book.get_metadata("DC", "publisher")
    publisher = _first([m[0] for m in pub_meta]) if pub_meta else None
    date_meta = book.get_metadata("DC", "date")
    published_date = _first([m[0] for m in date_meta]) if date_meta else None
    lang_meta = book.get_metadata("DC", "language")
    language = _first([m[0] for m in lang_meta]) if lang_meta else None
    subject_meta = book.get_metadata("DC", "subject")
    tags = [m[0].strip() for m in subject_meta if m[0].strip()] if subject_meta else []
    isbn = _epub_isbn(book)
    cover_bytes, cover_ext = _extract_epub_cover(book)
    return _build_parsed_epub(
        fallback=fallback,
        title=title,
        authors=authors,
        description=description,
        publisher=publisher,
        published_date=published_date,
        language=language,
        isbn=isbn,
        tags=tags,
        cover_bytes=cover_bytes,
        cover_ext=cover_ext,
    )


def parse_epub(path: Path, fallback_path: Path | None = None) -> ParsedBook:
    fb_path = fallback_path or path
    fallback = _parse_filename(fb_path)
    try:
        return _parse_epub_ebooklib(path, fallback)
    except Exception:
        fields = parse_epub_opf(path)
        return _build_parsed_epub(fallback=fallback, **fields)


def _clean_meta(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.casefold() in {"null", "none", "unknown", "n/a"}:
        return None
    return text


def _looks_truncated(value: str | None) -> bool:
    if not value:
        return False
    text = value.strip()
    if text.count("(") > text.count(")"):
        return True
    if text.endswith((",", ":", ";", "-", "—", "&")):
        return True
    return bool(_TRUNCATED_END.search(text))


def _is_tool_publisher(value: str | None) -> bool:
    return bool(value and _TOOL_PUBLISHER.search(value))


def _langalt(value: dict | None) -> str | None:
    if not value:
        return None
    for key in ("x-default", "en", "en-US", "en-GB"):
        text = _clean_meta(value.get(key))
        if text:
            return text
    for item in value.values():
        text = _clean_meta(str(item) if item is not None else None)
        if text:
            return text
    return None


def _split_list(value: str | None) -> list[str]:
    if not value:
        return []
    parts = re.split(r"[;,]", value)
    return [part.strip() for part in parts if part.strip()]


def _isbn_digits(value: str | None) -> str | None:
    if not value:
        return None
    digits = re.sub(r"[^0-9Xx]", "", value)
    if len(digits) in (10, 13):
        return digits.upper()
    return None


def _page_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def _join_wrapped(lines: list[str]) -> str:
    return re.sub(r"\s+", " ", " ".join(lines)).strip()


def _title_from_pages(pages: list[str], fallback: str | None) -> str | None:
    if not fallback or not _looks_truncated(fallback):
        return None
    folded = fallback.casefold()
    for page in pages:
        lines = _page_lines(page)
        if not lines or not (3 <= len(lines[0]) <= 140):
            continue
        if not folded.startswith(lines[0].casefold()):
            continue
        if len(lines) > 1:
            combined = _join_wrapped(lines[:2])
            if folded.startswith(combined.casefold()) and len(lines[1].split()) <= 8:
                return combined
        return lines[0]
    return None


def _description_from_pages(
    pages: list[str], title: str, authors: list[str]
) -> str | None:
    title_cf = title.casefold()
    author_cf = [author.casefold() for author in authors if author]
    for page in pages:
        lines = _page_lines(page)
        if not lines or not title_cf.startswith(lines[0].casefold()):
            continue
        if lines[0].casefold() != title_cf and not title_cf.startswith(
            _join_wrapped(lines[:2]).casefold()
        ):
            continue
        start = 1
        if start < len(lines) and title_cf.startswith(
            _join_wrapped(lines[:2]).casefold()
        ):
            start = 2
        body: list[str] = []
        for line in lines[start:]:
            folded = line.casefold()
            if any(folded == author for author in author_cf):
                break
            if any(folded.startswith(author + " is ") for author in author_cf):
                break
            body.append(line)
        text = _join_wrapped(body)
        if len(text) >= 80:
            return text[:4000]
    return None


def _subjects_from_text(text: str) -> list[str]:
    match = _CIP_SUBJECTS.search(text)
    if not match:
        return []
    subjects: list[str] = []
    for part in match.group(1).split("|"):
        subject = re.sub(r"\s+", " ", part).strip().rstrip(".")
        if subject and len(subject) <= 80:
            subjects.append(subject)
    return subjects[:20]


def _isbn_from_text(text: str) -> str | None:
    found: list[tuple[str, str]] = []
    for match in _TEXT_ISBN.finditer(text):
        digits = _isbn_digits(match.group(1))
        if not digits:
            continue
        context = text[match.end() : match.end() + 48].casefold()
        found.append((digits, context))
    for digits, context in found:
        if any(label in context for label in ("ebk", "e-book", "ebook")):
            return digits
    if found:
        return found[0][0]
    return None


def _text_metadata(pages: list[str], parsed: ParsedBook) -> ParsedBook:
    if not pages:
        return parsed
    combined = "\n".join(pages)
    title = _title_from_pages(pages, parsed.title) or parsed.title
    description = parsed.description or _description_from_pages(
        pages, title, parsed.authors
    )
    publisher = parsed.publisher
    published_date = parsed.published_date
    published = _FIRST_PUBLISHED.search(combined)
    if published:
        published_date = published.group(1)
        by_publisher = published.group(2).strip(" .")
        if by_publisher and (_looks_truncated(publisher) or not publisher):
            publisher = by_publisher
    series = parsed.series
    series_match = _CIP_SERIES.search(combined)
    if series_match and not series:
        candidate = series_match.group(1).strip(" .")
        if candidate:
            series = candidate
    tags = parsed.tags or _subjects_from_text(combined)
    isbn = _isbn_from_text(combined) or parsed.isbn
    return ParsedBook(
        title=title,
        authors=parsed.authors,
        description=description,
        publisher=publisher,
        published_date=normalize_published_date(published_date),
        language=parsed.language,
        isbn=isbn,
        tags=tags,
        series=series,
        series_index=parsed.series_index,
        cover_bytes=parsed.cover_bytes,
        cover_ext=parsed.cover_ext,
    )


def _pdf_embedded(reader: PdfReader) -> ParsedBook:
    info = reader.metadata
    title = _clean_meta(info.title) if info else None
    author = _clean_meta(info.author) if info else None
    description = _clean_meta(info.subject) if info else None
    keywords = _clean_meta(getattr(info, "keywords", None)) if info else None
    publisher = None
    language = None
    isbn = None
    tags = _split_list(keywords)

    try:
        xmp = reader.xmp_metadata
    except Exception:
        xmp = None
    if xmp:
        title = title or _langalt(xmp.dc_title)
        if not author and xmp.dc_creator:
            author = _clean_meta(xmp.dc_creator[0])
        description = description or _langalt(xmp.dc_description)
        if xmp.dc_publisher:
            publisher = _clean_meta(xmp.dc_publisher[0])
        if xmp.dc_language:
            language = _clean_meta(xmp.dc_language[0])
        isbn = _isbn_digits(_clean_meta(xmp.dc_identifier))
        if not tags and xmp.dc_subject:
            tags = [item.strip() for item in xmp.dc_subject if item and item.strip()]
        if not keywords:
            keywords = _clean_meta(xmp.pdf_keywords)
            if keywords and not tags:
                tags = _split_list(keywords)

    if _is_tool_publisher(publisher):
        publisher = None
    authors = (
        [part.strip() for part in re.split(r";|\s&\s", author) if part.strip()]
        if author
        else []
    )
    return ParsedBook(
        title=title or "",
        authors=authors,
        description=strip_html(description),
        publisher=publisher,
        language=normalize_language(language),
        isbn=isbn,
        tags=tags[:20],
    )


def _merge_pdf(fallback: ParsedBook, embedded: ParsedBook) -> ParsedBook:
    title = embedded.title or fallback.title
    if _looks_truncated(title):
        title = fallback.title if not _looks_truncated(fallback.title) else title
    authors = embedded.authors or fallback.authors
    publisher = embedded.publisher
    if not publisher or _is_tool_publisher(publisher):
        publisher = fallback.publisher
    if _is_tool_publisher(publisher):
        publisher = None
    return ParsedBook(
        title=title or fallback.title or "Unknown",
        authors=authors or ["Unknown"],
        description=embedded.description or fallback.description,
        publisher=publisher,
        published_date=normalize_published_date(
            fallback.published_date
        ),
        language=embedded.language or fallback.language,
        isbn=embedded.isbn or fallback.isbn,
        tags=embedded.tags or fallback.tags,
        series=fallback.series,
        series_index=fallback.series_index,
    )


def _render_pdf_cover(path: Path) -> tuple[bytes | None, str]:
    """Rasterize the first PDF page and return JPEG bytes."""
    try:
        import pymupdf
    except ImportError:
        return None, ".jpg"
    try:
        with pymupdf.open(path) as document:
            if document.page_count < 1:
                return None, ".jpg"
            page = document[0]
            pixmap = page.get_pixmap(dpi=140, alpha=False)
            if pixmap.width > 1400:
                scale = 1400 / pixmap.width
                pixmap = page.get_pixmap(
                    matrix=pymupdf.Matrix(scale, scale), alpha=False
                )
            data = pixmap.tobytes("jpeg", jpg_quality=82)
    except Exception:
        return None, ".jpg"
    if not data:
        return None, ".jpg"
    return data, ".jpg"


def parse_pdf(path: Path, fallback_path: Path | None = None) -> ParsedBook:
    fb_path = fallback_path or path
    fallback = _parse_filename(fb_path)
    try:
        reader = PdfReader(str(path))
        merged = _merge_pdf(fallback, _pdf_embedded(reader))
        page_count = min(12, len(reader.pages))
        pages = []
        for index in range(page_count):
            try:
                pages.append(reader.pages[index].extract_text() or "")
            except Exception:
                pages.append("")
        parsed = _text_metadata(pages, merged)
    except Exception:
        parsed = fallback
    cover_bytes, cover_ext = _render_pdf_cover(path)
    if cover_bytes:
        parsed.cover_bytes = cover_bytes
        parsed.cover_ext = cover_ext
    return parsed


def parse_file(path: Path, fallback_path: Path | None = None) -> ParsedBook:
    fb_path = fallback_path or path
    fmt = format_from_path(path)
    if fmt == "epub":
        try:
            return parse_epub(path, fallback_path=fb_path)
        except Exception:
            return _parse_filename(fb_path)
    if fmt == "pdf":
        return parse_pdf(path, fallback_path=fb_path)
    return _parse_filename(fb_path)


def _store_cover_if_missing(library_id: str, conn, book_id: int, parsed: ParsedBook) -> None:
    if not parsed.cover_bytes:
        return
    row = conn.execute(
        "SELECT cover_path FROM books WHERE id = ?", (book_id,)
    ).fetchone()
    if row and row["cover_path"]:
        return
    cover_path = _save_cover(
        library_id, book_id, parsed.cover_bytes, parsed.cover_ext
    )
    db.set_book_cover(conn, book_id, cover_path)


def _save_cover(library_id: str, book_id: int, data: bytes, ext: str) -> str:
    ext = ext if ext.startswith(".") else f".{ext}"
    if ext not in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
        ext = ".jpg"
    cover_path = db.library_covers_dir(library_id) / f"{book_id}{ext}"
    cover_path.write_bytes(data)
    return str(cover_path)


def _copy_book_file(
    library_id: str, conn, src: Path, book_id: int, fmt: str
) -> tuple[str, int]:
    dest = db.book_format_dest_path(library_id, conn, book_id, src.suffix)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    return str(dest), dest.stat().st_size


def _archive_filename_title(title: str) -> bool:
    return " -- " in title and parse_archive_filename(title) is not None


def _backfill_metadata(
    conn, library_id: str, book_id: int, parsed: ParsedBook
) -> None:
    """Fill empty catalog fields when the same book is imported again."""
    current = db.get_book(conn, book_id)
    if not current:
        return
    fields: dict = {}
    title = current["title"] or ""
    if parsed.title and (
        _archive_filename_title(title) or _looks_truncated(title)
    ):
        if not _looks_truncated(parsed.title):
            fields["title"] = parsed.title
    authors = current["authors"] or []
    if parsed.authors and (
        not authors or authors == ["Unknown"]
    ):
        fields["authors"] = parsed.authors
    publisher = current["publisher"]
    if parsed.publisher and (
        not publisher
        or _is_tool_publisher(publisher)
        or _looks_truncated(publisher)
    ):
        fields["publisher"] = parsed.publisher
    for key in ("description", "published_date", "language", "isbn", "series"):
        if not current.get(key) and getattr(parsed, key):
            fields[key] = getattr(parsed, key)
    if current.get("series_index") is None and parsed.series_index is not None:
        fields["series_index"] = parsed.series_index
    if not current.get("tags") and parsed.tags:
        fields["tags"] = parsed.tags
    if fields:
        db.update_book(conn, book_id, fields, library_id=library_id)


def import_file(
    path: Path,
    conn,
    library_id: str,
    *,
    fallback_path: Path | None = None,
) -> tuple[str, int | None]:
    """
    Returns (status, book_id) where status is 'added', 'merged', or 'skipped'.
    """
    path = path.resolve()
    if not path.is_file():
        return ("skipped", None)

    fmt = format_from_path(path)
    if not fmt:
        return ("skipped", None)

    parsed = parse_file(path, fallback_path=fallback_path)
    existing_id = db.find_book_by_title_authors(conn, parsed.title, parsed.authors)

    if existing_id:
        book_id = existing_id
        _backfill_metadata(conn, library_id, book_id, parsed)
        fmt_row = conn.execute(
            "SELECT id FROM formats WHERE book_id = ? AND format = ?",
            (book_id, fmt),
        ).fetchone()
        if fmt_row:
            _store_cover_if_missing(library_id, conn, book_id, parsed)
            conn.commit()
            return ("skipped", book_id)
        status = "merged"
    else:
        book_id = db.create_book(
            conn,
            title=parsed.title,
            authors=parsed.authors,
            tags=parsed.tags,
            description=parsed.description,
            publisher=parsed.publisher,
            published_date=parsed.published_date,
            language=parsed.language,
            isbn=parsed.isbn,
            series=parsed.series,
            series_index=parsed.series_index,
        )
        status = "added"

    file_path, file_size = _copy_book_file(library_id, conn, path, book_id, fmt)
    db.add_format(conn, book_id, fmt, file_path, file_size)

    _store_cover_if_missing(library_id, conn, book_id, parsed)

    conn.commit()
    return (status, book_id)


def import_folder(library_id: str, folder: str | Path) -> ImportResult:
    result = ImportResult()
    root = Path(folder).expanduser().resolve()
    if not root.is_dir():
        result.errors.append(f"Not a directory: {root}")
        return result

    with db.get_connection(library_id) as conn:
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            if path.suffix.lower() not in EBOOK_EXTENSIONS:
                continue
            try:
                status, _ = import_file(path, conn, library_id)
                if status == "added":
                    result.added += 1
                elif status == "merged":
                    result.merged += 1
                else:
                    result.skipped += 1
            except Exception as exc:
                result.skipped += 1
                result.errors.append(f"{path}: {exc}")

    return result


def import_uploaded_file(
    library_id: str, temp_path: Path, original_name: str
) -> tuple[str, int]:
    suffix = Path(original_name).suffix.lower()
    if suffix not in EBOOK_EXTENSIONS:
        raise ValueError(f"Unsupported file type: {suffix}")
    fallback_path = Path(original_name)
    with db.get_connection(library_id) as conn:
        status, book_id = import_file(
            temp_path, conn, library_id, fallback_path=fallback_path
        )
        if book_id is None:
            raise ValueError("Could not import file")
        return status, book_id
