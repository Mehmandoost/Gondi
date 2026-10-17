from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import db
from app import importer
from app import metadata as metadata_service
from app import reader as reader_service

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


class ReaderProgressBody(BaseModel):
    format: str
    page: int = Field(ge=1)


class HighlightRect(BaseModel):
    x0: float
    y0: float
    x1: float
    y1: float


class HighlightCreateBody(BaseModel):
    format: str
    page: int = Field(ge=1)
    quote: str
    rects: list[HighlightRect] = Field(min_length=1)
    note: str | None = None


class HighlightPatchBody(BaseModel):
    note: str | None = None


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


@app.delete("/api/libraries/{library_id}/books/{book_id}")
def delete_book_endpoint(library_id: str, book_id: int) -> dict[str, bool]:
    _require_library(library_id)
    with db.get_connection(library_id) as conn:
        if not db.delete_book(conn, library_id, book_id):
            raise HTTPException(status_code=404, detail="Book not found")
        conn.commit()
    return {"deleted": True}


@app.patch("/api/libraries/{library_id}/books/{book_id}")
def patch_book(
    library_id: str, book_id: int, body: BookPatch
) -> dict[str, Any]:
    _require_library(library_id)
    fields = body.model_dump(exclude_unset=True)
    with db.get_connection(library_id) as conn:
        updated = db.update_book(conn, book_id, fields, library_id=library_id)
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


@app.post("/api/libraries/{library_id}/books/{book_id}/files")
async def add_book_file(
    library_id: str, book_id: int, file: UploadFile = File(...)
) -> dict[str, Any]:
    _require_library(library_id)
    if not file.filename:
        raise HTTPException(status_code=400, detail="Missing filename")
    suffix = Path(file.filename).suffix.lower()
    if suffix not in {".epub", ".pdf", ".mobi", ".azw3"}:
        raise HTTPException(status_code=400, detail="Unsupported format")
    fmt = importer.format_from_path(Path(file.filename))
    if not fmt:
        raise HTTPException(status_code=400, detail="Unsupported format")

    temp_dir = db.DATA_DIR / "tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_path = temp_dir / f"upload-{file.filename}"
    content = await file.read()
    temp_path.write_bytes(content)
    try:
        with db.get_connection(library_id) as conn:
            if not db.get_book(conn, book_id):
                raise HTTPException(status_code=404, detail="Book not found")
            old_row = conn.execute(
                "SELECT file_path FROM formats WHERE book_id = ? AND format = ?",
                (book_id, fmt),
            ).fetchone()
            had_format = old_row is not None
            old_stored = old_row["file_path"] if old_row else None
            if had_format:
                db.delete_reader_data_for_format(conn, book_id, fmt)

            file_path, file_size = importer._copy_book_file(
                library_id, conn, temp_path, book_id, fmt
            )
            db.add_format(conn, book_id, fmt, file_path, file_size)

            if old_stored and old_stored != file_path:
                parent = db.unlink_format_file_on_disk(
                    library_id, book_id, fmt, old_stored
                )
                if parent:
                    db.prune_book_storage(library_id, parent)

            conn.commit()
            book = db.get_book(conn, book_id)
    finally:
        temp_path.unlink(missing_ok=True)

    return {"status": "replaced" if had_format else "added", "book": book}


@app.delete("/api/libraries/{library_id}/books/{book_id}/files/{format_name}")
def remove_book_file(
    library_id: str, book_id: int, format_name: str
) -> dict[str, Any]:
    _require_library(library_id)
    with db.get_connection(library_id) as conn:
        if not db.get_book(conn, book_id):
            raise HTTPException(status_code=404, detail="Book not found")
        stored = db.remove_format(conn, book_id, format_name)
        if stored is None:
            raise HTTPException(status_code=404, detail="Format not found")

        parent = db.unlink_format_file_on_disk(
            library_id, book_id, format_name, stored
        )
        if parent:
            db.prune_book_storage(library_id, parent)

        remaining = conn.execute(
            "SELECT COUNT(*) AS n FROM formats WHERE book_id = ?",
            (book_id,),
        ).fetchone()["n"]
        if remaining == 0:
            db.delete_book(conn, library_id, book_id)
            conn.commit()
            return {"book": None, "deleted": True}

        conn.commit()
        return {"book": db.get_book(conn, book_id), "deleted": False}


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


@app.get("/api/libraries/{library_id}/books/{book_id}/reader")
def reader_info(
    library_id: str, book_id: int, format: str | None = None
) -> dict[str, Any]:
    _require_library(library_id)
    with db.get_connection(library_id) as conn:
        book = db.get_book(conn, book_id)
        if not book:
            raise HTTPException(status_code=404, detail="Book not found")
        formats = db.list_reader_formats(conn, book_id)
        if not formats:
            raise HTTPException(status_code=404, detail="No readable format")
        chosen = reader_service.choose_reader_format(formats, format)
        if not chosen:
            raise HTTPException(status_code=400, detail="Invalid format")
        path = db.get_format_file_path(conn, library_id, book_id, chosen)
        if not path:
            raise HTTPException(status_code=404, detail="File not found")
        try:
            page_count = reader_service.page_count_for_file(path)
        except Exception as exc:
            raise HTTPException(
                status_code=422, detail=f"Cannot open book: {exc}"
            ) from exc
        saved = db.get_reading_progress(conn, book_id, chosen)
        page = saved if saved and 1 <= saved <= page_count else 1
        return {
            "title": book["title"],
            "language": book.get("language"),
            "direction": reader_service.text_direction(book.get("language")),
            "formats": formats,
            "format": chosen,
            "page_count": page_count,
            "page": page,
        }


