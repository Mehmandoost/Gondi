# Gondi

A local book library: import ebooks from a folder, search and filter your catalog, edit metadata, and enrich records from [Open Library](https://openlibrary.org/).

Books and catalogs live in `data/` next to the project. You can keep multiple libraries; each has its own SQLite database, `books/`, and `covers/` under `data/libraries/<id>/`. The list of libraries is in `data/libraries.json`. The server binds to localhost only.

## Requirements

- Python 3.11+

## Setup

```bash
cd /path/to/gondi
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
source .venv/bin/activate
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000) in your browser.

## Usage

- **Libraries** — switch, create, rename, or delete libraries from the header (the last library cannot be deleted).
- **Import folder** — enter an absolute path on this machine; EPUB and PDF metadata are read when available, with filename fallback for other types.
- **Upload file** — add a single `.epub`, `.pdf`, `.mobi`, or `.azw3` file.
- **Book detail** — add or remove format files on a book, or delete the book entirely (removing the last file deletes the book).
- **Search** — matches title, author, tags, series, and description (SQLite FTS5).
- **Fetch metadata** — on a book’s detail panel, pick a match from Open Library to fill fields and download a cover.
