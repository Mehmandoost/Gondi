const LIBRARY_STORAGE_KEY = "gondi-library-id";

const state = {
  libraries: [],
  libraryId: null,
  books: [],
  filters: { authors: [], tags: [], series: [], formats: [] },
  query: "",
  active: { author: "", tag: "", series: "", format: "" },
  selectedId: null,
};

const $ = (sel) => document.querySelector(sel);

function apiBase() {
  if (!state.libraryId) throw new Error("No library selected");
  return `/api/libraries/${encodeURIComponent(state.libraryId)}`;
}

function showStatus(message, isError = false) {
  const el = $("#status");
  el.textContent = message;
  el.hidden = !message;
  el.classList.toggle("error", isError);
  if (message && !isError) {
    setTimeout(() => {
      el.hidden = true;
    }, 5000);
  }
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", ...options.headers },
    ...options,
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || res.statusText);
  }
  if (res.status === 204) return null;
  const ct = res.headers.get("content-type") || "";
  if (ct.includes("application/json")) return res.json();
  return res;
}

function resetCatalogView() {
  state.query = "";
  state.active = { author: "", tag: "", series: "", format: "" };
  state.selectedId = null;
  state.books = [];
  const search = $("#search-input");
  if (search) search.value = "";
  const panel = $("#detail-panel");
  if (panel) panel.hidden = true;
}

function buildBooksUrl() {
  const params = new URLSearchParams();
  if (state.query) params.set("q", state.query);
  if (state.active.author) params.set("author", state.active.author);
  if (state.active.tag) params.set("tag", state.active.tag);
  if (state.active.series) params.set("series", state.active.series);
  if (state.active.format) params.set("format", state.active.format);
  const qs = params.toString();
  return `${apiBase()}/books${qs ? `?${qs}` : ""}`;
}

function renderLibrarySelect() {
  const select = $("#library-select");
  if (!select) return;
  select.innerHTML = state.libraries
    .map(
      (lib) =>
        `<option value="${escapeAttr(lib.id)}"${
          lib.id === state.libraryId ? " selected" : ""
        }>${escapeHtml(lib.name)}</option>`
    )
    .join("");
  select.disabled = !state.libraries.length;
}

async function loadLibraries() {
  state.libraries = await api("/api/libraries");
  if (!state.libraries.length) {
    state.libraryId = null;
    renderLibrarySelect();
    return;
  }
  const stored = localStorage.getItem(LIBRARY_STORAGE_KEY);
  if (stored && state.libraries.some((l) => l.id === stored)) {
    state.libraryId = stored;
  } else {
    state.libraryId = state.libraries[0].id;
    localStorage.setItem(LIBRARY_STORAGE_KEY, state.libraryId);
  }
  renderLibrarySelect();
}

async function switchLibrary(libraryId) {
  if (!libraryId || libraryId === state.libraryId) return;
  state.libraryId = libraryId;
  localStorage.setItem(LIBRARY_STORAGE_KEY, libraryId);
  resetCatalogView();
  renderLibrarySelect();
  await loadFilters();
  await loadBooks();
}

async function loadFilters() {
  if (!state.libraryId) return;
  state.filters = await api(`${apiBase()}/filters`);
  renderFilters();
}

async function loadBooks() {
  if (!state.libraryId) {
    state.books = [];
    renderGrid();
    return;
  }
  state.books = await api(buildBooksUrl());
  renderGrid();
}

function renderFilters() {
  const aside = $("#filters");
  const sections = [
    { key: "author", label: "Authors", items: state.filters.authors },
    { key: "tag", label: "Tags", items: state.filters.tags },
    { key: "series", label: "Series", items: state.filters.series },
    { key: "format", label: "Formats", items: state.filters.formats },
  ];

  aside.innerHTML = `
    <h2>Filters</h2>
    <button type="button" class="filter-clear" id="clear-filters">Clear all</button>
    ${sections
      .map(
        (s) => `
      <div class="filter-group">
        <h3>${s.label}</h3>
        <ul>
          ${(s.items || [])
            .slice(0, 50)
            .map(
              (name) => `
            <li>
              <button type="button" class="filter-item${
                state.active[s.key] === name ? " active" : ""
              }" data-filter="${s.key}" data-value="${escapeAttr(name)}">
                ${escapeHtml(name)}
              </button>
            </li>`
            )
            .join("")}
        </ul>
      </div>`
      )
      .join("")}
  `;

  aside.querySelector("#clear-filters").addEventListener("click", () => {
    state.active = { author: "", tag: "", series: "", format: "" };
    loadBooks();
    loadFilters();
  });

  aside.querySelectorAll(".filter-item").forEach((btn) => {
    btn.addEventListener("click", () => {
      const key = btn.dataset.filter;
      const value = btn.dataset.value;
      state.active[key] = state.active[key] === value ? "" : value;
      loadBooks();
      renderFilters();
    });
  });
}

function escapeHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function escapeAttr(str) {
  return escapeHtml(str).replace(/'/g, "&#39;");
}

function coverUrl(book) {
  if (!book.has_cover) return null;
  return `${apiBase()}/books/${book.id}/cover`;
}

function renderGrid() {
  const grid = $("#book-grid");
  const empty = $("#empty-state");
  if (!state.libraryId) {
    grid.innerHTML = "";
    empty.hidden = false;
    empty.textContent = "Create a library to get started.";
    return;
  }
  if (!state.books.length) {
    grid.innerHTML = "";
    empty.hidden = false;
    empty.innerHTML =
      "Your library is empty. Use <strong>Import folder</strong> with a path on this machine, or upload a single ebook file.";
    return;
  }
  empty.hidden = true;
  grid.innerHTML = state.books
    .map((book) => {
      const cover = coverUrl(book);
      const authors = book.authors?.join(", ") || "Unknown";
      const formats = (book.formats || []).map((f) => f.format).join(", ");
      return `
      <article class="book-card${
        state.selectedId === book.id ? " selected" : ""
      }" data-id="${book.id}" tabindex="0">
        <div class="cover">
          ${
            cover
              ? `<img src="${cover}" alt="" loading="lazy" />`
              : `<span class="no-cover">${escapeHtml(book.title.slice(0, 1))}</span>`
          }
        </div>
        <h3>${escapeHtml(book.title)}</h3>
        <p class="meta">${escapeHtml(authors)}</p>
        ${formats ? `<p class="formats">${escapeHtml(formats)}</p>` : ""}
      </article>`;
    })
    .join("");

  grid.querySelectorAll(".book-card").forEach((card) => {
    card.addEventListener("click", () => openDetail(Number(card.dataset.id)));
    card.addEventListener("keydown", (e) => {
      if (e.key === "Enter") openDetail(Number(card.dataset.id));
    });
  });
}

async function openDetail(id) {
  state.selectedId = id;
  renderGrid();
  const book = await api(`${apiBase()}/books/${id}`);
  const panel = $("#detail-panel");
  const content = $("#detail-content");
  panel.hidden = false;

  const formats = (book.formats || [])
    .map(
      (f) =>
        `<a href="${apiBase()}/books/${book.id}/file/${f.format}" download>${escapeHtml(
          f.format
        )}</a>`
    )
    .join(" · ");

  content.innerHTML = `
    <div class="detail-cover">
      ${
        book.has_cover
          ? `<img src="${apiBase()}/books/${book.id}/cover" alt="" />`
          : `<div class="no-cover large">${escapeHtml(book.title.slice(0, 1))}</div>`
      }
    </div>
    <form id="edit-form" class="edit-form">
      <label>Title<input name="title" value="${escapeAttr(book.title)}" /></label>
      <label>Authors (comma-separated)
        <input name="authors" value="${escapeAttr((book.authors || []).join(", "))}" />
      </label>
      <label>Tags (comma-separated)
        <input name="tags" value="${escapeAttr((book.tags || []).join(", "))}" />
      </label>
      <label>Series<input name="series" value="${escapeAttr(book.series || "")}" /></label>
      <label>Series index<input name="series_index" type="number" step="any" value="${book.series_index ?? ""}" /></label>
      <label>ISBN<input name="isbn" value="${escapeAttr(book.isbn || "")}" /></label>
      <label>Publisher<input name="publisher" value="${escapeAttr(book.publisher || "")}" /></label>
      <label>Published<input name="published_date" value="${escapeAttr(book.published_date || "")}" /></label>
      <label>Language<input name="language" value="${escapeAttr(book.language || "")}" /></label>
      <label>Description<textarea name="description" rows="4">${escapeHtml(book.description || "")}</textarea></label>
      <div class="form-actions">
        <button type="submit" class="primary">Save</button>
        <button type="button" id="fetch-metadata">Fetch metadata</button>
      </div>
    </form>
    ${formats ? `<p class="downloads"><strong>Download:</strong> ${formats}</p>` : ""}
  `;

  content.querySelector("#edit-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const seriesIndex = fd.get("series_index");
    const body = {
      title: fd.get("title"),
      authors: splitList(fd.get("authors")),
      tags: splitList(fd.get("tags")),
      series: fd.get("series") || null,
      series_index: seriesIndex === "" ? null : Number(seriesIndex),
      isbn: fd.get("isbn") || null,
      publisher: fd.get("publisher") || null,
      published_date: fd.get("published_date") || null,
      language: fd.get("language") || null,
      description: fd.get("description") || null,
    };
    try {
      await api(`${apiBase()}/books/${id}`, {
        method: "PATCH",
        body: JSON.stringify(body),
      });
      showStatus("Saved.");
      await loadBooks();
      await loadFilters();
      openDetail(id);
    } catch (err) {
      showStatus(err.message, true);
    }
  });

  content.querySelector("#fetch-metadata").addEventListener("click", () =>
    openMetadataDialog(id)
  );
}

