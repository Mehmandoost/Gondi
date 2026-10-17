from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import db
from app import importer
from app import metadata as metadata_service

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "static"

app = FastAPI(title="Gondi", description="Local book library")

db.init_db()


class LibraryCreateBody(BaseModel):
    name: str


class LibraryPatchBody(BaseModel):
    name: str


class ImportBody(BaseModel):
    path: str


class BookPatch(BaseModel):
    title: str | None = None
    authors: list[str] | None = None
    tags: list[str] | None = None
    description: str | None = None
    publisher: str | None = None
    published_date: str | None = None
    language: str | None = None
    isbn: str | None = None
    series: str | None = None
    series_index: float | None = None


class ApplyMetadataBody(BaseModel):
    candidate: dict[str, Any] = Field(default_factory=dict)


def _require_library(library_id: str) -> dict[str, str]:
    lib = db.get_library(library_id)
    if not lib:
        raise HTTPException(status_code=404, detail="Library not found")
    return lib


@app.get("/api/libraries")
def list_libraries() -> list[dict[str, str]]:
    return db.list_libraries()


@app.post("/api/libraries")
def create_library(body: LibraryCreateBody) -> dict[str, str]:
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name is required")
    return db.create_library(name)


@app.patch("/api/libraries/{library_id}")
def patch_library(library_id: str, body: LibraryPatchBody) -> dict[str, str]:
    _require_library(library_id)
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name is required")
    updated = db.rename_library(library_id, name)
    if not updated:
        raise HTTPException(status_code=404, detail="Library not found")
    return updated


@app.delete("/api/libraries/{library_id}")
def remove_library(library_id: str) -> dict[str, bool]:
    _require_library(library_id)
    if not db.delete_library(library_id):
        raise HTTPException(
            status_code=400,
            detail="Cannot delete the last library",
        )
    return {"ok": True}


@app.get("/api/libraries/{library_id}/books")
def list_books(
    library_id: str,
    q: str | None = None,
    author: str | None = None,
    tag: str | None = None,
    series: str | None = None,
    format: str | None = None,
) -> list[dict[str, Any]]:
    _require_library(library_id)
    with db.get_connection(library_id) as conn:
        return db.list_books(
            conn,
            q=q,
            author=author,
            tag=tag,
            series=series,
            format_name=format,
        )


@app.get("/api/libraries/{library_id}/filters")
def get_filters(library_id: str) -> dict[str, list[str]]:
    _require_library(library_id)
    with db.get_connection(library_id) as conn:
        return db.list_filter_options(conn)


@app.get("/api/libraries/{library_id}/books/{book_id}")
def get_book(library_id: str, book_id: int) -> dict[str, Any]:
    _require_library(library_id)
    with db.get_connection(library_id) as conn:
        book = db.get_book(conn, book_id)
    if not book:
        raise HTTPException(status_code=404, detail="Book not found")
    return book


@app.patch("/api/libraries/{library_id}/books/{book_id}")
def patch_book(
    library_id: str, book_id: int, body: BookPatch
) -> dict[str, Any]:
    _require_library(library_id)
    fields = body.model_dump(exclude_unset=True)
    with db.get_connection(library_id) as conn:
        updated = db.update_book(conn, book_id, fields)
        if not updated:
            raise HTTPException(status_code=404, detail="Book not found")
        conn.commit()
        return updated


@app.post("/api/libraries/{library_id}/import")
def import_folder(library_id: str, body: ImportBody) -> dict[str, Any]:
    _require_library(library_id)
    result = importer.import_folder(library_id, body.path)
    return {
        "added": result.added,
        "merged": result.merged,
        "skipped": result.skipped,
        "errors": result.errors,
    }


@app.post("/api/libraries/{library_id}/books/upload")
async def upload_book(
    library_id: str, file: UploadFile = File(...)
) -> dict[str, Any]:
    _require_library(library_id)
    if not file.filename:
        raise HTTPException(status_code=400, detail="Missing filename")
    suffix = Path(file.filename).suffix.lower()
    if suffix not in {".epub", ".pdf", ".mobi", ".azw3"}:
        raise HTTPException(status_code=400, detail="Unsupported format")

    temp_dir = db.DATA_DIR / "tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_path = temp_dir / f"upload-{file.filename}"
    content = await file.read()
    temp_path.write_bytes(content)
    try:
        status, book_id = importer.import_uploaded_file(
            library_id, temp_path, file.filename
        )
    finally:
        temp_path.unlink(missing_ok=True)

    with db.get_connection(library_id) as conn:
        book = db.get_book(conn, book_id)
    return {"status": status, "book": book}


@app.get("/api/libraries/{library_id}/books/{book_id}/metadata-candidates")
async def metadata_candidates(
    library_id: str, book_id: int
) -> list[dict[str, Any]]:
    _require_library(library_id)
    with db.get_connection(library_id) as conn:
        book = db.get_book(conn, book_id)
    if not book:
        raise HTTPException(status_code=404, detail="Book not found")

    author = book["authors"][0] if book["authors"] else None
    return await metadata_service.search_candidates(
        title=book.get("title"),
        author=author,
        isbn=book.get("isbn"),
    )


@app.post("/api/libraries/{library_id}/books/{book_id}/apply-metadata")
async def apply_metadata(
    library_id: str, book_id: int, body: ApplyMetadataBody
) -> dict[str, Any]:
    _require_library(library_id)
    updated = await metadata_service.apply_metadata(
        library_id, book_id, body.candidate
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Book not found")
    return updated


@app.get("/api/libraries/{library_id}/books/{book_id}/cover")
def get_cover(library_id: str, book_id: int):
    _require_library(library_id)
    with db.get_connection(library_id) as conn:
        path = db.get_book_cover_path(conn, book_id)
    if not path or not db.safe_data_path(path, library_id):
        raise HTTPException(status_code=404, detail="Cover not found")
    media = "image/jpeg"
    if path.suffix.lower() == ".png":
        media = "image/png"
    elif path.suffix.lower() == ".gif":
        media = "image/gif"
    elif path.suffix.lower() == ".webp":
        media = "image/webp"
    return FileResponse(
        path,
        media_type=media,
        headers={"Cache-Control": "private, no-cache"},
    )


@app.get("/api/libraries/{library_id}/books/{book_id}/file/{format_name}")
def get_file(library_id: str, book_id: int, format_name: str):
    _require_library(library_id)
    with db.get_connection(library_id) as conn:
        path = db.get_format_file_path(conn, book_id, format_name)
    if not path or not db.safe_data_path(path, library_id):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path, filename=path.name)


if STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