@app.get("/api/libraries/{library_id}/books/{book_id}/reader/pages/{page}")
def reader_page_image(library_id: str, book_id: int, page: int, format: str):
    _require_library(library_id)
    if page < 1:
        raise HTTPException(status_code=400, detail="Invalid page")
    with db.get_connection(library_id) as conn:
        if not db.get_book(conn, book_id):
            raise HTTPException(status_code=404, detail="Book not found")
        path = db.get_format_file_path(conn, library_id, book_id, format)
    if not path or not db.safe_data_path(path, library_id):
        raise HTTPException(status_code=404, detail="File not found")
    try:
        png_path = reader_service.render_page_png(library_id, path, page)
    except ValueError:
        raise HTTPException(status_code=404, detail="Page not found")
    except Exception as exc:
        raise HTTPException(
            status_code=422, detail=f"Cannot render page: {exc}"
        ) from exc
    if not db.safe_data_path(png_path, library_id):
        raise HTTPException(status_code=500, detail="Invalid cache path")
    return FileResponse(png_path, media_type="image/png")


@app.get("/api/libraries/{library_id}/books/{book_id}/reader/pages/{page}/words")
def reader_page_words(
    library_id: str, book_id: int, page: int, format: str
) -> dict[str, Any]:
    _require_library(library_id)
    if page < 1:
        raise HTTPException(status_code=400, detail="Invalid page")
    with db.get_connection(library_id) as conn:
        book = db.get_book(conn, book_id)
        if not book:
            raise HTTPException(status_code=404, detail="Book not found")
        path = db.get_format_file_path(conn, library_id, book_id, format)
    if not path or not db.safe_data_path(path, library_id):
        raise HTTPException(status_code=404, detail="File not found")
    try:
        data = reader_service.page_words(path, page)
    except ValueError:
        raise HTTPException(status_code=404, detail="Page not found")
    except Exception as exc:
        raise HTTPException(
            status_code=422, detail=f"Cannot read page: {exc}"
        ) from exc
    return {
        "words": data["words"],
        "direction": reader_service.text_direction(book.get("language")),
    }


@app.put("/api/libraries/{library_id}/books/{book_id}/reader/progress")
def reader_save_progress(
    library_id: str, book_id: int, body: ReaderProgressBody
) -> dict[str, bool]:
    _require_library(library_id)
    with db.get_connection(library_id) as conn:
        if not db.get_book(conn, book_id):
            raise HTTPException(status_code=404, detail="Book not found")
        formats = db.list_reader_formats(conn, book_id)
        if body.format.lower() not in formats:
            raise HTTPException(status_code=400, detail="Invalid format")
        db.set_reading_progress(conn, book_id, body.format, body.page)
        conn.commit()
    return {"ok": True}


@app.get("/api/libraries/{library_id}/books/{book_id}/highlights")
def list_highlights_endpoint(
    library_id: str,
    book_id: int,
    format: str,
    page: int | None = None,
) -> list[dict[str, Any]]:
    _require_library(library_id)
    with db.get_connection(library_id) as conn:
        if not db.get_book(conn, book_id):
            raise HTTPException(status_code=404, detail="Book not found")
        return db.list_highlights(conn, book_id, format, page=page)


@app.post("/api/libraries/{library_id}/books/{book_id}/highlights")
def create_highlight_endpoint(
    library_id: str, book_id: int, body: HighlightCreateBody
) -> dict[str, Any]:
    _require_library(library_id)
    with db.get_connection(library_id) as conn:
        if not db.get_book(conn, book_id):
            raise HTTPException(status_code=404, detail="Book not found")
        formats = db.list_reader_formats(conn, book_id)
        if body.format.lower() not in formats:
            raise HTTPException(status_code=400, detail="Invalid format")
        try:
            created = db.create_highlight(
                conn,
                book_id,
                body.format,
                body.page,
                body.quote,
                [r.model_dump() for r in body.rects],
                body.note,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        conn.commit()
        return created


@app.patch("/api/libraries/{library_id}/books/{book_id}/highlights/{highlight_id}")
def patch_highlight_endpoint(
    library_id: str,
    book_id: int,
    highlight_id: int,
    body: HighlightPatchBody,
) -> dict[str, Any]:
    _require_library(library_id)
    with db.get_connection(library_id) as conn:
        existing = db.get_highlight(conn, highlight_id)
        if not existing or existing["book_id"] != book_id:
            raise HTTPException(status_code=404, detail="Highlight not found")
        updated = db.update_highlight_note(conn, highlight_id, body.note)
        conn.commit()
        return updated


@app.delete("/api/libraries/{library_id}/books/{book_id}/highlights/{highlight_id}")
def delete_highlight_endpoint(
    library_id: str, book_id: int, highlight_id: int
) -> dict[str, bool]:
    _require_library(library_id)
    with db.get_connection(library_id) as conn:
        existing = db.get_highlight(conn, highlight_id)
        if not existing or existing["book_id"] != book_id:
            raise HTTPException(status_code=404, detail="Highlight not found")
        db.delete_highlight(conn, highlight_id)
        conn.commit()
    return {"ok": True}


@app.get("/api/libraries/{library_id}/books/{book_id}/file/{format_name}")
def get_file(library_id: str, book_id: int, format_name: str):
    _require_library(library_id)
    with db.get_connection(library_id) as conn:
        path = db.get_format_file_path(conn, library_id, book_id, format_name)
    if not path or not db.safe_data_path(path, library_id):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path, filename=path.name)


if STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
