import json
import re
import shutil
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from app.languages import normalize_language

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
LIBRARIES_DIR = DATA_DIR / "libraries"
LIBRARIES_JSON = DATA_DIR / "libraries.json"

SUPPORTED_FORMATS = {"epub", "pdf", "mobi", "azw3"}
READER_FORMATS = ("pdf", "epub", "mobi", "azw3")

_PATH_UNSAFE = re.compile(r'[/\\:*?"<>|\x00-\x1f]')
_MAX_PATH_COMPONENT = 120


def ensure_data_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LIBRARIES_DIR.mkdir(parents=True, exist_ok=True)


def _load_registry() -> list[dict[str, str]]:
    if not LIBRARIES_JSON.is_file():
        return []
    try:
        data = json.loads(LIBRARIES_JSON.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [{"id": str(x["id"]), "name": str(x["name"])} for x in data]
    except (json.JSONDecodeError, KeyError, TypeError):
        pass
    return []


def _save_registry(libraries: list[dict[str, str]]) -> None:
    ensure_data_dirs()
    LIBRARIES_JSON.write_text(
        json.dumps(libraries, indent=2) + "\n", encoding="utf-8"
    )


def _slug_from_name(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "library"


def _unique_library_id(name: str, existing_ids: set[str]) -> str:
    base = _slug_from_name(name)
    if base not in existing_ids:
        return base
    for _ in range(100):
        candidate = f"{base}-{uuid.uuid4().hex[:6]}"
        if candidate not in existing_ids:
            return candidate
    return f"library-{uuid.uuid4().hex[:8]}"


def library_dir(library_id: str) -> Path:
    return LIBRARIES_DIR / library_id


def library_db_path(library_id: str) -> Path:
    return library_dir(library_id) / "library.db"


def library_books_dir(library_id: str) -> Path:
    return library_dir(library_id) / "books"


def library_covers_dir(library_id: str) -> Path:
    return library_dir(library_id) / "covers"


def sanitize_path_component(name: str | None) -> str:
    if not name or not str(name).strip():
        return "Unknown"
    text = _PATH_UNSAFE.sub(" ", str(name).strip())
    text = re.sub(r"\s+", " ", text).strip().rstrip(". ")
    if not text:
        return "Unknown"
    if len(text) > _MAX_PATH_COMPONENT:
        text = text[:_MAX_PATH_COMPONENT].rstrip(". ")
    return text or "Unknown"


def _path_is_under(path: Path, parent: Path) -> bool:
    try:
        resolved = path.resolve()
        root = parent.resolve()
        prefix = str(root) + "/"
        return str(resolved).startswith(prefix) or resolved == root
    except OSError:
        return False


def _storage_dir_used_by_other_book(
    conn: sqlite3.Connection, book_id: int, dir_path: Path
) -> bool:
    try:
        dir_resolved = dir_path.resolve()
        dir_prefix = str(dir_resolved) + "/"
    except OSError:
        return False
    rows = conn.execute(
        "SELECT book_id, file_path FROM formats WHERE book_id != ?",
        (book_id,),
    ).fetchall()
    for row in rows:
        try:
            other = Path(row["file_path"]).resolve()
        except OSError:
            continue
        if str(other).startswith(dir_prefix) or other == dir_resolved:
            return True
    return False


def book_storage_dir(
    library_id: str,
    conn: sqlite3.Connection,
    book_id: int,
    *,
    title: str | None = None,
    authors: list[str] | None = None,
) -> Path:
    if title is None:
        row = conn.execute("SELECT title FROM books WHERE id = ?", (book_id,)).fetchone()
        title = row["title"] if row else "Unknown"
    if authors is None:
        authors = _book_author_names(conn, book_id)
    author_name = sanitize_path_component(authors[0] if authors else "Unknown")
    title_name = sanitize_path_component(title or "Unknown")
    books_root = library_books_dir(library_id)
    base_dir = books_root / author_name / title_name
    if _storage_dir_used_by_other_book(conn, book_id, base_dir):
        title_name = f"{title_name} ({book_id})"
        base_dir = books_root / author_name / title_name
    return base_dir


def book_format_dest_path(
    library_id: str,
    conn: sqlite3.Connection,
    book_id: int,
    suffix: str,
    *,
    title: str | None = None,
    authors: list[str] | None = None,
) -> Path:
    if not suffix.startswith("."):
        suffix = f".{suffix}"
    if title is None:
        row = conn.execute("SELECT title FROM books WHERE id = ?", (book_id,)).fetchone()
        title = row["title"] if row else "Unknown"
    file_name = sanitize_path_component(title or "Unknown") + suffix.lower()
    return book_storage_dir(
        library_id, conn, book_id, title=title, authors=authors
    ) / file_name


def _legacy_format_candidates(
    library_id: str, book_id: int, format_name: str, stored_path: str
) -> list[Path]:
    fmt = format_name.lower()
    books = library_books_dir(library_id)
    candidates = [Path(stored_path)]
    for parent in (books / str(book_id), books / "books" / str(book_id)):
        candidates.append(parent / f"{fmt}.{fmt}")
        candidates.append(parent / f"{fmt}{'.' + fmt}")
    seen: set[str] = set()
    unique: list[Path] = []
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def resolve_format_file_on_disk(
    library_id: str, book_id: int, format_name: str, stored_path: str
) -> Path | None:
    for candidate in _legacy_format_candidates(
        library_id, book_id, format_name, stored_path
    ):
        if candidate.is_file():
            return candidate
    return None


def _prune_empty_parents(path: Path, stop_at: Path) -> None:
    current = path
    try:
        stop = stop_at.resolve()
    except OSError:
        return
    while True:
        try:
            current = current.resolve()
        except OSError:
            break
        if current == stop or not _path_is_under(current, stop):
            break
        if not current.is_dir():
            break
        try:
            next(current.iterdir())
            break
        except StopIteration:
            current.rmdir()
            current = current.parent
        except OSError:
            break


def relocate_book_files(
    conn: sqlite3.Connection, library_id: str, book_id: int
) -> None:
    books_root = library_books_dir(library_id)
    rows = conn.execute(
        "SELECT format, file_path FROM formats WHERE book_id = ?",
        (book_id,),
    ).fetchall()
    old_parents: set[Path] = set()
    for row in rows:
        fmt = row["format"]
        stored = row["file_path"]
        src = resolve_format_file_on_disk(library_id, book_id, fmt, stored)
        if not src:
            continue
        dest = book_format_dest_path(library_id, conn, book_id, src.suffix)
        try:
            if src.resolve() == dest.resolve():
                if str(src) != str(dest):
                    add_format(conn, book_id, fmt, str(dest), src.stat().st_size)
                continue
        except OSError:
            pass
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.is_file() and dest.resolve() != src.resolve():
            dest.unlink()
        shutil.move(str(src), str(dest))
        add_format(conn, book_id, fmt, str(dest), dest.stat().st_size)
        old_parents.add(src.parent)
    for parent in old_parents:
        _prune_empty_parents(parent, books_root)


def migrate_library_book_paths(library_id: str) -> None:
    with get_connection(library_id) as conn:
        book_ids = [
            r["id"] for r in conn.execute("SELECT id FROM books").fetchall()
        ]
        for book_id in book_ids:
            relocate_book_files(conn, library_id, book_id)
        conn.commit()


def ensure_library_dirs(library_id: str) -> None:
    ensure_data_dirs()
    lib = library_dir(library_id)
    lib.mkdir(parents=True, exist_ok=True)
    library_books_dir(library_id).mkdir(parents=True, exist_ok=True)
    library_covers_dir(library_id).mkdir(parents=True, exist_ok=True)


def _migrate_legacy_data() -> None:
    legacy_db = DATA_DIR / "library.db"
    if not legacy_db.is_file():
        return
    if LIBRARIES_DIR.is_dir() and any(LIBRARIES_DIR.iterdir()):
        return
    library_id = "default"
    ensure_library_dirs(library_id)
    dest = library_dir(library_id)
    shutil.move(str(legacy_db), str(dest / "library.db"))
    legacy_books = DATA_DIR / "books"
    if legacy_books.is_dir():
        dest_books = library_books_dir(library_id)
        dest_books.mkdir(parents=True, exist_ok=True)
        for item in legacy_books.iterdir():
            target = dest_books / item.name
            if target.exists():
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()
            shutil.move(str(item), str(target))
        try:
            legacy_books.rmdir()
        except OSError:
            pass
    else:
        library_books_dir(library_id).mkdir(parents=True, exist_ok=True)
    legacy_covers = DATA_DIR / "covers"
    if legacy_covers.is_dir():
        dest_covers = library_covers_dir(library_id)
        dest_covers.mkdir(parents=True, exist_ok=True)
        for item in legacy_covers.iterdir():
            target = dest_covers / item.name
            if target.exists():
                target.unlink()
            shutil.move(str(item), str(target))
        try:
            legacy_covers.rmdir()
        except OSError:
            pass
    else:
        library_covers_dir(library_id).mkdir(parents=True, exist_ok=True)
    _save_registry([{"id": library_id, "name": "Library"}])


def list_libraries() -> list[dict[str, str]]:
    return _load_registry()


def get_library(library_id: str) -> dict[str, str] | None:
    for lib in _load_registry():
        if lib["id"] == library_id:
            return lib
    return None


def create_library(name: str) -> dict[str, str]:
    name = name.strip() or "Library"
    libraries = _load_registry()
    existing_ids = {lib["id"] for lib in libraries}
    library_id = _unique_library_id(name, existing_ids)
    lib_path = library_dir(library_id)
    if lib_path.exists():
        shutil.rmtree(lib_path)
    ensure_library_dirs(library_id)
    libraries.append({"id": library_id, "name": name})
    _save_registry(libraries)
    init_library_schema(library_id)
    return {"id": library_id, "name": name}


def rename_library(library_id: str, name: str) -> dict[str, str] | None:
    name = name.strip()
    if not name:
        return None
    libraries = _load_registry()
    for lib in libraries:
        if lib["id"] == library_id:
            lib["name"] = name
            _save_registry(libraries)
            return {"id": library_id, "name": name}
    return None


def delete_library(library_id: str) -> bool:
    libraries = _load_registry()
    if len(libraries) <= 1:
        return False
    if not any(lib["id"] == library_id for lib in libraries):
        return False
    libraries = [lib for lib in libraries if lib["id"] != library_id]
    _save_registry(libraries)
    lib_path = library_dir(library_id)
    if lib_path.is_dir():
        shutil.rmtree(lib_path)
    return True


def init_libraries() -> None:
    ensure_data_dirs()
    _migrate_legacy_data()
    libraries = _load_registry()
    if not libraries:
        create_library("Library")
        return
    for lib in libraries:
        ensure_library_dirs(lib["id"])
        init_library_schema(lib["id"])


def get_connection(library_id: str) -> sqlite3.Connection:
    if not get_library(library_id):
        raise ValueError(f"Unknown library: {library_id}")
    ensure_library_dirs(library_id)
    conn = sqlite3.connect(library_db_path(library_id))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_library_schema(library_id: str) -> None:
    with get_connection(library_id) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS books (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                title_norm TEXT NOT NULL,
                description TEXT,
                publisher TEXT,
                published_date TEXT,
                language TEXT,
                isbn TEXT,
                series TEXT,
                series_index REAL,
                cover_path TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS authors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                name_norm TEXT NOT NULL UNIQUE
            );

            CREATE TABLE IF NOT EXISTS book_authors (
                book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
                author_id INTEGER NOT NULL REFERENCES authors(id) ON DELETE CASCADE,
                position INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (book_id, author_id)
            );

            CREATE TABLE IF NOT EXISTS tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                name_norm TEXT NOT NULL UNIQUE
            );

            CREATE TABLE IF NOT EXISTS book_tags (
                book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
                tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
                PRIMARY KEY (book_id, tag_id)
            );

            CREATE TABLE IF NOT EXISTS formats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
                format TEXT NOT NULL,
                file_path TEXT NOT NULL,
                file_size INTEGER NOT NULL DEFAULT 0,
                UNIQUE (book_id, format)
            );

            CREATE TABLE IF NOT EXISTS reading_progress (
                book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
                format TEXT NOT NULL,
                page INTEGER NOT NULL,
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (book_id, format)
            );

            CREATE TABLE IF NOT EXISTS highlights (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
                format TEXT NOT NULL,
                page INTEGER NOT NULL,
                quote TEXT NOT NULL,
                note TEXT,
                rects_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_highlights_book_page
                ON highlights(book_id, format, page);

            CREATE VIRTUAL TABLE IF NOT EXISTS books_fts USING fts5(
                title,
                authors,
                tags,
                series,
                description,
                tokenize='porter unicode61'
            );
            """
        )
        _migrate_fts_if_needed(conn)
        _prune_authors_without_books(conn)
        conn.commit()


def init_db() -> None:
    init_libraries()
    for lib in list_libraries():
        migrate_library_book_paths(lib["id"])


def _migrate_fts_if_needed(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='books_fts'"
    ).fetchone()
    if not row or not row[0]:
        return
    if "content=''" in row[0] or 'content=""' in row[0]:
        conn.execute("DROP TABLE books_fts")
        conn.execute(
            """
            CREATE VIRTUAL TABLE books_fts USING fts5(
                title,
                authors,
                tags,
                series,
                description,
                tokenize='porter unicode61'
            )
            """
        )
        book_ids = [r["id"] for r in conn.execute("SELECT id FROM books").fetchall()]
        for book_id in book_ids:
            refresh_book_fts(conn, book_id)


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    text = value.strip().lower()
    text = re.sub(r"\s+", " ", text)
    return text


def _upsert_author(conn: sqlite3.Connection, name: str) -> int:
    name = name.strip()
    norm = normalize_text(name)
    row = conn.execute(
        "SELECT id FROM authors WHERE name_norm = ?", (norm,)
    ).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO authors (name, name_norm) VALUES (?, ?)",
        (name, norm),
    )
    return cur.lastrowid


def _prune_authors_without_books(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        DELETE FROM authors
        WHERE NOT EXISTS (
            SELECT 1 FROM book_authors ba WHERE ba.author_id = authors.id
        )
        """
    )


