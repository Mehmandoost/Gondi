import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from ebooklib import epub
from pypdf import PdfReader

from app import db


EBOOK_EXTENSIONS = {".epub", ".pdf", ".mobi", ".azw3"}


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


def parse_epub(path: Path) -> ParsedBook:
    book = epub.read_epub(str(path))
    meta = book.get_metadata("DC", "title")
    title = _first([m[0] for m in meta]) if meta else None
    author_meta = book.get_metadata("DC", "creator")
    authors = [m[0].strip() for m in author_meta if m[0].strip()] if author_meta else []
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

    cover_bytes = None
    cover_ext = ".jpg"
    for item in book.get_items():
        if item.get_type() == epub.ITEM_COVER:
            cover_bytes = item.get_content()
            name = item.get_name() or ""
            if "." in name:
                cover_ext = "." + name.rsplit(".", 1)[-1].lower()
            break

    fallback = _parse_filename(path)
    return ParsedBook(
        title=title or fallback.title,
        authors=authors or fallback.authors,
        description=description,
        publisher=publisher,
        published_date=published_date,
        language=language,
        tags=tags,
        cover_bytes=cover_bytes,
        cover_ext=cover_ext,
    )


def parse_pdf(path: Path) -> ParsedBook:
    fallback = _parse_filename(path)
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


def parse_file(path: Path) -> ParsedBook:
    fmt = format_from_path(path)
    if fmt == "epub":
        try:
            return parse_epub(path)
        except Exception:
            return _parse_filename(path)
    if fmt == "pdf":
        return parse_pdf(path)
    return _parse_filename(path)


def _save_cover(book_id: int, data: bytes, ext: str) -> str:
    ext = ext if ext.startswith(".") else f".{ext}"
    if ext not in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
        ext = ".jpg"
    cover_path = db.COVERS_DIR / f"{book_id}{ext}"
    cover_path.write_bytes(data)
    return str(cover_path)


def _copy_book_file(src: Path, book_id: int, fmt: str) -> tuple[str, int]:
    dest_dir = db.BOOKS_DIR / str(book_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{fmt}{src.suffix.lower()}"
    shutil.copy2(src, dest)
    return str(dest), dest.stat().st_size


def import_file(path: Path, conn) -> tuple[str, int | None]:
    """
    Returns (status, book_id) where status is 'added', 'merged', or 'skipped'.
    """
    path = path.resolve()
    if not path.is_file():
        return ("skipped", None)

    fmt = format_from_path(path)
    if not fmt:
        return ("skipped", None)

    parsed = parse_file(path)
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
        )
        status = "added"

    file_path, file_size = _copy_book_file(path, book_id, fmt)
    db.add_format(conn, book_id, fmt, file_path, file_size)

    if parsed.cover_bytes:
        row = conn.execute(
            "SELECT cover_path FROM books WHERE id = ?", (book_id,)
        ).fetchone()
        if not row or not row["cover_path"]:
            cover_path = _save_cover(book_id, parsed.cover_bytes, parsed.cover_ext)
            db.set_book_cover(conn, book_id, cover_path)

    conn.commit()
    return (status, book_id)


def import_folder(folder: str | Path) -> ImportResult:
    result = ImportResult()
    root = Path(folder).expanduser().resolve()
    if not root.is_dir():
        result.errors.append(f"Not a directory: {root}")
        return result

    with db.get_connection() as conn:
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            if path.suffix.lower() not in EBOOK_EXTENSIONS:
                continue
            try:
                status, _ = import_file(path, conn)
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


def import_uploaded_file(temp_path: Path, original_name: str) -> tuple[str, int]:
    suffix = Path(original_name).suffix.lower()
    if suffix not in EBOOK_EXTENSIONS:
        raise ValueError(f"Unsupported file type: {suffix}")
    with db.get_connection() as conn:
        status, book_id = import_file(temp_path, conn)
        if book_id is None:
            raise ValueError("Could not import file")
        return status, book_id
