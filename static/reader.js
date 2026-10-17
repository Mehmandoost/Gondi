const readerState = {
  bookId: null,
  format: null,
  page: 1,
  pageCount: 1,
  title: "",
  direction: "ltr",
  formats: [],
  highlights: [],
  editingHighlightId: null,
  keyHandler: null,
};

let deps = null;
let pendingSelection = null;

function $(sel, root = document) {
  return root.querySelector(sel);
}

function bookBase() {
  return `${deps.apiBase()}/books/${readerState.bookId}`;
}

function formatQuery() {
  return `format=${encodeURIComponent(readerState.format)}`;
}

async function loadReaderInfo(format = null) {
  const qs = format ? `?format=${encodeURIComponent(format)}` : "";
  const info = await deps.api(`${bookBase()}/reader${qs}`);
  readerState.format = info.format;
  readerState.pageCount = info.page_count;
  readerState.page = info.page;
  readerState.title = info.title;
  readerState.direction = info.direction || "ltr";
  readerState.formats = info.formats || [];
  return info;
}

async function saveProgress() {
  if (!readerState.bookId || !readerState.format) return;
  try {
    await deps.api(`${bookBase()}/reader/progress`, {
      method: "PUT",
      body: JSON.stringify({
        format: readerState.format,
        page: readerState.page,
      }),
    });
  } catch {
    /* ignore progress errors */
  }
}

const saveProgressDebounced = debounce(saveProgress, 400);

async function loadHighlights() {
  const list = await deps.api(
    `${bookBase()}/highlights?${formatQuery()}&page=${readerState.page}`
  );
  readerState.highlights = list;
  renderHighlightList();
  renderHighlightOverlays();
  if (readerState.editingHighlightId) {
    const still = list.some((h) => h.id === readerState.editingHighlightId);
    if (still) {
      openNotationEditor(readerState.editingHighlightId, false);
    } else {
      closeNotationEditor();
    }
  }
}

function renderToolbar() {
  const titleEl = $("#reader-title");
  const countEl = $("#reader-page-count");
  const input = $("#reader-page-input");
  const formatSelect = $("#reader-format");

  titleEl.textContent = readerState.title;
  countEl.textContent = String(readerState.pageCount);
  input.max = String(readerState.pageCount);
  input.value = String(readerState.page);

  if (readerState.formats.length > 1) {
    formatSelect.hidden = false;
    formatSelect.innerHTML = readerState.formats
      .map(
        (f) =>
          `<option value="${deps.escapeAttr(f)}"${
            f === readerState.format ? " selected" : ""
          }>${deps.escapeHtml(f)}</option>`
      )
      .join("");
  } else {
    formatSelect.hidden = true;
  }
}

function highlightById(id) {
  return readerState.highlights.find((h) => h.id === id);
}

function openNotationEditor(highlightId, focusText = true) {
  const hl = highlightById(highlightId);
  if (!hl) return;

  readerState.editingHighlightId = highlightId;
  const editor = $("#reader-note-editor");
  const quoteEl = $("#reader-note-quote");
  const textEl = $("#reader-note-text");

  editor.hidden = false;
  quoteEl.textContent = hl.quote;
  textEl.value = hl.note ?? "";

  $("#reader-highlight-list")
    .querySelectorAll(".reader-hl-item")
    .forEach((item) => {
      item.classList.toggle("selected", Number(item.dataset.id) === highlightId);
    });

  $("#reader-highlights-layer")
    ?.querySelectorAll(".reader-hl-rect")
    .forEach((el) => {
      el.classList.toggle("active", Number(el.dataset.id) === highlightId);
    });

  if (focusText) {
    textEl.focus();
  }
}

function closeNotationEditor() {
  readerState.editingHighlightId = null;
  const editor = $("#reader-note-editor");
  if (editor) editor.hidden = true;
  $("#reader-highlight-list")
    ?.querySelectorAll(".reader-hl-item.selected")
    .forEach((el) => el.classList.remove("selected"));
  $("#reader-highlights-layer")
    ?.querySelectorAll(".reader-hl-rect.active")
    .forEach((el) => el.classList.remove("active"));
}