def _set_book_authors(
    conn: sqlite3.Connection, book_id: int, author_names: list[str]
) -> None:
    conn.execute("DELETE FROM book_authors WHERE book_id = ?", (book_id,))
    for i, name in enumerate(author_names):
        name = name.strip()
        if not name:
            continue
        author_id = _upsert_author(conn, name)
        conn.execute(
            "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) VALUES (?, ?, ?)",
            (book_id, author_id, i),
        )
    _prune_authors_without_books(conn)


def _upsert_tag(conn: sqlite3.Connection, name: str) -> int:
    name = name.strip()
    norm = normalize_text(name)
    row = conn.execute("SELECT id FROM tags WHERE name_norm = ?", (norm,)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO tags (name, name_norm) VALUES (?, ?)",
        (name, norm),
    )
    return cur.lastrowid


def _set_book_tags(conn: sqlite3.Connection, book_id: int, tag_names: list[str]) -> None:
    conn.execute("DELETE FROM book_tags WHERE book_id = ?", (book_id,))
    for name in tag_names:
        name = name.strip()
        if not name:
            continue
        tag_id = _upsert_tag(conn, name)
        conn.execute(
            "INSERT OR IGNORE INTO book_tags (book_id, tag_id) VALUES (?, ?)",
            (book_id, tag_id),
        )


