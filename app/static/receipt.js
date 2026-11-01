(() => {
  const { request, statusLabel, formatDate } = window.HouseAccount;
  const receiptId = document.querySelector(".detail-layout").dataset.receiptId;
  const list = document.querySelector("#item-list");
  const addButton = document.querySelector("#add-item");
  const saveDraftButton = document.querySelector("#save-draft");
  const saveConfirmedButton = document.querySelector("#save-confirmed");
  const saveIndicator = document.querySelector("#save-indicator");
  const saveError = document.querySelector("#save-error");
  const ocrMessage = document.querySelector("#ocr-message");
  const candidatePanel = document.querySelector("#candidate-panel");
  const addCandidatesButton = document.querySelector("#add-candidates");
  let detail;
  let initialized = false;
  let dirty = false;
  let saving = false;
  let rerunningOcr = false;
  let pollTimer = null;
  let runRevision = 0;
  const pollingRunIds = new Set();

  function setDirty(value = true) {
    dirty = value;
    saveIndicator.textContent = dirty ? "未保存" : "保存済み";
    saveIndicator.classList.toggle("is-dirty", dirty);
  }

  function updateSavingControls() {
    const editorDisabled = saving || !initialized;
    [...document.querySelectorAll(".editor-column input, .editor-column button, .save-bar button")]
      .forEach((control) => { control.disabled = editorDisabled; });
    addCandidatesButton.disabled = editorDisabled || !(detail?.last_successful_run?.candidates?.length);
    document.querySelector("#rerun-ocr").disabled = !initialized || rerunningOcr;
    document.querySelector("#delete-receipt").disabled = !initialized;
  }

  function updateEmptyState() {
    document.querySelector("#items-empty").hidden = list.children.length !== 0;
  }

  function clearItemErrors() {
    list.querySelectorAll(".item-error").forEach((error) => {
      error.hidden = true;
      error.textContent = "";
    });
    list.querySelectorAll('[aria-invalid="true"]').forEach((input) => {
      input.removeAttribute("aria-invalid");
    });
  }

  function showFieldErrors(fields = {}) {
    let firstInvalid = null;
    Object.entries(fields).forEach(([field, message]) => {
      const match = field.match(/^items\.(\d+)\.(?:name|tag_ids|id)?$/);
      if (!match) return;
      const row = list.children[Number(match[1])];
      if (!row) return;
      const error = row.querySelector(".item-error");
      error.textContent = String(message);
      error.hidden = false;
      const target = field.endsWith(".name")
        ? row.querySelector("[name=item-name]")
        : row.querySelector(".tag-picker > summary");
      if (target) {
        target.setAttribute("aria-invalid", "true");
        firstInvalid ||= target;
      }
    });
    firstInvalid?.focus();
  }

  function makeChip(name, onRemove) {
    const chip = document.createElement("span");
    chip.className = "tag-chip";
    const label = document.createElement("span");
    label.textContent = name;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.setAttribute("aria-label", `${name}を解除`);
    remove.textContent = "×";
    remove.addEventListener("click", onRemove);
    chip.append(label, remove);
    return chip;
  }

  function selectedTags(row) {
    return [...row.querySelectorAll(".tag-option input:checked")].map((input) => ({
      id: input.value,
      name: input.dataset.name,
    }));
  }

  function syncTagIds(row) {
    row.dataset.tagIds = [...row.querySelectorAll(".tag-option input:checked")]
      .map((input) => input.value)
      .join(",");
  }

  function renderTagChips(row) {
    const area = row.querySelector(".tag-chips");
    const selected = selectedTags(row);
    area.replaceChildren(
      ...selected.map((tag) =>
        makeChip(tag.name, () => {
          if (saving) return;
          const checkbox = row.querySelector(`input[value="${CSS.escape(tag.id)}"]`);
          if (checkbox) checkbox.checked = false;
          syncTagIds(row);
          renderTagChips(row);
          setDirty();
        }),
      ),
    );
  }

  function renderTagOptions(row, tags) {
    const options = row.querySelector(".tag-options");
    const selected = new Set(selectedTags(row).map((tag) => tag.id));
    const checkedFromServer = new Set((row.dataset.tagIds || "").split(",").filter(Boolean));
    options.replaceChildren();
    tags.forEach((tag) => {
      const label = document.createElement("label");
      label.className = "tag-option";
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.value = tag.id;
      checkbox.dataset.name = tag.name;
      checkbox.checked = selected.has(tag.id) || checkedFromServer.has(tag.id);
      checkbox.addEventListener("change", () => {
        if (saving) return;
        syncTagIds(row);
        renderTagChips(row);
        setDirty();
      });
      const name = document.createElement("span");
      name.textContent = tag.name;
      label.append(checkbox, name);
      options.append(label);
    });
    syncTagIds(row);
    renderTagChips(row);
    updateSavingControls();
  }

  async function loadTagOptions(row, query = "", additionalTags = []) {
    const revision = Number(row.dataset.tagSearchRevision || 0) + 1;
    row.dataset.tagSearchRevision = String(revision);
    const result = await request(`/api/tags?q=${encodeURIComponent(query)}`);
    if (row.dataset.tagSearchRevision !== String(revision)) return;
    const selected = selectedTags(row);
    const currentTags = new Map(selected.map((tag) => [tag.id, tag]));
    result.items.forEach((tag) => currentTags.set(tag.id, tag));
    additionalTags.forEach((tag) => currentTags.set(tag.id, tag));
    renderTagOptions(row, [...currentTags.values()]);
  }

  function createItem(item = {}) {
    const row = document.createElement("article");
    row.className = "item-card";
    row.dataset.itemId = item.id || "";
    row.dataset.sourceRunId = item.source_run_id || "";
    row.dataset.sourceCandidateId = item.source_candidate_id || "";
    row.dataset.tagIds = (item.tag_ids || []).join(",");

    const top = document.createElement("div");
    top.className = "item-topline";
    const nameLabel = document.createElement("label");
    nameLabel.className = "item-name-label";
    const nameTitle = document.createElement("span");
    nameTitle.textContent = "商品名";
    const name = document.createElement("input");
    name.type = "text";
    name.name = "item-name";
    name.maxLength = 200;
    name.required = true;
    name.autocomplete = "off";
    name.placeholder = "商品名を入力";
    name.value = item.name || "";
    name.addEventListener("input", () => setDirty());
    nameLabel.append(nameTitle, name);
    const remove = document.createElement("button");
    remove.className = "icon-button remove-item";
    remove.type = "button";
    remove.textContent = "削除";
    remove.setAttribute("aria-label", "この明細を削除");
    remove.addEventListener("click", () => {
      row.remove();
      updateEmptyState();
      setDirty();
    });
    top.append(nameLabel, remove);

    const tagBar = document.createElement("div");
    tagBar.className = "tag-bar";
    const chips = document.createElement("div");
    chips.className = "tag-chips";
    const picker = document.createElement("details");
    picker.className = "tag-picker";
    const summary = document.createElement("summary");
    summary.textContent = "タグを選ぶ";
    const menu = document.createElement("div");
    menu.className = "tag-menu";
    const search = document.createElement("input");
    search.type = "search";
    search.placeholder = "タグを検索";
    search.setAttribute("aria-label", "タグを検索");
    const options = document.createElement("div");
    options.className = "tag-options";
    const create = document.createElement("button");
    create.type = "button";
    create.className = "button button-secondary button-small create-tag";
    create.textContent = "この名前でタグを作成";
    create.addEventListener("click", async () => {
      const tagName = search.value.trim();
      if (!tagName) {
        search.focus();
        return;
      }
      try {
        const createdTag = await request("/api/tags", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: tagName }),
        });
        window.clearTimeout(searchTimer);
        search.value = "";
        await loadTagOptions(row, "", [createdTag]);
        search.focus();
      } catch (error) {
        saveError.textContent = error.message;
      }
    });
    let searchTimer;
    search.addEventListener("input", () => {
      row.dataset.tagSearchRevision = String(Number(row.dataset.tagSearchRevision || 0) + 1);
      window.clearTimeout(searchTimer);
      searchTimer = window.setTimeout(() => {
        loadTagOptions(row, search.value).catch((error) => {
          saveError.textContent = error.message;
        });
      }, 160);
    });
    picker.addEventListener("toggle", () => {
      if (picker.open) loadTagOptions(row).catch((error) => {
        saveError.textContent = error.message;
      });
    });
    menu.append(search, options, create);
    picker.append(summary, menu);
    tagBar.append(chips, picker);
    const itemError = document.createElement("p");
    itemError.className = "item-error";
    itemError.hidden = true;
    itemError.setAttribute("role", "alert");
    row.append(top, tagBar, itemError);
    if (item.tags) {
      renderTagOptions(row, item.tags);
    }
    list.append(row);
    updateSavingControls();
    updateEmptyState();
    return row;
  }

  function renderCandidates(successRun, latestRun) {
    const panel = document.querySelector("#candidate-panel");
    const candidates = successRun?.candidates || [];
    panel.hidden = !successRun;
    document.querySelector("#candidate-count").textContent = `${candidates.length}件`;
    const candidateList = document.querySelector("#candidate-list");
    candidateList.replaceChildren();
    candidates.forEach((candidate) => {
      const row = document.createElement("li");
      const name = document.createElement("span");
      name.textContent = candidate.name;
      const review = document.createElement("small");
      review.textContent = candidate.needs_review ? "要確認" : "";
      row.append(name, review);
      candidateList.append(row);
    });
    document.querySelector("#ocr-raw").textContent =
      latestRun?.raw?.raw_text || successRun?.raw?.raw_text || "原文はありません。";
    addCandidatesButton.disabled = saving || !initialized || candidates.length === 0;
  }

  function renderOcrState(run) {
    const state = document.querySelector("#ocr-state");
    const status = run?.status || "未実行";
    state.textContent = statusLabel(status);
    state.className = `state-badge state-${status}`;
    if (!run) {
      ocrMessage.textContent = "OCRの実行履歴はありません。補助操作から再実行できます。";
    } else if (run.error_message) {
      ocrMessage.textContent = run.error_message;
    } else if (status === "running" || status === "queued") {
      ocrMessage.textContent = "処理中も商品を入力して保存できます。";
    } else if (status === "no_candidates") {
      ocrMessage.textContent = "候補がありません。商品名を手入力できます。";
    } else if (status === "succeeded") {
      ocrMessage.textContent = "候補を確認し、必要な明細だけ追加してください。";
    } else {
      ocrMessage.textContent = "商品名は画像を見て手入力できます。";
    }
    const successfulRun = detail.last_successful_run || (run && ["succeeded", "no_candidates"].includes(run.status) ? run : null);
    renderCandidates(successfulRun, run);
  }

  function setLatestRun(run) {
    detail.latest_run = run;
    runRevision += 1;
    if (run && ["succeeded", "no_candidates"].includes(run.status)) {
      detail.last_successful_run = run;
    }
    renderOcrState(run);
  }

  async function refreshRun() {
    const run = detail.latest_run;
    if (!run || pollingRunIds.has(run.id)) return;
    const revision = runRevision;
    pollingRunIds.add(run.id);
    try {
      const updated = await request(`/api/ocr-runs/${encodeURIComponent(run.id)}`);
      if (detail.latest_run?.id !== run.id || runRevision !== revision) return;
      setLatestRun(updated);
      if (!["queued", "running"].includes(updated.status)) {
        window.clearInterval(pollTimer);
        pollTimer = null;
      }
    } finally {
      pollingRunIds.delete(run.id);
    }
  }

  function pollRun(run) {
    if (pollTimer !== null) {
      window.clearInterval(pollTimer);
      pollTimer = null;
    }
    if (!["queued", "running"].includes(run?.status)) return;
    pollTimer = window.setInterval(() => refreshRun().catch(() => {}), 2_000);
  }

  function collectItems() {
    return [...list.querySelectorAll(".item-card")].map((row) => ({
      id: row.dataset.itemId || null,
      name: row.querySelector("[name=item-name]").value.trim(),
      tag_ids: row.dataset.tagIds ? row.dataset.tagIds.split(",") : [],
      source_run_id: row.dataset.sourceRunId || null,
      source_candidate_id: row.dataset.sourceCandidateId || null,
    }));
  }

  async function save(status) {
    if (saving || !initialized) return;
    clearItemErrors();
    saveError.textContent = "";
    const inputs = [...list.querySelectorAll("[name=item-name]")];
    if (status === "confirmed" && inputs.length === 0) {
      saveError.textContent = "確認保存には1件以上の明細が必要です。";
      addButton.focus();
      return;
    }
    const invalid = inputs.find((input) => !input.value.trim());
    if (invalid) {
      invalid.setCustomValidity("商品名を入力してください。");
      invalid.reportValidity();
      invalid.addEventListener("input", () => invalid.setCustomValidity(""), { once: true });
      return;
    }
    saving = true;
    saveError.textContent = "";
    saveIndicator.textContent = "保存中";
    updateSavingControls();
    try {
      const result = await request(`/api/receipts/${encodeURIComponent(receiptId)}/items`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ revision: detail.revision, status, items: collectItems() }),
      });
      detail.revision = result.revision;
      detail.status = result.status;
      document.querySelector("#receipt-meta").textContent = `${formatDate(result.created_at)} ・ ${statusLabel(result.status)}`;
      [...list.querySelectorAll(".item-card")].forEach((row, index) => {
        row.dataset.itemId = result.items[index].id;
      });
      setDirty(false);
    } catch (error) {
      if (error.code === "revision_conflict") {
        saveError.textContent = "別の画面で更新されています。入力を保持しました。別タブを閉じ、内容をコピーしてから再読み込みしてください。";
      } else {
        saveError.textContent = error.message;
        showFieldErrors(error.fields);
      }
      saveIndicator.textContent = "保存できませんでした";
    } finally {
      saving = false;
      updateSavingControls();
      updateEmptyState();
    }
  }

  addCandidatesButton.addEventListener("click", () => {
    if (saving || !initialized) return;
    const run = detail.last_successful_run;
    if (!run) return;
    const existing = new Set(
      [...list.querySelectorAll(".item-card")].map((row) => `${row.dataset.sourceRunId}:${row.dataset.sourceCandidateId}`),
    );
    let added = 0;
    run.candidates.forEach((candidate) => {
      const key = `${run.id}:${candidate.id}`;
      if (existing.has(key)) return;
      createItem({
        name: candidate.name,
        source_run_id: run.id,
        source_candidate_id: candidate.id,
        tag_ids: [],
        tags: [],
      });
      existing.add(key);
      added += 1;
    });
    if (added) {
      list.lastElementChild.querySelector("[name=item-name]").focus();
      setDirty();
    }
  });
  addButton.addEventListener("click", () => {
    if (!initialized) return;
    createItem().querySelector("[name=item-name]").focus();
    setDirty();
  });
  saveDraftButton.addEventListener("click", () => save("draft"));
  saveConfirmedButton.addEventListener("click", () => save("confirmed"));
  document.querySelector("#rerun-ocr").addEventListener("click", async () => {
    if (!initialized || rerunningOcr) return;
    rerunningOcr = true;
    const rerunRevision = ++runRevision;
    if (pollTimer !== null) {
      window.clearInterval(pollTimer);
      pollTimer = null;
    }
    updateSavingControls();
    let rerunCompleted = false;
    try {
      const result = await request(`/api/receipts/${encodeURIComponent(receiptId)}/ocr-runs`, { method: "POST" });
      const run = await request(`/api/ocr-runs/${encodeURIComponent(result.run_id)}`);
      if (runRevision !== rerunRevision) return;
      setLatestRun(run);
      pollRun(detail.latest_run);
      rerunCompleted = true;
    } catch (error) {
      ocrMessage.textContent = error.message;
    } finally {
      rerunningOcr = false;
      updateSavingControls();
      if (!rerunCompleted) pollRun(detail.latest_run);
    }
  });
  document.querySelector("#delete-receipt").addEventListener("click", async (event) => {
    if (!initialized) return;
    if (!window.confirm("このレシートと明細を削除します。よろしいですか？")) return;
    const button = event.currentTarget;
    button.disabled = true;
    try {
      await request(`/api/receipts/${encodeURIComponent(receiptId)}?revision=${detail.revision}`, { method: "DELETE" });
      setDirty(false);
      window.location.assign("/");
    } catch (error) {
      saveError.textContent = error.message;
      button.disabled = false;
    }
  });
  window.addEventListener("beforeunload", (event) => {
    if (!dirty && !saving) return;
    event.preventDefault();
    event.returnValue = "";
  });

  async function initialize() {
    updateSavingControls();
    detail = await request(`/api/receipts/${encodeURIComponent(receiptId)}`);
    document.querySelector("#receipt-meta").textContent = `${formatDate(detail.created_at)} ・ ${statusLabel(detail.status)}`;
    const imageUrl = `/api/receipts/${encodeURIComponent(receiptId)}/image`;
    document.querySelector("#receipt-image").src = imageUrl;
    document.querySelector("#image-link").href = imageUrl;
    detail.items.forEach(createItem);
    updateEmptyState();
    renderOcrState(detail.latest_run);
    if (detail.latest_run && ["queued", "running"].includes(detail.latest_run.status)) {
      pollRun(detail.latest_run);
    }
    initialized = true;
    setDirty(false);
    updateSavingControls();
  }

  initialize().catch((error) => {
    saveError.textContent = error.message;
  });
})();
