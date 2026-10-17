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
from app.languages import normalize_language


EBOOK_EXTENSIONS = {".epub", ".pdf", ".mobi", ".azw3"}

_VOLUME_SUFFIX = re.compile(r":\s*Volume\s+(\d+)\s*$", re.IGNORECASE)
_OPF_NS = "{http://www.idpf.org/2007/opf}"


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


def parse_pdf(path: Path, fallback_path: Path | None = None) -> ParsedBook:
    fb_path = fallback_path or path
    fallback = _parse_filename(fb_path)
    try:
        reader = PdfReader(str(path))
        info = reader.metadata
        if not info:
            return fallback
        title = info.title
        author = info.author
        authors = [author.strip()] if author and author.strip() else fallback.authors
        return ParsedBook(
            title=(title.strip() if title else None) or fallback.title,
            authors=authors,
            description=None,
            publisher=info.get("/Producer") if hasattr(info, "get") else None,
        )
    except Exception:
        return fallback


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


def _save_cover(library_id: str, book_id: int, data: bytes, ext: str) -> str:
    ext = ext if ext.startswith(".") else f".{ext}"
    if ext not in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
        ext = ".jpg"
    cover_path = db.library_covers_dir(library_id) / f"{book_id}{ext}"
    cover_path.write_bytes(data)
    return str(cover_path)


def _copy_book_file(
    library_id: str, src: Path, book_id: int, fmt: str
) -> tuple[str, int]:
    dest_dir = db.library_books_dir(library_id) / str(book_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{fmt}{src.suffix.lower()}"
    shutil.copy2(src, dest)
    return str(dest), dest.stat().st_size


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
        fmt_row = conn.execute(
            "SELECT id FROM formats WHERE book_id = ? AND format = ?",
            (book_id, fmt),
        ).fetchone()
        if fmt_row:
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

    file_path, file_size = _copy_book_file(library_id, path, book_id, fmt)
    db.add_format(conn, book_id, fmt, file_path, file_size)

    if parsed.cover_bytes:
        row = conn.execute(
            "SELECT cover_path FROM books WHERE id = ?", (book_id,)
        ).fetchone()
        if not row or not row["cover_path"]:
            cover_path = _save_cover(
                library_id, book_id, parsed.cover_bytes, parsed.cover_ext
            )
            db.set_book_cover(conn, book_id, cover_path)

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