def _book_author_names(conn: sqlite3.Connection, book_id: int) -> list[str]:
    rows = conn.execute(
        """
        SELECT a.name FROM authors a
        JOIN book_authors ba ON ba.author_id = a.id
        WHERE ba.book_id = ?
        ORDER BY ba.position
        """,
        (book_id,),
    ).fetchall()
    return [r["name"] for r in rows]


def _book_tag_names(conn: sqlite3.Connection, book_id: int) -> list[str]:
    rows = conn.execute(
        """
        SELECT t.name FROM tags t
        JOIN book_tags bt ON bt.tag_id = t.id
        WHERE bt.book_id = ?
        ORDER BY t.name
        """,
        (book_id,),
    ).fetchall()
    return [r["name"] for r in rows]


def _book_formats(conn: sqlite3.Connection, book_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT format, file_path, file_size FROM formats WHERE book_id = ? ORDER BY format",
        (book_id,),
    ).fetchall()
    return [
        {"format": r["format"], "file_size": r["file_size"]} for r in rows
    ]


def refresh_book_fts(conn: sqlite3.Connection, book_id: int) -> None:
    row = conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
    if not row:
        return
    authors = ", ".join(_book_author_names(conn, book_id))
    tags = ", ".join(_book_tag_names(conn, book_id))
    conn.execute("DELETE FROM books_fts WHERE rowid = ?", (book_id,))
    conn.execute(
        """
        INSERT INTO books_fts (rowid, title, authors, tags, series, description)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            book_id,
            row["title"] or "",
            authors,
            tags,
            row["series"] or "",
            row["description"] or "",
        ),
    )


def book_to_dict(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    book_id = row["id"]
    return {
        "id": book_id,
        "title": row["title"],
        "authors": _book_author_names(conn, book_id),
        "tags": _book_tag_names(conn, book_id),
        "description": row["description"],
        "publisher": row["publisher"],
        "published_date": row["published_date"],
        "language": normalize_language(row["language"]),
        "isbn": row["isbn"],
        "series": row["series"],
        "series_index": row["series_index"],
        "has_cover": bool(row["cover_path"]),
        "formats": _book_formats(conn, book_id),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def find_book_by_title_authors(
    conn: sqlite3.Connection, title: str, authors: list[str]
) -> int | None:
    title_norm = normalize_text(title)
    if not title_norm:
        return None
    author_norms = sorted(normalize_text(a) for a in authors if a.strip())
    rows = conn.execute(
        "SELECT id FROM books WHERE title_norm = ?", (title_norm,)
    ).fetchall()
    for row in rows:
        book_id = row["id"]
        existing = sorted(
            normalize_text(a) for a in _book_author_names(conn, book_id) if a.strip()
        )
        if existing == author_norms:
            return book_id
    return None


def create_book(
    conn: sqlite3.Connection,
    *,
    title: str,
    authors: list[str],
    tags: list[str] | None = None,
    description: str | None = None,
    publisher: str | None = None,
    published_date: str | None = None,
    language: str | None = None,
    isbn: str | None = None,
    series: str | None = None,
    series_index: float | None = None,
) -> int:
    title = title.strip() or "Unknown"
    language = normalize_language(language)
    if series is not None and not str(series).strip():
        series = None
        series_index = None
    cur = conn.execute(
        """
        INSERT INTO books (
            title, title_norm, description, publisher, published_date,
            language, isbn, series, series_index
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            title,
            normalize_text(title),
            description,
            publisher,
            published_date,
            language,
            isbn,
            series,
            series_index,
        ),
    )
    book_id = cur.lastrowid
    _set_book_authors(conn, book_id, authors or ["Unknown"])
    if tags:
        _set_book_tags(conn, book_id, tags)
    refresh_book_fts(conn, book_id)
    return book_id


