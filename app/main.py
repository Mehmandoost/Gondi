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


@app.get("/api/books")
def list_books(
    q: str | None = None,
    author: str | None = None,
    tag: str | None = None,
    series: str | None = None,
    format: str | None = None,
) -> list[dict[str, Any]]:
    with db.get_connection() as conn:
        return db.list_books(
            conn,
            q=q,
            author=author,
            tag=tag,
            series=series,
            format_name=format,
        )


@app.get("/api/filters")
def get_filters() -> dict[str, list[str]]:
    with db.get_connection() as conn:
        return db.list_filter_options(conn)


@app.get("/api/books/{book_id}")
def get_book(book_id: int) -> dict[str, Any]:
    with db.get_connection() as conn:
        book = db.get_book(conn, book_id)
    if not book:
        raise HTTPException(status_code=404, detail="Book not found")
    return book


@app.patch("/api/books/{book_id}")
def patch_book(book_id: int, body: BookPatch) -> dict[str, Any]:
    fields = body.model_dump(exclude_unset=True)
    with db.get_connection() as conn:
        updated = db.update_book(conn, book_id, fields)
        if not updated:
            raise HTTPException(status_code=404, detail="Book not found")
        conn.commit()
        return updated


@app.post("/api/import")
def import_folder(body: ImportBody) -> dict[str, Any]:
    result = importer.import_folder(body.path)
    return {
        "added": result.added,
        "merged": result.merged,
        "skipped": result.skipped,
        "errors": result.errors,
    }


@app.post("/api/books/upload")
async def upload_book(file: UploadFile = File(...)) -> dict[str, Any]:
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
        status, book_id = importer.import_uploaded_file(temp_path, file.filename)
    finally:
        temp_path.unlink(missing_ok=True)

    with db.get_connection() as conn:
        book = db.get_book(conn, book_id)
    return {"status": status, "book": book}


@app.get("/api/books/{book_id}/metadata-candidates")
async def metadata_candidates(book_id: int) -> list[dict[str, Any]]:
    with db.get_connection() as conn:
        book = db.get_book(conn, book_id)
    if not book:
        raise HTTPException(status_code=404, detail="Book not found")

    author = book["authors"][0] if book["authors"] else None
    return await metadata_service.search_candidates(
        title=book.get("title"),
        author=author,
        isbn=book.get("isbn"),
    )


@app.post("/api/books/{book_id}/apply-metadata")
async def apply_metadata(book_id: int, body: ApplyMetadataBody) -> dict[str, Any]:
    updated = await metadata_service.apply_metadata(book_id, body.candidate)
    if not updated:
        raise HTTPException(status_code=404, detail="Book not found")
    return updated


@app.get("/api/books/{book_id}/cover")
def get_cover(book_id: int):
    with db.get_connection() as conn:
        path = db.get_book_cover_path(conn, book_id)
    if not path or not db.safe_data_path(path):
        raise HTTPException(status_code=404, detail="Cover not found")
    media = "image/jpeg"
    if path.suffix.lower() == ".png":
        media = "image/png"
    elif path.suffix.lower() == ".gif":
        media = "image/gif"
    elif path.suffix.lower() == ".webp":
        media = "image/webp"
    return FileResponse(path, media_type=media)


@app.get("/api/books/{book_id}/file/{format_name}")
def get_file(book_id: int, format_name: str):
    with db.get_connection() as conn:
        path = db.get_format_file_path(conn, book_id, format_name)
    if not path or not db.safe_data_path(path):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path, filename=path.name)


if STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
