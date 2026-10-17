import json
import re
import shutil
import sqlite3
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
LIBRARIES_DIR = DATA_DIR / "libraries"
LIBRARIES_JSON = DATA_DIR / "libraries.json"

SUPPORTED_FORMATS = {"epub", "pdf", "mobi", "azw3"}


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
        shutil.move(str(legacy_books), str(dest / "books"))
    else:
        library_books_dir(library_id).mkdir(parents=True, exist_ok=True)
    legacy_covers = DATA_DIR / "covers"
    if legacy_covers.is_dir():
        shutil.move(str(legacy_covers), str(dest / "covers"))
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
        conn.commit()


def init_db() -> None:
    init_libraries()


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
        "language": row["language"],
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
) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
    if not row:
        return None

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
    conn: sqlite3.Connection, book_id: int, format_name: str
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
    return None


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
        for r in conn.execute("SELECT name FROM authors ORDER BY name COLLATE NOCASE").fetchall()
    ]
    tags = [
        r["name"]
        for r in conn.execute("SELECT name FROM tags ORDER BY name COLLATE NOCASE").fetchall()
    ]
    series = [
        r["series"]
        for r in conn.execute(
            "SELECT DISTINCT series FROM books WHERE series IS NOT NULL AND series != '' ORDER BY series COLLATE NOCASE"
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