def update_book(
    conn: sqlite3.Connection,
    book_id: int,
    fields: dict[str, Any],
    *,
    library_id: str | None = None,
) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
    if not row:
        return None

    relocate = library_id and (
        "title" in fields
        or ("authors" in fields and fields["authors"] is not None)
    )

    scalar_map = {
        "title": "title",
        "description": "description",
        "publisher": "publisher",
        "published_date": "published_date",
        "language": "language",
        "isbn": "isbn",
        "series": "series",
        "series_index": "series_index",
    }
    updates: list[str] = []
    values: list[Any] = []
    for key, col in scalar_map.items():
        if key in fields:
            updates.append(f"{col} = ?")
            val = fields[key]
            if key == "language":
                val = normalize_language(val)
            if key == "series" and isinstance(val, str) and not val.strip():
                val = None
            values.append(val)
            if key == "title":
                updates.append("title_norm = ?")
                values.append(normalize_text(val or ""))

    if updates:
        updates.append("updated_at = datetime('now')")
        conn.execute(
            f"UPDATE books SET {', '.join(updates)} WHERE id = ?",
            (*values, book_id),
        )

    if "authors" in fields and fields["authors"] is not None:
        _set_book_authors(conn, book_id, fields["authors"])
    if "tags" in fields and fields["tags"] is not None:
        _set_book_tags(conn, book_id, fields["tags"])

    if relocate:
        relocate_book_files(conn, library_id, book_id)

    refresh_book_fts(conn, book_id)
    updated = conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
    return book_to_dict(conn, updated)


