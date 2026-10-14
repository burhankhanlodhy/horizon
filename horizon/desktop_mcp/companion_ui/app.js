(() => {
  "use strict";

  const MODES = ["passthrough", "compact", "focused", "reference", "reference_only"];
  const MAX_INLINE_BYTES = 8 * 1024 * 1024;
  const byId = (id) => document.getElementById(id);
  const tokenFromFragment = () => {
    const fragment = window.location.hash.slice(1);
    if (!fragment) return "";
    const params = new URLSearchParams(fragment);
    return params.get("token") || (fragment.includes("=") ? "" : decodeURIComponent(fragment));
  };

  // Remove the bearer credential before any network request or other work.
  let token = "";
  try {
    token = tokenFromFragment();
  } catch {
    token = "";
  }
  if (window.location.hash) window.history.replaceState(null, "", window.location.pathname + window.location.search);

  const state = { staging: null, packet: "", previewRequest: "", session: false, revision: 0 };
  const modeDescriptions = {
    passthrough: "Keeps the request and supporting context as written.",
    compact: "Uses only a verified local transform; if its full packet does not meet the configured limits, it falls back to passthrough.",
    focused: "Selects relevant passages from an existing approved source. Coverage may be partial.",
    reference: "Includes a bounded exact excerpt from an existing approved source and keeps its handle for recovery.",
    reference_only: "Keeps the existing approved source handle and version for exact recovery without adding a source excerpt."
  };

  function setText(id, value) {
    byId(id).textContent = value == null || value === "" ? "—" : String(value);
  }

  function setVisible(node, visible) {
    node.hidden = !visible;
  }

  function invalidatePreview() {
    state.revision += 1;
    state.packet = "";
    state.previewRequest = "";
    byId("packet").value = "";
    byId("copy-packet").disabled = true;
    setVisible(byId("preview"), false);
    setText("copy-status", "Inputs changed. Prepare a new preview before copying.");
  }

  function showError(id, error) {
    const node = byId(id);
    node.textContent = error;
    node.hidden = !error;
  }

  function bytes(value) {
    return new TextEncoder().encode(value).byteLength;
  }

  function formatBytes(value) {
    if (!Number.isFinite(Number(value))) return "—";
    const size = Number(value);
    if (size < 1024) return `${size} bytes`;
    if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KiB`;
    return `${(size / (1024 * 1024)).toFixed(2)} MiB`;
  }

  function formatDate(value) {
    if (!value) return "not available";
    const date = new Date(value);
    return Number.isNaN(date.valueOf()) ? "not available" : date.toLocaleString();
  }

  function formatExpiry(value) {
    if (!value) return "not available";
    const date = new Date(value);
    return Number.isNaN(date.valueOf()) ? "not available" : `${date.toLocaleString()} (${date.toISOString()})`;
  }

  function publicError(code) {
    const messages = {
      invalid_request: "Check the fields and try again.",
      invalid_source_id: "That source handle is not valid. Paste a handle returned by your local MCP source.",
      query_required: "Enter a focus query for focused mode.",
      source_unavailable: "The saved source is unavailable. Check that the local broker is running and the handle is current.",
      source_version_mismatch: "The source changed or the saved handle has expired. Search the approved source again.",
      input_too_large: "The request or context is above the local size limit.",
      packet_too_large: "The prepared packet is above the configured size limit.",
      staging_unavailable: "Approved local staging is unavailable. No context was saved.",
      staging_full: "The approved staging folder has reached its item or size limit.",
      staging_expired: "This saved item expired. Save the context again if you still need it.",
      not_found: "That saved item no longer exists.",
      unauthorized: "This local session has expired. Restart the companion to create a new session.",
      session_token_in_input: "The temporary session credential cannot be included in a request or saved context.",
      packet_contains_session_token: "The preview included this temporary session credential, so it was hidden and cannot be copied.",
      session_closed: "This local session has closed. Restart the companion to continue.",
      method_not_allowed: "That action is not available.",
      request_too_large: "The input is above the local size limit.",
      preparation_failed: "The local preview could not be prepared. Check your inputs and try again.",
      source_id_and_config_required: "Enter an existing source handle and check the local broker configuration.",
      broker_unavailable: "The local source broker is unavailable. Keep its launcher running and try again."
    };
    return messages[code] || "The local companion could not complete that action. Check its terminal for service status, then try again.";
  }

  async function api(path, options = {}) {
    if (!token) throw new Error("unauthorized");
    const headers = new Headers(options.headers || {});
    headers.set("Authorization", `Bearer ${token}`);
    if (options.body !== undefined) headers.set("Content-Type", "application/json");
    let response;
    try {
      response = await fetch(path, {
        ...options,
        headers,
        credentials: "omit",
        cache: "no-store",
        redirect: "error",
        referrerPolicy: "no-referrer"
      });
    } catch {
      throw new Error("session_closed");
    }
    let data;
    try {
      data = await response.json();
    } catch {
      throw new Error("request_failed");
    }
    if (!response.ok) {
      const code = typeof data?.error === "string" ? data.error : "request_failed";
      if (response.status === 401 || response.status === 403 || code === "unauthorized" || code === "session_closed") lockPage(publicError(code));
      throw new Error(code);
    }
    return data;
  }

  function lockPage(message) {
    token = "";
    state.session = false;
    state.packet = "";
    byId("request").value = "";
    byId("context").value = "";
    byId("source-id").value = "";
    byId("query").value = "";
    byId("packet").value = "";
    byId("app").hidden = true;
    byId("boot-panel").hidden = false;
    byId("boot-panel").replaceChildren();
    const title = document.createElement("h1");
    title.textContent = "Local session ended";
    const copy = document.createElement("p");
    copy.className = "muted";
    copy.textContent = `${message} Request and packet text have been cleared from this page.`;
    byId("boot-panel").append(title, copy);
  }

  function modeChanged() {
    invalidatePreview();
    const mode = byId("mode").value;
    const sourceMode = ["focused", "reference", "reference_only"].includes(mode);
    setVisible(byId("source-fields"), sourceMode);
    setVisible(byId("query-field"), mode === "focused");
    byId("context").disabled = sourceMode;
    byId("context").setAttribute("aria-disabled", String(sourceMode));
    byId("mode-help").textContent = `${modeDescriptions[mode] || "Choose a preparation mode."}${sourceMode ? " Inline supporting context is omitted while a saved source is selected." : ""}`;
  }

  function describeMeasurements(data) {
    const measures = data.measurements || {};
    const tokens = measures.prepared_packet_token_estimate ?? measures.packet_token_estimate ?? measures.estimated_tokens ?? measures.token_estimate;
    setText("preview-tokens", tokens == null ? "Not available" : `${Number(tokens).toLocaleString()} estimated`);
    setText("preview-size", formatBytes(data.packet_utf8_bytes));
    setText("preview-mode", data.mode);
    setText("preview-fidelity", data.fidelity);
    setText("preview-coverage", data.coverage);
    setVisible(byId("fallback-note"), Boolean(data.fallback_reason));
    if (data.fallback_reason) byId("fallback-note").textContent = `Compact mode used its safe fallback: ${String(data.fallback_reason).replaceAll("_", " ")}.`;
    const provenance = data.provenance && typeof data.provenance === "object" ? data.provenance : {};
    const entries = Object.entries(provenance).filter(([, value]) => ["string", "number", "boolean"].includes(typeof value));
    const target = byId("provenance");
    target.replaceChildren();
    for (const [label, value] of entries) {
      const row = document.createElement("div");
      const name = document.createElement("span");
      name.className = "label";
      name.textContent = label.replaceAll("_", " ");
      const content = document.createElement("span");
      content.textContent = String(value);
      row.append(name, content);
      target.append(row);
    }
  }

  async function prepare() {
    const error = byId("form-error");
    showError("form-error", "");
    setVisible(byId("preview"), false);
    setText("copy-status", "");
    const request = byId("request").value;
    const context = byId("context").value;
    const mode = byId("mode").value;
    const sourceId = byId("source-id").value;
    const query = byId("query").value;
    const revision = state.revision;
    if (!MODES.includes(mode) || !state.session) return;
    if (!request.trim()) return showError("form-error", "Enter a request before preparing a preview.");
    if (bytes(request) > MAX_INLINE_BYTES || bytes(context) > MAX_INLINE_BYTES) {
      return showError("form-error", "The request and context must each be 8 MiB or smaller.");
    }
    if (["focused", "reference", "reference_only"].includes(mode) && !sourceId.trim()) {
      return showError("form-error", "Enter an existing broker source handle for this mode.");
    }
    if (mode === "focused" && !query.trim()) return showError("form-error", "Enter a focus query for focused mode.");

    const button = byId("prepare");
    button.disabled = true;
    button.textContent = "Preparing…";
    setText("prepare-status", "Building a local preview");
    try {
      const payload = { request, mode };
      if (!(["focused", "reference", "reference_only"].includes(mode))) payload.context = context;
      if (["focused", "reference", "reference_only"].includes(mode)) payload.source_id = sourceId.trim();
      if (mode === "focused") payload.query = query;
      const body = JSON.stringify(payload);
      if (body.includes(token)) throw new Error("session_token_in_input");
      if (bytes(body) > 20 * 1024 * 1024) throw new Error("request_too_large");
      const data = await api("/api/prepare", { method: "POST", body });
      if (revision !== state.revision) return;
      if (data.status !== "preview_ready" || typeof data.packet_json !== "string") throw new Error("preparation_failed");
      if (data.packet_json.includes(token)) throw new Error("packet_contains_session_token");
      state.packet = data.packet_json;
      state.previewRequest = request;
      byId("packet").value = data.packet_json;
      byId("copy-packet").disabled = false;
      byId("request-review").textContent = request;
      describeMeasurements(data);
      setVisible(byId("preview"), true);
      byId("preview").scrollIntoView({ behavior: "smooth", block: "start" });
      setText("prepare-status", "Preview ready. Review the exact packet before copying.");
    } catch (e) {
      showError("form-error", publicError(e instanceof Error ? e.message : "request_failed"));
      setText("prepare-status", "");
    } finally {
      button.disabled = false;
      button.textContent = "Prepare preview";
    }
  }

  async function copyText(value, label) {
    try {
      if (!navigator.clipboard || !window.isSecureContext) throw new Error("clipboard_unavailable");
      await navigator.clipboard.writeText(value);
      setText("copy-status", `${label} copied. Paste it into your existing chat when ready.`);
      return true;
    } catch {
      setText("copy-status", `Clipboard permission is unavailable. Select the ${label.toLowerCase()} and press Ctrl+C, then paste it into your existing chat.`);
      return false;
    }
  }

  function createStagedCard(item) {
    const card = document.createElement("article");
    card.className = "staged-item";
    const info = document.createElement("div");
    info.className = "staged-info";
    const title = document.createElement("strong");
    title.textContent = item.name || "Saved supporting context";
    const detail = document.createElement("span");
    detail.className = "muted small";
    detail.textContent = `${formatBytes(item.utf8_bytes)} · expires ${formatExpiry(item.expires_at)}`;
    info.append(title, detail);
    card.append(info);

    const source = item.source;
    if (source && typeof source.source_id === "string") {
      const handle = document.createElement("code");
      handle.className = "source-handle";
      handle.textContent = source.source_id;
      const meta = document.createElement("span");
      meta.className = "muted small source-meta";
      const lines = source.selected_lines && Number.isInteger(source.selected_lines.start) && Number.isInteger(source.selected_lines.end)
        ? ` · lines ${source.selected_lines.start}–${source.selected_lines.end}`
        : "";
      meta.textContent = `${source.kind || "text source"} · version ${source.version || "unknown"}${lines} · ${source.coverage || "existing broker snapshot"} · session source, expires ${formatExpiry(item.expires_at)}; available while the companion runs`;
      const controls = document.createElement("div");
      controls.className = "staged-controls";
      const use = document.createElement("button");
      use.type = "button";
      use.textContent = "Use this handle";
      use.addEventListener("click", () => {
        byId("source-id").value = source.source_id;
        const desiredMode = byId("mode").value === "focused" ? "focused" : "reference";
        byId("mode").value = desiredMode;
        modeChanged();
        byId("source-id").focus();
        window.scrollTo({ top: 0, behavior: "smooth" });
      });
      const copy = document.createElement("button");
      copy.type = "button";
      copy.textContent = "Copy handle";
      copy.addEventListener("click", () => void copyText(source.source_id, "Recovery handle"));
      controls.append(use, copy);
      card.append(handle, meta, controls);
    }

    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "danger-button";
    remove.textContent = "Remove saved source";
    const removal = document.createElement("div");
    removal.className = "remove-confirm";
    removal.hidden = true;
    const removalPrompt = document.createElement("span");
    removalPrompt.className = "muted small";
    removalPrompt.textContent = "Remove the staged file and block later recovery through this handle?";
    const confirmRemove = document.createElement("button");
    confirmRemove.type = "button";
    confirmRemove.className = "danger-button";
    confirmRemove.textContent = "Confirm removal";
    confirmRemove.addEventListener("click", () => void removeStaged(item.id, confirmRemove, remove, removal));
    const cancelRemove = document.createElement("button");
    cancelRemove.type = "button";
    cancelRemove.textContent = "Cancel";
    cancelRemove.addEventListener("click", () => {
      removal.hidden = true;
      remove.hidden = false;
      remove.focus();
    });
    removal.append(removalPrompt, confirmRemove, cancelRemove);
    remove.addEventListener("click", () => {
      removal.hidden = false;
      remove.hidden = true;
      confirmRemove.focus();
    });
    card.append(remove, removal);
    return card;
  }

  async function refreshStaged() {
    const target = byId("staged-list");
    target.replaceChildren();
    if (!state.staging?.enabled) {
      const note = document.createElement("p");
      note.className = "muted small";
      note.textContent = "Approved local staging is disabled in this launch configuration.";
      target.append(note);
      return;
    }
    try {
      const data = await api("/api/staging", { method: "GET" });
      const items = Array.isArray(data.items) ? data.items : [];
      if (!items.length) {
        const note = document.createElement("p");
        note.className = "muted small";
        note.textContent = "No context is saved for this local session.";
        target.append(note);
        return;
      }
      for (const item of items) target.append(createStagedCard(item));
    } catch (e) {
      const note = document.createElement("p");
      note.className = "error small";
      note.textContent = publicError(e instanceof Error ? e.message : "staging_unavailable");
      target.append(note);
    }
  }

  async function saveContext() {
    showError("import-error", "");
    const text = byId("context").value;
    if (!state.staging?.enabled) return showError("import-error", "Approved local staging is disabled for this launch.");
    if (!byId("storage-consent").checked) return showError("import-error", "Check the authorization box before saving context.");
    if (!text.trim()) return showError("import-error", "Paste supporting context before saving it for recovery.");
    if (bytes(text) > Number(state.staging.max_bytes || MAX_INLINE_BYTES)) {
      return showError("import-error", `This context is above the ${formatBytes(state.staging.max_bytes || MAX_INLINE_BYTES)} staging limit.`);
    }
    const button = byId("save-source");
    button.disabled = true;
    button.textContent = "Saving and requesting handle…";
    setText("import-status", "The approved local broker is creating a versioned handle.");
    try {
      if (text.includes(token)) throw new Error("session_token_in_input");
      const body = JSON.stringify({ text, consent: "store_for_session_recovery" });
      if (bytes(body) > 20 * 1024 * 1024) throw new Error("request_too_large");
      const data = await api("/api/staging", { method: "POST", body });
      if (data.status !== "imported" || typeof data.id !== "string" || typeof data.source?.source_id !== "string") throw new Error("staging_unavailable");
      byId("storage-consent").checked = false;
      setText("import-status", `Saved for this local session. Handle expires ${formatExpiry(data.expires_at)}.`);
      await refreshStaged();
      const matching = [...byId("staged-list").querySelectorAll(".source-handle")].find((node) => node.textContent === data.source.source_id);
      if (matching) matching.scrollIntoView({ behavior: "smooth", block: "center" });
    } catch (e) {
      showError("import-error", publicError(e instanceof Error ? e.message : "staging_unavailable"));
      setText("import-status", "No context was saved.");
    } finally {
      button.disabled = false;
      button.textContent = "Save context and create recovery handle";
    }
  }

  async function removeStaged(id, button, removeButton, confirmation) {
    if (typeof id !== "string" || !/^[0-9a-f]{32}$/.test(id)) return;
    button.disabled = true;
    button.textContent = "Removing…";
    try {
      await api(`/api/staging/${encodeURIComponent(id)}`, { method: "DELETE" });
      const selectedId = byId("source-id").value;
      const card = button.closest(".staged-item");
      if (selectedId && card?.querySelector(".source-handle")?.textContent === selectedId) {
        byId("source-id").value = "";
        invalidatePreview();
      }
      await refreshStaged();
      setText("import-status", "Saved context removed from this local session.");
    } catch (e) {
      button.disabled = false;
      button.textContent = "Confirm removal";
      confirmation.hidden = true;
      removeButton.hidden = false;
      showError("import-error", publicError(e instanceof Error ? e.message : "staging_unavailable"));
    }
  }

  async function boot() {
    if (!token) {
      byId("boot-panel").replaceChildren();
      const title = document.createElement("h1");
      title.textContent = "Start the companion from its launcher";
      const copy = document.createElement("p");
      copy.className = "muted";
      copy.textContent = "This page needs a temporary local session token. Close this tab and open the companion link printed by the launcher.";
      byId("boot-panel").append(title, copy);
      return;
    }
    try {
      const data = await api("/api/session", { method: "GET" });
      const supported = Array.isArray(data.modes) ? data.modes.filter((mode) => MODES.includes(mode)) : [];
      for (const option of [...byId("mode").options]) option.disabled = !supported.includes(option.value);
      if (!supported.length || data.status !== "ready") throw new Error("session_closed");
      if (!supported.includes(byId("mode").value)) byId("mode").value = supported.includes("compact") ? "compact" : supported[0];
      state.staging = data.staging || { enabled: false };
      state.session = true;
      byId("boot-panel").hidden = true;
      byId("app").hidden = false;
      const allowed = Boolean(state.staging.enabled && typeof state.staging.display_root === "string" && state.staging.display_root);
      byId("save-source").disabled = !allowed;
      byId("storage-consent").disabled = !allowed;
      setText("staging-state", allowed ? `Session storage · up to ${Number(state.staging.max_items || 32)} items` : "Session storage disabled");
      setText("staging-root", allowed ? state.staging.display_root : "No approved staging folder is enabled for this session.");
      if (state.staging.enabled && !allowed) setText("import-status", "The configured staging folder path is unavailable; saving remains disabled.");
      await refreshStaged();
      modeChanged();
    } catch (e) {
      byId("boot-panel").replaceChildren();
      const title = document.createElement("h1");
      title.textContent = "The local companion is unavailable";
      const copy = document.createElement("p");
      copy.className = "muted";
      copy.textContent = publicError(e instanceof Error ? e.message : "session_closed");
      byId("boot-panel").append(title, copy);
    }
  }

  byId("context").addEventListener("input", () => setText("context-count", `${formatBytes(bytes(byId("context").value))}`));
  byId("request").addEventListener("input", invalidatePreview);
  byId("context").addEventListener("input", invalidatePreview);
  byId("source-id").addEventListener("input", invalidatePreview);
  byId("query").addEventListener("input", invalidatePreview);
  byId("mode").addEventListener("change", modeChanged);
  byId("prepare").addEventListener("click", () => void prepare());
  byId("copy-packet").addEventListener("click", () => void copyText(state.packet, "Packet"));
  byId("save-source").addEventListener("click", () => void saveContext());
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && state.session) void refreshStaged();
  });
  byId("clear-session").addEventListener("click", async () => {
    const button = byId("clear-session");
    button.disabled = true;
    button.textContent = "Clearing session…";
    try {
      await api("/api/session", { method: "DELETE" });
    } catch {
      // Drop this page's credential even if the launcher has already exited.
    }
    token = "";
    state.session = false;
    byId("app").hidden = true;
    byId("boot-panel").hidden = false;
    byId("boot-panel").replaceChildren();
    const title = document.createElement("h1");
    title.textContent = "Local session cleared";
    const copy = document.createElement("p");
    copy.className = "muted";
    copy.textContent = "The companion is shutting down and removing this session’s staged files. You can close this tab.";
    byId("boot-panel").append(title, copy);
  });

  window.setInterval(() => {
    if (document.visibilityState === "visible" && state.session && state.staging?.enabled) void refreshStaged();
  }, 30000);

  void boot();
})();