async function saveNotationEditor() {
  const id = readerState.editingHighlightId;
  if (!id) return;
  const textEl = $("#reader-note-text");
  try {
    await deps.api(`${bookBase()}/highlights/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ note: textEl.value.trim() || null }),
    });
    await loadHighlights();
    openNotationEditor(id, false);
    deps.showStatus("Notation saved.");
  } catch (err) {
    deps.showStatus(err.message, true);
  }
}

async function deleteEditingHighlight() {
  const id = readerState.editingHighlightId;
  if (!id) return;
  const ok = window.confirm("Delete this highlight and its notation?");
  if (!ok) return;
  try {
    await deps.api(`${bookBase()}/highlights/${id}`, { method: "DELETE" });
    closeNotationEditor();
    await loadHighlights();
  } catch (err) {
    deps.showStatus(err.message, true);
  }
}

function renderHighlightList() {
  const list = $("#reader-highlight-list");
  if (!readerState.highlights.length) {
    list.innerHTML = `<li class="reader-hl-empty">No highlights on this page. Select text to highlight and add a notation.</li>`;
    return;
  }
  list.innerHTML = readerState.highlights
    .map((hl) => {
      const note = (hl.note ?? "").trim();
      const notationHtml = note
        ? `<p class="reader-hl-notation">${deps.escapeHtml(note)}</p>`
        : `<p class="reader-hl-notation muted">Tap to add notation…</p>`;
      return `<li class="reader-hl-item${
        readerState.editingHighlightId === hl.id ? " selected" : ""
      }" data-id="${hl.id}">
        <blockquote class="reader-hl-quote">${deps.escapeHtml(hl.quote)}</blockquote>
        ${notationHtml}
      </li>`;
    })
    .join("");

  list.querySelectorAll(".reader-hl-item").forEach((item) => {
    item.addEventListener("click", () => {
      openNotationEditor(Number(item.dataset.id));
    });
  });
}

function renderHighlightOverlays() {
  const layer = $("#reader-highlights-layer");
  if (!layer) return;
  layer.innerHTML = readerState.highlights
    .flatMap((hl) => {
      const rects = mergeHighlightRects(hl.rects || []);
      return rects.map(
        (r, i) =>
          `<div class="reader-hl-rect${
            readerState.editingHighlightId === hl.id ? " active" : ""
          }" data-id="${hl.id}" data-part="${i}" style="left:${r.x0 * 100}%;top:${r.y0 * 100}%;width:${(r.x1 - r.x0) * 100}%;height:${(r.y1 - r.y0) * 100}%;"></div>`
      );
    })
    .join("");

  layer.querySelectorAll(".reader-hl-rect").forEach((el) => {
    el.addEventListener("click", (e) => {
      e.stopPropagation();
      openNotationEditor(Number(el.dataset.id));
    });
  });
}

function buildTextLayer(words, direction, pageHeightPx) {
  const layer = $("#reader-text-layer");
  layer.dir = direction;
  layer.innerHTML = words
    .map((w) => {
      const left = w.x0 * 100;
      const top = w.y0 * 100;
      const width = (w.x1 - w.x0) * 100;
      const height = (w.y1 - w.y0) * 100;
      const fontPx = Math.max((w.y1 - w.y0) * pageHeightPx * 0.92, 6);
      return `<span class="reader-word" data-x0="${w.x0}" data-y0="${w.y0}" data-x1="${w.x1}" data-y1="${w.y1}" style="left:${left}%;top:${top}%;width:${width}%;height:${height}%;font-size:${fontPx}px;">${deps.escapeHtml(w.text)}</span>`;
    })
    .join(" ");
}

function rectsOnSameLine(a, b) {
  const overlap = Math.min(a.y1, b.y1) - Math.max(a.y0, b.y0);
  const h = Math.min(a.y1 - a.y0, b.y1 - b.y0);
  if (h <= 0) return false;
  return overlap >= h * 0.35;
}

function unionRect(into, r) {
  into.x0 = Math.min(into.x0, r.x0);
  into.y0 = Math.min(into.y0, r.y0);
  into.x1 = Math.max(into.x1, r.x1);
  into.y1 = Math.max(into.y1, r.y1);
}