def set_book_cover(conn: sqlite3.Connection, book_id: int, cover_path: str) -> None:
    conn.execute(
        "UPDATE books SET cover_path = ?, updated_at = datetime('now') WHERE id = ?",
        (cover_path, book_id),
    )


def add_format(
    conn: sqlite3.Connection,
    book_id: int,
    format_name: str,
    file_path: str,
    file_size: int,
) -> None:
    format_name = format_name.lower()
    conn.execute(
        """
        INSERT INTO formats (book_id, format, file_path, file_size)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(book_id, format) DO UPDATE SET
            file_path = excluded.file_path,
            file_size = excluded.file_size
        """,
        (book_id, format_name, file_path, file_size),
    )
    conn.execute(
        "UPDATE books SET updated_at = datetime('now') WHERE id = ?",
        (book_id,),
    )


def prune_book_storage(library_id: str, path: Path) -> None:
    _prune_empty_parents(path, library_books_dir(library_id))


def unlink_format_file_on_disk(
    library_id: str,
    book_id: int,
    format_name: str,
    stored_path: str,
) -> Path | None:
    """Delete a format file under the library data dir; return its parent for pruning."""
    path = resolve_format_file_on_disk(
        library_id, book_id, format_name, stored_path
    )
    if not path or not path.is_file():
        return None
    if not safe_data_path(path, library_id):
        return None
    parent = path.parent
    path.unlink(missing_ok=True)
    return parent


