# Gondi

A local book library: import ebooks from a folder, search and filter your catalog, edit metadata, and enrich records from [Open Library](https://openlibrary.org/).

Books and the SQLite database live in `data/` next to the project. The server binds to localhost only.

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

- **Import folder** — enter an absolute path on this machine; EPUB and PDF metadata are read when available, with filename fallback for other types.
- **Upload file** — add a single `.epub`, `.pdf`, `.mobi`, or `.azw3` file.
- **Search** — matches title, author, tags, series, and description (SQLite FTS5).
- **Fetch metadata** — on a book’s detail panel, pick a match from Open Library to fill fields and download a cover.
