(() => {
  const { request, statusLabel, formatDate } = window.HouseAccount;
  const form = document.querySelector("#upload-form");
  const fileInput = document.querySelector("#receipt-file");
  const uploadStatus = document.querySelector("#upload-status");
  const list = document.querySelector("#receipt-list");
  let page = 1;

  function createText(tag, className, value) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    element.textContent = value;
    return element;
  }

  function receiptCard(receipt) {
    const link = document.createElement("a");
    link.className = "receipt-card";
    link.href = `/receipts/${encodeURIComponent(receipt.id)}`;
    const image = document.createElement("img");
    image.className = "receipt-thumb";
    image.src = `/api/receipts/${encodeURIComponent(receipt.id)}/image`;
    image.alt = "レシートのサムネイル";
    image.loading = "lazy";
    const content = document.createElement("span");
    content.className = "receipt-card-content";
    content.append(
      createText("strong", "receipt-card-title", formatDate(receipt.created_at)),
      createText("span", "muted", statusLabel(receipt.status)),
    );
    const states = document.createElement("span");
    states.className = "card-states";
    states.append(createText("span", "state-badge", statusLabel(receipt.ocr_status)));
    const arrow = createText("span", "card-arrow", "›");
    arrow.setAttribute("aria-hidden", "true");
    link.append(image, content, states, arrow);
    return link;
  }

  async function loadReceipts() {
    const result = await request(`/api/receipts?page=${page}`);
    list.replaceChildren(...result.items.map(receiptCard));
    document.querySelector("#empty-state").hidden = result.total !== 0;
    document.querySelector("#receipt-count").textContent = `${result.total}件`;
    document.querySelector("#page-indicator").textContent = `${result.page}ページ`;
    document.querySelector("#previous-page").disabled = page <= 1;
    document.querySelector("#next-page").disabled = !result.has_next;
  }

  document.querySelector("#previous-page").addEventListener("click", async () => {
    page = Math.max(1, page - 1);
    await loadReceipts();
  });
  document.querySelector("#next-page").addEventListener("click", async () => {
    page += 1;
    await loadReceipts();
  });
  fileInput.addEventListener("change", () => {
    const file = fileInput.files[0];
    document.querySelector(".file-picker strong").textContent = file?.name || "レシート画像を選ぶ";
  });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!fileInput.files.length) return;
    const button = form.querySelector("button[type=submit]");
    button.disabled = true;
    uploadStatus.textContent = "画像を登録しています…";
    uploadStatus.classList.remove("is-error");
    try {
      const data = new FormData();
      data.append("file", fileInput.files[0]);
      const result = await request("/api/receipts", { method: "POST", body: data });
      window.location.assign(`/receipts/${encodeURIComponent(result.receipt_id)}`);
    } catch (error) {
      uploadStatus.textContent = error.message;
      uploadStatus.classList.add("is-error");
      button.disabled = false;
    }
  });

  loadReceipts().catch((error) => {
    uploadStatus.textContent = error.message;
    uploadStatus.classList.add("is-error");
  });
})();