def remove_format(
    conn: sqlite3.Connection, book_id: int, format_name: str
) -> str | None:
    """Remove a format row; return stored file_path, or None if missing."""
    fmt = format_name.lower()
    row = conn.execute(
        "SELECT file_path FROM formats WHERE book_id = ? AND format = ?",
        (book_id, fmt),
    ).fetchone()
    if not row:
        return None
    conn.execute(
        "DELETE FROM formats WHERE book_id = ? AND format = ?",
        (book_id, fmt),
    )
    delete_reader_data_for_format(conn, book_id, fmt)
    conn.execute(
        "UPDATE books SET updated_at = datetime('now') WHERE id = ?",
        (book_id,),
    )
    return row["file_path"]


def delete_reader_data_for_format(
    conn: sqlite3.Connection, book_id: int, format_name: str
) -> None:
    fmt = format_name.lower()
    conn.execute(
        "DELETE FROM highlights WHERE book_id = ? AND format = ?",
        (book_id, fmt),
    )
    conn.execute(
        "DELETE FROM reading_progress WHERE book_id = ? AND format = ?",
        (book_id, fmt),
    )


def list_reader_formats(conn: sqlite3.Connection, book_id: int) -> list[str]:
    rows = conn.execute(
        """
        SELECT format FROM formats
        WHERE book_id = ? AND format IN ('pdf', 'epub', 'mobi', 'azw3')
        ORDER BY format
        """,
        (book_id,),
    ).fetchall()
    available = {r["format"] for r in rows}
    return [fmt for fmt in READER_FORMATS if fmt in available]


def get_reading_progress(
    conn: sqlite3.Connection, book_id: int, format_name: str
) -> int | None:
    row = conn.execute(
        """
        SELECT page FROM reading_progress
        WHERE book_id = ? AND format = ?
        """,
        (book_id, format_name.lower()),
    ).fetchone()
    return int(row["page"]) if row else None