function splitList(value) {
  return String(value || "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

async function openMetadataDialog(bookId) {
  const dialog = $("#metadata-dialog");
  const list = $("#metadata-list");
  list.innerHTML = "<p>Loading…</p>";
  dialog.showModal();
  try {
    const candidates = await api(
      `${apiBase()}/books/${bookId}/metadata-candidates`
    );
    if (!candidates.length) {
      list.innerHTML = "<p>No matches found.</p>";
      return;
    }
    list.innerHTML = candidates
      .map(
        (c, i) => `
      <button type="button" class="metadata-item" data-index="${i}">
        <strong>${escapeHtml(c.title || "Untitled")}</strong>
        <span>${escapeHtml((c.authors || []).join(", "))}</span>
        ${c.published_date ? `<span>${escapeHtml(c.published_date)}</span>` : ""}
      </button>`
      )
      .join("");
    list.querySelectorAll(".metadata-item").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const idx = Number(btn.dataset.index);
        try {
          await api(`${apiBase()}/books/${bookId}/apply-metadata`, {
            method: "POST",
            body: JSON.stringify({ candidate: candidates[idx] }),
          });
          dialog.close();
          showStatus("Metadata applied.");
          await loadBooks();
          await loadFilters();
          openDetail(bookId);
        } catch (err) {
          showStatus(err.message, true);
        }
      });
    });
  } catch (err) {
    list.innerHTML = `<p class="error">${escapeHtml(err.message)}</p>`;
  }
}

async function createLibrary() {
  const name = window.prompt("New library name:");
  if (!name || !name.trim()) return;
  try {
    const lib = await api("/api/libraries", {
      method: "POST",
      body: JSON.stringify({ name: name.trim() }),
    });
    await loadLibraries();
    await switchLibrary(lib.id);
    showStatus(`Created “${lib.name}”.`);
  } catch (err) {
    showStatus(err.message, true);
  }
}

async function renameLibrary() {
  const current = state.libraries.find((l) => l.id === state.libraryId);
  if (!current) return;
  const name = window.prompt("Rename library:", current.name);
  if (!name || !name.trim() || name.trim() === current.name) return;
  try {
    const lib = await api(`/api/libraries/${encodeURIComponent(state.libraryId)}`, {
      method: "PATCH",
      body: JSON.stringify({ name: name.trim() }),
    });
    await loadLibraries();
    state.libraryId = lib.id;
    renderLibrarySelect();
    showStatus("Library renamed.");
  } catch (err) {
    showStatus(err.message, true);
  }
}

async function deleteLibrary() {
  if (state.libraries.length <= 1) {
    showStatus("Cannot delete the last library.", true);
    return;
  }
  const current = state.libraries.find((l) => l.id === state.libraryId);
  if (!current) return;
  const ok = window.confirm(
    `Delete library “${current.name}” and all its books? This cannot be undone.`
  );
  if (!ok) return;
  try {
    await api(`/api/libraries/${encodeURIComponent(state.libraryId)}`, {
      method: "DELETE",
    });
    resetCatalogView();
    await loadLibraries();
    await loadFilters();
    await loadBooks();
    showStatus("Library deleted.");
  } catch (err) {
    showStatus(err.message, true);
  }
}

function wireEvents() {
  $("#library-select").addEventListener("change", (e) => {
    switchLibrary(e.target.value);
  });
  $("#library-new").addEventListener("click", () => createLibrary());
  $("#library-rename").addEventListener("click", () => renameLibrary());
  $("#library-delete").addEventListener("click", () => deleteLibrary());

  $("#search-input").addEventListener(
    "input",
    debounce((e) => {
      state.query = e.target.value.trim();
      loadBooks();
    }, 250)
  );

  $("#import-btn").addEventListener("click", () => {
    $("#import-dialog").showModal();
  });

  $("#import-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const path = $("#import-path").value.trim();
    if (!path) return;
    $("#import-dialog").close();
    showStatus("Importing…");
    try {
      const result = await api(`${apiBase()}/import`, {
        method: "POST",
        body: JSON.stringify({ path }),
      });
      const msg = `Added ${result.added}, merged ${result.merged}, skipped ${result.skipped}.`;
      showStatus(result.errors?.length ? `${msg} ${result.errors.length} errors.` : msg);
      await loadBooks();
      await loadFilters();
    } catch (err) {
      showStatus(err.message, true);
    }
  });

  $("#upload-input").addEventListener("change", async (e) => {
    const file = e.target.files?.[0];
    if (!file) return;
    const form = new FormData();
    form.append("file", file);
    showStatus("Uploading…");
    try {
      const res = await fetch(`${apiBase()}/books/upload`, {
        method: "POST",
        body: form,
      });
      if (!res.ok) throw new Error(await res.text());
      const data = await res.json();
      showStatus(`Upload: ${data.status}.`);
      await loadBooks();
      await loadFilters();
      if (data.book?.id) openDetail(data.book.id);
    } catch (err) {
      showStatus(err.message, true);
    }
    e.target.value = "";
  });

  $("#close-detail").addEventListener("click", () => {
    $("#detail-panel").hidden = true;
    state.selectedId = null;
    renderGrid();
  });
}

function debounce(fn, ms) {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

async function init() {
  wireEvents();
  await loadLibraries();
  await loadFilters();
  await loadBooks();
}

init();
