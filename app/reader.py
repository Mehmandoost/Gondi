from __future__ import annotations

import hashlib
from pathlib import Path

import pymupdf

from app.db import library_dir

READER_FORMAT_ORDER = ("pdf", "epub", "mobi", "azw3")
READER_LAYOUT_WIDTH = 612
READER_LAYOUT_HEIGHT = 792
RENDER_MATRIX = pymupdf.Matrix(2, 2)

_RTL_LANG_PREFIXES = frozenset(
    {"ar", "he", "fa", "ur", "yi", "ku", "ps", "sd", "ug", "dv"}
)


def choose_reader_format(
    available: list[str], requested: str | None = None
) -> str | None:
    by_fmt = {f.lower() for f in available}
    if requested:
        req = requested.lower()
        if req in by_fmt:
            return req
    for fmt in READER_FORMAT_ORDER:
        if fmt in by_fmt:
            return fmt
    return None


def text_direction(language: str | None) -> str:
    if not language or not str(language).strip():
        return "ltr"
    code = str(language).strip().lower().split("-")[0]
    if code in _RTL_LANG_PREFIXES:
        return "rtl"
    return "ltr"


def open_for_reading(path: Path) -> pymupdf.Document:
    doc = pymupdf.open(path)
    if doc.page_count == 0:
        doc.layout(
            width=READER_LAYOUT_WIDTH,
            height=READER_LAYOUT_HEIGHT,
            fontsize=11,
        )
    return doc


def page_count_for_file(path: Path) -> int:
    doc = open_for_reading(path)
    try:
        return doc.page_count
    finally:
        doc.close()


def _cache_dir(library_id: str, path: Path) -> Path:
    st = path.stat()
    material = f"{path.resolve()}:{st.st_mtime_ns}:{st.st_size}"
    digest = hashlib.sha256(material.encode()).hexdigest()[:16]
    return library_dir(library_id) / "reader-cache" / digest


def render_page_png(library_id: str, path: Path, page_num: int) -> Path:
    cache = _cache_dir(library_id, path)
    cache.mkdir(parents=True, exist_ok=True)
    out = cache / f"page-{page_num}.png"
    if out.is_file():
        return out

    doc = open_for_reading(path)
    try:
        if page_num < 1 or page_num > doc.page_count:
            raise ValueError("page out of range")
        page = doc[page_num - 1]
        pix = page.get_pixmap(matrix=RENDER_MATRIX, alpha=False)
        pix.save(str(out))
    finally:
        doc.close()
    return out


def page_words(path: Path, page_num: int) -> dict[str, object]:
    doc = open_for_reading(path)
    try:
        if page_num < 1 or page_num > doc.page_count:
            raise ValueError("page out of range")
        page = doc[page_num - 1]
        rect = page.rect
        w, h = rect.width, rect.height
        if w <= 0 or h <= 0:
            return {"words": []}
        words: list[dict[str, float | str]] = []
        for item in page.get_text("words"):
            x0, y0, x1, y1, text, *_ = item
            if not str(text).strip():
                continue
            words.append(
                {
                    "text": str(text),
                    "x0": x0 / w,
                    "y0": y0 / h,
                    "x1": x1 / w,
                    "y1": y1 / h,
                }
            )
        return {"words": words}
    finally:
        doc.close()


def normalize_rects(rects: list[dict[str, float]]) -> list[dict[str, float]]:
    out: list[dict[str, float]] = []
    for rect in rects:
        x0 = float(rect["x0"])
        y0 = float(rect["y0"])
        x1 = float(rect["x1"])
        y1 = float(rect["y1"])
        if x1 < x0:
            x0, x1 = x1, x0
        if y1 < y0:
            y0, y1 = y1, y0
        out.append(
            {
                "x0": max(0.0, min(1.0, x0)),
                "y0": max(0.0, min(1.0, y0)),
                "x1": max(0.0, min(1.0, x1)),
                "y1": max(0.0, min(1.0, y1)),
            }
        )
    return out