def set_reading_progress(
    conn: sqlite3.Connection, book_id: int, format_name: str, page: int
) -> None:
    fmt = format_name.lower()
    conn.execute(
        """
        INSERT INTO reading_progress (book_id, format, page, updated_at)
        VALUES (?, ?, ?, datetime('now'))
        ON CONFLICT(book_id, format) DO UPDATE SET
            page = excluded.page,
            updated_at = datetime('now')
        """,
        (book_id, fmt, page),
    )


def list_highlights(
    conn: sqlite3.Connection,
    book_id: int,
    format_name: str,
    *,
    page: int | None = None,
) -> list[dict[str, Any]]:
    fmt = format_name.lower()
    if page is not None:
        rows = conn.execute(
            """
            SELECT * FROM highlights
            WHERE book_id = ? AND format = ? AND page = ?
            ORDER BY id
            """,
            (book_id, fmt, page),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT * FROM highlights
            WHERE book_id = ? AND format = ?
            ORDER BY page, id
            """,
            (book_id, fmt),
        ).fetchall()
    return [_highlight_to_dict(r) for r in rows]


def _highlight_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "book_id": row["book_id"],
        "format": row["format"],
        "page": row["page"],
        "quote": row["quote"],
        "note": row["note"],
        "rects": json.loads(row["rects_json"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def create_highlight(
    conn: sqlite3.Connection,
    book_id: int,
    format_name: str,
    page: int,
    quote: str,
    rects: list[dict[str, float]],
    note: str | None = None,
) -> dict[str, Any]:
    from app.reader import normalize_rects

    fmt = format_name.lower()
    quote_text = quote.strip()
    if not quote_text:
        raise ValueError("quote is required")
    if not rects:
        raise ValueError("rects are required")
    cur = conn.execute(
        """
        INSERT INTO highlights (book_id, format, page, quote, note, rects_json)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (book_id, fmt, page, quote_text, note, json.dumps(normalize_rects(rects))),
    )
    row = conn.execute(
        "SELECT * FROM highlights WHERE id = ?", (cur.lastrowid,)
    ).fetchone()
    return _highlight_to_dict(row)


def update_highlight_note(
    conn: sqlite3.Connection, highlight_id: int, note: str | None
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT id FROM highlights WHERE id = ?", (highlight_id,)
    ).fetchone()
    if not row:
        return None
    conn.execute(
        """
        UPDATE highlights
        SET note = ?, updated_at = datetime('now')
        WHERE id = ?
        """,
        (note, highlight_id),
    )
    updated = conn.execute(
        "SELECT * FROM highlights WHERE id = ?", (highlight_id,)
    ).fetchone()
    return _highlight_to_dict(updated)


def delete_highlight(conn: sqlite3.Connection, highlight_id: int) -> bool:
    cur = conn.execute("DELETE FROM highlights WHERE id = ?", (highlight_id,))
    return cur.rowcount > 0


def get_highlight(
    conn: sqlite3.Connection, highlight_id: int
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM highlights WHERE id = ?", (highlight_id,)
    ).fetchone()
    if not row:
        return None
    return _highlight_to_dict(row)


def delete_book(conn: sqlite3.Connection, library_id: str, book_id: int) -> bool:
    row = conn.execute("SELECT cover_path FROM books WHERE id = ?", (book_id,)).fetchone()
    if not row:
        return False

    books_root = library_books_dir(library_id)
    parents_to_prune: set[Path] = set()

    format_rows = conn.execute(
        "SELECT format, file_path FROM formats WHERE book_id = ?",
        (book_id,),
    ).fetchall()
    for fmt_row in format_rows:
        parent = unlink_format_file_on_disk(
            library_id,
            book_id,
            fmt_row["format"],
            fmt_row["file_path"],
        )
        if parent:
            parents_to_prune.add(parent)

    if row["cover_path"]:
        cover = Path(row["cover_path"])
        if cover.is_file() and safe_data_path(cover, library_id):
            cover.unlink(missing_ok=True)

    conn.execute("DELETE FROM books_fts WHERE rowid = ?", (book_id,))
    conn.execute("DELETE FROM books WHERE id = ?", (book_id,))

    for parent in parents_to_prune:
        _prune_empty_parents(parent, books_root)

    return True