/** One continuous band per text line (fills gaps between words). */
function mergeHighlightRects(rects) {
  if (!rects.length) return rects;
  let lines = rects.map((r) => ({ ...r }));
  let changed = true;
  while (changed) {
    changed = false;
    const next = [];
    for (const r of lines) {
      const match = next.find((ln) => rectsOnSameLine(ln, r));
      if (match) {
        unionRect(match, r);
        changed = true;
      } else {
        next.push({ ...r });
      }
    }
    lines = next;
  }
  return lines.sort((a, b) => a.y0 - b.y0 || a.x0 - b.x0);
}

function rectsFromSelectedWords(range, page) {
  const words = page.querySelectorAll(".reader-word");
  if (!words.length) return null;

  const rects = [];
  for (const word of words) {
    if (!range.intersectsNode(word)) continue;
    rects.push({
      x0: Number(word.dataset.x0),
      y0: Number(word.dataset.y0),
      x1: Number(word.dataset.x1),
      y1: Number(word.dataset.y1),
    });
  }
  if (!rects.length) return null;
  return mergeHighlightRects(rects);
}

function rectsFromSelectionRange(range, page) {
  const pageRect = page.getBoundingClientRect();
  const rects = [];
  for (const r of range.getClientRects()) {
    if (r.width < 1 || r.height < 1) continue;
    const w = r.width / pageRect.width;
    const h = r.height / pageRect.height;
    if (w > 0.92 || h > 0.25) continue;
    rects.push({
      x0: (r.left - pageRect.left) / pageRect.width,
      y0: (r.top - pageRect.top) / pageRect.height,
      x1: (r.right - pageRect.left) / pageRect.width,
      y1: (r.bottom - pageRect.top) / pageRect.height,
    });
  }
  if (!rects.length) return null;
  return mergeHighlightRects(rects);
}

async function loadPage() {
  const img = $("#reader-page-img");
  const textLayer = $("#reader-text-layer");
  hideSelectionPopover();
  closeNotationEditor();

  img.alt = `Page ${readerState.page}`;
  img.src = `${bookBase()}/reader/pages/${readerState.page}?${formatQuery()}`;

  textLayer.innerHTML = "";
  renderToolbar();

  await new Promise((resolve, reject) => {
    img.onload = () => resolve();
    img.onerror = () => reject(new Error("Failed to load page"));
  });

  try {
    const data = await deps.api(
      `${bookBase()}/reader/pages/${readerState.page}/words?${formatQuery()}`
    );
    buildTextLayer(
      data.words || [],
      data.direction || readerState.direction,
      img.clientHeight
    );
  } catch {
    textLayer.innerHTML = "";
  }

  await loadHighlights();
  saveProgressDebounced();
}

function hideSelectionPopover() {
  pendingSelection = null;
  const pop = $("#reader-selection-popover");
  pop.hidden = true;
  const noteInput = $("#reader-new-notation");
  if (noteInput) noteInput.value = "";
}

function showSelectionPopover(x, y) {
  const pop = $("#reader-selection-popover");
  const wrap = $(".reader-stage-wrap");
  const wrapRect = wrap.getBoundingClientRect();
  pop.style.left = `${x - wrapRect.left}px`;
  pop.style.top = `${y - wrapRect.top}px`;
  pop.hidden = false;
  const noteInput = $("#reader-new-notation");
  noteInput.value = "";
  setTimeout(() => noteInput.focus(), 0);
}

function selectionRectsNormalized() {
  const sel = window.getSelection();
  if (!sel || sel.isCollapsed || !sel.rangeCount) return null;
  const range = sel.getRangeAt(0);
  const page = $("#reader-page");
  if (!page.contains(range.commonAncestorContainer)) return null;

  const quote = sel.toString().trim();
  if (!quote) return null;

  const rects =
    rectsFromSelectedWords(range, page) ??
    rectsFromSelectionRange(range, page);
  if (!rects?.length) return null;
  return { quote, rects };
}

function captureSelectionForPopover() {
  const payload = selectionRectsNormalized();
  if (!payload) {
    hideSelectionPopover();
    return;
  }
  pendingSelection = payload;
  const sel = window.getSelection();
  if (!sel?.rangeCount) return;
  const rect = sel.getRangeAt(0).getBoundingClientRect();
  showSelectionPopover(rect.left + rect.width / 2, rect.top - 8);
}

