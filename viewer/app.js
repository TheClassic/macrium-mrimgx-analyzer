(function () {
  "use strict";

  const MAGIC = "MRVWPK01";
  const HEADER_SIZE = 32;
  const VERSION = 1;

  const state = {
    file: null,
    manifest: null,
    selectedElement: null,
    specialLoaded: false,
  };

  const bundleInput = document.getElementById("bundle-input");
  const loadStatus = document.getElementById("load-status");
  const treeRoot = document.getElementById("tree-root");
  const overviewEmpty = document.getElementById("overview-empty");
  const overviewContent = document.getElementById("overview-content");
  const overviewGrid = document.getElementById("overview-grid");
  const overviewImages = document.getElementById("overview-images");
  const overviewNotes = document.getElementById("overview-notes");
  const detailEmpty = document.getElementById("detail-empty");
  const detailContent = document.getElementById("detail-content");
  const detailTitle = document.getElementById("detail-title");
  const detailGrid = document.getElementById("detail-grid");
  const specialToggle = document.getElementById("special-toggle");
  const specialContainer = document.getElementById("special-container");

  bundleInput.addEventListener("change", async (event) => {
    const file = event.target.files && event.target.files[0];
    if (!file) {
      return;
    }
    try {
      await loadBundle(file);
    } catch (error) {
      console.error(error);
      setStatus(`Failed to load bundle: ${error.message}`, true);
    }
  });

  specialToggle.addEventListener("click", async () => {
    if (!state.manifest || !state.manifest.special) {
      return;
    }

    if (specialContainer.hidden) {
      specialContainer.hidden = false;
      specialToggle.textContent = "Hide Synthetic And Metadata Buckets";
      if (!state.specialLoaded) {
        setStatus("Loading synthetic buckets...");
        const chunk = await readJsonChunk(state.manifest.special.chunk_offset, state.manifest.special.chunk_length);
        renderSpecialChunk(chunk);
        state.specialLoaded = true;
        setStatus("Synthetic buckets loaded.");
      }
      return;
    }

    specialContainer.hidden = true;
    specialToggle.textContent = "Show Synthetic And Metadata Buckets";
  });

  async function loadBundle(file) {
    resetView();
    state.file = file;
    setStatus("Reading bundle header...");
    const headerBuffer = await file.slice(0, HEADER_SIZE).arrayBuffer();
    const header = parseHeader(headerBuffer);
    if (header.magic !== MAGIC) {
      throw new Error("Unrecognized viewer bundle header.");
    }
    if (header.version !== VERSION) {
      throw new Error(`Unsupported viewer bundle version ${header.version}.`);
    }

    setStatus("Reading manifest...");
    const manifest = await readJsonChunk(header.manifestOffset, header.manifestLength);
    state.manifest = manifest;
    renderOverview(manifest);

    if (manifest.special) {
      specialToggle.disabled = false;
      specialToggle.textContent = "Show Synthetic And Metadata Buckets";
    }

    setStatus("Loading root tree...");
    const rootChunk = await readJsonChunk(manifest.root.chunk_offset, manifest.root.chunk_length);
    renderTree(rootChunk);
    setStatus(`Loaded ${file.name}.`);
  }

  function resetView() {
    state.manifest = null;
    state.file = null;
    state.selectedElement = null;
    state.specialLoaded = false;
    treeRoot.innerHTML = "";
    specialContainer.innerHTML = "";
    specialContainer.hidden = true;
    specialToggle.disabled = true;
    specialToggle.textContent = "Show Synthetic And Metadata Buckets";
    overviewEmpty.hidden = false;
    overviewContent.hidden = true;
    detailEmpty.hidden = false;
    detailContent.hidden = true;
  }

  function parseHeader(buffer) {
    const view = new DataView(buffer);
    const magicBytes = new Uint8Array(buffer, 0, 8);
    const magic = new TextDecoder("utf-8").decode(magicBytes);
    const version = view.getUint32(8, true);
    const headerSize = view.getUint32(12, true);
    const manifestOffset = Number(view.getBigUint64(16, true));
    const manifestLength = Number(view.getBigUint64(24, true));
    if (headerSize !== HEADER_SIZE) {
      throw new Error(`Unexpected header size ${headerSize}.`);
    }
    return { magic, version, manifestOffset, manifestLength };
  }

  async function readJsonChunk(offset, length) {
    if (!state.file) {
      throw new Error("No bundle file loaded.");
    }
    const text = await state.file.slice(offset, offset + length).text();
    return JSON.parse(text);
  }

  function renderOverview(manifest) {
    overviewGrid.innerHTML = "";
    overviewImages.innerHTML = "";
    overviewNotes.innerHTML = "";
    overviewEmpty.hidden = true;
    overviewContent.hidden = false;

    const rows = [
      ["Target file", manifest.target_file],
      ["Restore point", `${manifest.target_backup_type} file #${manifest.target_file_number}`],
      ["Images analyzed", `${manifest.analyzed_image_count} (requested ${manifest.requested_image_count})`],
      ["Stored bytes", formatBytes(manifest.totals.stored_bytes)],
      ["Logical bytes", formatBytes(manifest.totals.changed_bytes)],
      ["Flat buckets", formatInteger(manifest.totals.bucket_count)],
    ];
    for (const [label, value] of rows) {
      appendDefinition(overviewGrid, label, value);
    }

    for (const image of manifest.analyzed_images || []) {
      const card = document.createElement("div");
      card.className = "image-card";
      card.innerHTML = `
        <strong>file #${image.file_number} (${escapeHtml(image.backup_type)})</strong><br>
        stored=${escapeHtml(formatBytes(image.total_stored_bytes))},
        logical=${escapeHtml(formatBytes(image.total_changed_bytes))},
        changed blocks=${escapeHtml(formatInteger(image.changed_block_count))}
        ${image.parent_file_number === null ? "" : `, parent=#${escapeHtml(String(image.parent_file_number))}`}
      `;
      overviewImages.appendChild(card);
    }

    for (const note of manifest.notes || []) {
      const item = document.createElement("li");
      item.textContent = note;
      overviewNotes.appendChild(item);
    }
  }

  function renderTree(rootChunk) {
    treeRoot.innerHTML = "";
    const item = createTreeItem({
      ...rootChunk.node,
      chunk_offset: state.manifest.root.chunk_offset,
      chunk_length: state.manifest.root.chunk_length,
      _initialChunk: rootChunk,
    });
    treeRoot.appendChild(item.element);
    selectNode(item.summary, item.row);
  }

  function renderSpecialChunk(chunk) {
    specialContainer.innerHTML = "";
    for (const bucket of chunk.buckets || []) {
      const item = createTreeItem(bucket);
      specialContainer.appendChild(item.element);
    }
  }

  function createTreeItem(summary) {
    const item = document.createElement("div");
    item.className = "tree-item";

    const row = document.createElement("div");
    row.className = "tree-row";
    item.appendChild(row);

    const hasChildren = summary.kind === "directory" && (summary.child_count > 0 || summary.path === ".\\");
    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "tree-toggle";
    toggle.textContent = "+";
    toggle.disabled = !hasChildren;
    row.appendChild(toggle);

    const badge = document.createElement("span");
    badge.className = `kind-badge ${summary.kind === "file" ? "file" : summary.kind !== "directory" ? "synthetic" : ""}`.trim();
    badge.textContent = summary.kind === "directory" ? "Dir" : summary.kind === "file" ? "File" : "Special";
    row.appendChild(badge);

    const label = document.createElement("button");
    label.type = "button";
    label.className = "tree-label-button";
    label.innerHTML = `
      <span class="tree-name">${escapeHtml(summary.name)}</span>
      <span class="tree-metrics">${escapeHtml(formatSummary(summary))}</span>
    `;
    row.appendChild(label);

    const children = document.createElement("div");
    children.className = "tree-children";
    children.hidden = true;
    item.appendChild(children);

    const localState = {
      loaded: false,
      loading: false,
      summary,
      row,
      children,
      toggle,
      initialChunk: summary._initialChunk || null,
    };

    toggle.addEventListener("click", async () => {
      if (children.hidden) {
        children.hidden = false;
        toggle.textContent = "−";
        if (!localState.loaded && !localState.loading) {
          await loadChildren(localState);
        }
      } else {
        children.hidden = true;
        toggle.textContent = "+";
      }
    });

    label.addEventListener("click", () => selectNode(summary, row));

    return { element: item, row, summary };
  }

  async function loadChildren(localState) {
    if (!localState.summary.chunk_offset || !localState.summary.chunk_length) {
      return;
    }
    localState.loading = true;
    setStatus(`Loading ${localState.summary.path}...`);
    const chunk = localState.initialChunk || await readJsonChunk(localState.summary.chunk_offset, localState.summary.chunk_length);
    localState.initialChunk = null;
    localState.children.innerHTML = "";

    for (const directory of chunk.directories || []) {
      const item = createTreeItem(directory);
      localState.children.appendChild(item.element);
    }

    for (const file of chunk.files || []) {
      const item = createTreeItem(file);
      localState.children.appendChild(item.element);
    }

    localState.loaded = true;
    localState.loading = false;
    setStatus(`Loaded ${localState.summary.path}.`);
  }

  function selectNode(summary, rowElement) {
    if (state.selectedElement) {
      state.selectedElement.classList.remove("selected");
    }
    state.selectedElement = rowElement;
    rowElement.classList.add("selected");
    detailEmpty.hidden = true;
    detailContent.hidden = false;
    detailTitle.textContent = summary.path;
    detailGrid.innerHTML = "";

    const rows = [
      ["Name", summary.name],
      ["Kind", summary.kind],
      ["Stored bytes", formatBytes(summary.stored_bytes)],
      ["Stored percent", formatPercent(summary.stored_percent)],
      ["Logical bytes", formatBytes(summary.changed_bytes)],
      ["Logical percent", formatPercent(summary.changed_percent)],
      ["Blocks", formatInteger(summary.blocks)],
      ["Changed ranges", summary.changed_ranges == null ? "n/a" : formatInteger(summary.changed_ranges)],
      ["Images", summary.image_occurrences == null ? "n/a" : formatInteger(summary.image_occurrences)],
    ];
    if (summary.image_file_numbers && summary.image_file_numbers.length) {
      rows.push(["Image file numbers", summary.image_file_numbers.join(", ")]);
    }
    if (summary.bucket_key) {
      rows.push(["Bucket key", summary.bucket_key]);
    }
    if (summary.child_count != null && summary.kind === "directory") {
      rows.push(["Child directories", formatInteger(summary.child_count)]);
    }

    for (const [label, value] of rows) {
      appendDefinition(detailGrid, label, value);
    }
  }

  function appendDefinition(container, label, value) {
    const dt = document.createElement("dt");
    dt.textContent = label;
    const dd = document.createElement("dd");
    dd.textContent = value;
    container.appendChild(dt);
    container.appendChild(dd);
  }

  function formatSummary(summary) {
    const parts = [
      `stored=${formatBytes(summary.stored_bytes)}`,
      `logical=${formatBytes(summary.changed_bytes)}`,
      `blocks=${formatInteger(summary.blocks)}`,
    ];
    if (summary.image_occurrences != null) {
      parts.push(`images=${formatInteger(summary.image_occurrences)}`);
    }
    return parts.join(", ");
  }

  function formatBytes(value) {
    if (typeof value !== "number" || !Number.isFinite(value)) {
      return "n/a";
    }
    const units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];
    let size = value;
    let unitIndex = 0;
    while (Math.abs(size) >= 1024 && unitIndex < units.length - 1) {
      size /= 1024;
      unitIndex += 1;
    }
    if (unitIndex === 0) {
      return `${Math.round(size)} ${units[unitIndex]}`;
    }
    return `${size.toFixed(2)} ${units[unitIndex]}`;
  }

  function formatPercent(value) {
    if (typeof value !== "number" || !Number.isFinite(value)) {
      return "n/a";
    }
    return `${value.toFixed(1)}%`;
  }

  function formatInteger(value) {
    if (typeof value !== "number" || !Number.isFinite(value)) {
      return "n/a";
    }
    return Math.round(value).toLocaleString();
  }

  function setStatus(message, isError) {
    loadStatus.textContent = message;
    loadStatus.classList.toggle("muted", !isError);
  }

  function escapeHtml(value) {
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/\"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }
})();