def get_book(conn: sqlite3.Connection, book_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
    if not row:
        return None
    return book_to_dict(conn, row)


def get_book_cover_path(conn: sqlite3.Connection, book_id: int) -> Path | None:
    row = conn.execute(
        "SELECT cover_path FROM books WHERE id = ?", (book_id,)
    ).fetchone()
    if not row or not row["cover_path"]:
        return None
    path = Path(row["cover_path"])
    if path.is_file():
        return path
    return None


def get_format_file_path(
    conn: sqlite3.Connection,
    library_id: str,
    book_id: int,
    format_name: str,
) -> Path | None:
    row = conn.execute(
        "SELECT file_path FROM formats WHERE book_id = ? AND format = ?",
        (book_id, format_name.lower()),
    ).fetchone()
    if not row:
        return None
    path = Path(row["file_path"])
    if path.is_file():
        return path
    return resolve_format_file_on_disk(
        library_id, book_id, format_name, row["file_path"]
    )


def list_books(
    conn: sqlite3.Connection,
    *,
    q: str | None = None,
    author: str | None = None,
    tag: str | None = None,
    series: str | None = None,
    format_name: str | None = None,
) -> list[dict[str, Any]]:
    params: list[Any] = []
    joins: list[str] = []
    where: list[str] = []

    if q and q.strip():
        term = q.strip()
        # FTS5 query: wrap words for prefix search
        fts_query = " ".join(f'"{w}"*' for w in term.split() if w) or term
        where.append("b.id IN (SELECT rowid FROM books_fts WHERE books_fts MATCH ?)")
        params.append(fts_query)

    if author:
        joins.append("JOIN book_authors ba ON ba.book_id = b.id")
        joins.append("JOIN authors a ON a.id = ba.author_id")
        where.append("a.name_norm = ?")
        params.append(normalize_text(author))

    if tag:
        joins.append("JOIN book_tags bt ON bt.book_id = b.id")
        joins.append("JOIN tags t ON t.id = bt.tag_id")
        where.append("t.name_norm = ?")
        params.append(normalize_text(tag))

    if series:
        where.append("b.series = ?")
        params.append(series)

    if format_name:
        joins.append("JOIN formats f ON f.book_id = b.id")
        where.append("f.format = ?")
        params.append(format_name.lower())

    sql = f"""
        SELECT DISTINCT b.* FROM books b
        {' '.join(joins)}
        {'WHERE ' + ' AND '.join(where) if where else ''}
        ORDER BY b.title COLLATE NOCASE
    """
    rows = conn.execute(sql, params).fetchall()
    return [book_to_dict(conn, r) for r in rows]


def list_filter_options(conn: sqlite3.Connection) -> dict[str, list[str]]:
    authors = [
        r["name"]
        for r in conn.execute(
            """
            SELECT a.name
            FROM authors a
            WHERE EXISTS (
                SELECT 1 FROM book_authors ba WHERE ba.author_id = a.id
            )
            ORDER BY a.name COLLATE NOCASE
            """
        ).fetchall()
    ]
    tags = [
        r["name"]
        for r in conn.execute("SELECT name FROM tags ORDER BY name COLLATE NOCASE").fetchall()
    ]
    series = [
        r["series"]
        for r in conn.execute(
            """
            SELECT series
            FROM books
            WHERE series IS NOT NULL AND trim(series) != ''
            GROUP BY series
            HAVING COUNT(*) > 0
            ORDER BY series COLLATE NOCASE
            """
        ).fetchall()
    ]
    formats = [
        r["format"]
        for r in conn.execute(
            "SELECT DISTINCT format FROM formats ORDER BY format"
        ).fetchall()
    ]
    return {"authors": authors, "tags": tags, "series": series, "formats": formats}


def safe_data_path(path: Path, library_id: str) -> bool:
    try:
        resolved = path.resolve()
        lib_resolved = library_dir(library_id).resolve()
        prefix = str(lib_resolved) + "/"
        return str(resolved).startswith(prefix) or resolved == lib_resolved
    except OSError:
        return False