async function createHighlightFromPopover() {
  if (!pendingSelection) return;
  const note = $("#reader-new-notation").value.trim() || null;
  const payload = pendingSelection;
  hideSelectionPopover();
  window.getSelection()?.removeAllRanges();
  try {
    const created = await deps.api(`${bookBase()}/highlights`, {
      method: "POST",
      body: JSON.stringify({
        format: readerState.format,
        page: readerState.page,
        quote: payload.quote,
        rects: payload.rects,
        note,
      }),
    });
    await loadHighlights();
    openNotationEditor(created.id, !note);
  } catch (err) {
    deps.showStatus(err.message, true);
  }
}

function goToPage(next) {
  const page = Math.max(1, Math.min(readerState.pageCount, next));
  if (page === readerState.page) return;
  readerState.page = page;
  loadPage().catch((err) => deps.showStatus(err.message, true));
}

function closeReader() {
  const overlay = $("#reader-overlay");
  overlay.hidden = true;
  hideSelectionPopover();
  closeNotationEditor();
  saveProgress();
  if (readerState.keyHandler) {
    document.removeEventListener("keydown", readerState.keyHandler);
    readerState.keyHandler = null;
  }
  readerState.bookId = null;
}

export async function openReader(bookId) {
  readerState.bookId = bookId;
  const overlay = $("#reader-overlay");
  overlay.hidden = false;

  try {
    await loadReaderInfo();
    renderToolbar();
    await loadPage();
  } catch (err) {
    deps.showStatus(err.message, true);
    closeReader();
    return;
  }

  if (!readerState.keyHandler) {
    readerState.keyHandler = (e) => {
      if ($("#reader-overlay").hidden) return;
      const inField = e.target.matches("input, textarea, select");
      if (inField && e.key !== "Escape") return;
      if (e.key === "ArrowLeft" && !inField) {
        e.preventDefault();
        goToPage(readerState.page - 1);
      } else if (e.key === "ArrowRight" && !inField) {
        e.preventDefault();
        goToPage(readerState.page + 1);
      } else if (e.key === "Escape") {
        if (!$("#reader-selection-popover").hidden) {
          hideSelectionPopover();
        } else if (!$("#reader-note-editor").hidden) {
          closeNotationEditor();
        } else {
          closeReader();
        }
      } else if ((e.ctrlKey || e.metaKey) && e.key === "Enter" && inField) {
        const noteText = $("#reader-note-text");
        const newNote = $("#reader-new-notation");
        if (e.target === noteText && !$("#reader-note-editor").hidden) {
          e.preventDefault();
          saveNotationEditor();
        } else if (e.target === newNote && !$("#reader-selection-popover").hidden) {
          e.preventDefault();
          createHighlightFromPopover();
        }
      }
    };
    document.addEventListener("keydown", readerState.keyHandler);
  }
}

export function setupReader(dependencies) {
  deps = dependencies;

  $("#reader-close").addEventListener("click", () => closeReader());

  $("#reader-prev").addEventListener("click", () =>
    goToPage(readerState.page - 1)
  );
  $("#reader-next").addEventListener("click", () =>
    goToPage(readerState.page + 1)
  );

  $("#reader-page-input").addEventListener("change", (e) => {
    const val = Number(e.target.value);
    if (!Number.isFinite(val)) return;
    goToPage(val);
  });

  $("#reader-format").addEventListener("change", async (e) => {
    const fmt = e.target.value;
    try {
      await loadReaderInfo(fmt);
      await loadPage();
    } catch (err) {
      deps.showStatus(err.message, true);
    }
  });

  $("#reader-save-highlight").addEventListener("click", () =>
    createHighlightFromPopover()
  );

  $("#reader-note-save").addEventListener("click", () => saveNotationEditor());
  $("#reader-note-delete").addEventListener("click", () =>
    deleteEditingHighlight()
  );

  const textLayer = $("#reader-text-layer");
  textLayer.addEventListener("mouseup", () => {
    setTimeout(() => captureSelectionForPopover(), 10);
  });

  document.addEventListener("mousedown", (e) => {
    if ($("#reader-overlay").hidden) return;
    if (
      e.target.closest(
        "#reader-selection-popover, #reader-note-editor, .reader-hl-item"
      )
    ) {
      return;
    }
    if (!e.target.closest("#reader-text-layer")) {
      hideSelectionPopover();
    }
  });
}

function debounce(fn, ms) {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}
