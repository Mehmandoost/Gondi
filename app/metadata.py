from __future__ import annotations

from typing import Any

import httpx

from app import db
from app.languages import normalize_language

OPEN_LIBRARY_SEARCH = "https://openlibrary.org/search.json"
COVER_BASE = "https://covers.openlibrary.org/b"


def _cover_url(cover_id: int | None, isbn: str | None) -> str | None:
    if cover_id:
        return f"{COVER_BASE}/id/{cover_id}-L.jpg"
    if isbn:
        clean = isbn.replace("-", "").strip()
        if clean:
            return f"{COVER_BASE}/isbn/{clean}-L.jpg"
    return None


def _doc_to_candidate(doc: dict[str, Any]) -> dict[str, Any]:
    authors = doc.get("author_name") or []
    isbn_list = doc.get("isbn") or []
    isbn = isbn_list[0] if isbn_list else None
    cover_id = doc.get("cover_i")
    return {
        "key": doc.get("key"),
        "title": doc.get("title"),
        "authors": authors,
        "publisher": (doc.get("publisher") or [None])[0],
        "published_date": str(doc.get("first_publish_year") or ""),
        "language": normalize_language((doc.get("language") or [None])[0]),
        "isbn": isbn,
        "subjects": doc.get("subject") or [],
        "cover_url": _cover_url(cover_id, isbn),
    }


async def search_candidates(
    *,
    title: str | None = None,
    author: str | None = None,
    isbn: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    params: dict[str, str | int] = {"limit": limit}
    if isbn and isbn.strip():
        params["isbn"] = isbn.replace("-", "").strip()
    else:
        if title:
            params["title"] = title
        if author:
            params["author"] = author

    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(OPEN_LIBRARY_SEARCH, params=params)
        resp.raise_for_status()
        data = resp.json()

    docs = data.get("docs") or []
    return [_doc_to_candidate(d) for d in docs]


async def fetch_cover_bytes(url: str) -> bytes | None:
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        resp = await client.get(url)
        if resp.status_code != 200:
            return None
        return resp.content


async def apply_metadata(
    library_id: str,
    book_id: int,
    candidate: dict[str, Any],
    *,
    replace_fields: bool = True,
) -> dict[str, Any] | None:
    with db.get_connection(library_id) as conn:
        book = db.get_book(conn, book_id)
        if not book:
            return None

        fields: dict[str, Any] = {}
        if replace_fields:
            if candidate.get("title"):
                fields["title"] = candidate["title"]
            if candidate.get("authors"):
                fields["authors"] = candidate["authors"]
            if candidate.get("publisher"):
                fields["publisher"] = candidate["publisher"]
            if candidate.get("published_date"):
                fields["published_date"] = candidate["published_date"]
            if candidate.get("language"):
                fields["language"] = candidate["language"]
            if candidate.get("isbn"):
                fields["isbn"] = candidate["isbn"]
            subjects = candidate.get("subjects")
            if subjects:
                fields["tags"] = subjects[:20]

        updated = db.update_book(conn, book_id, fields) if fields else book

        cover_url = candidate.get("cover_url")
        if cover_url:
            cover_data = await fetch_cover_bytes(cover_url)
            if cover_data:
                cover_path = db.library_covers_dir(library_id) / f"{book_id}.jpg"
                cover_path.write_bytes(cover_data)
                db.set_book_cover(conn, book_id, str(cover_path))

        conn.commit()
        return db.get_book(conn, book_id)
