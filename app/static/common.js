(() => {
  const messages = {
    queued: "OCR待機中",
    running: "読み取り中",
    succeeded: "読み取り完了",
    no_candidates: "候補なし",
    ocr_failed: "読み取り失敗",
    extraction_failed: "候補抽出失敗",
    timeout: "時間切れ",
    interrupted: "中断",
    draft: "下書き",
    confirmed: "確認済み",
  };

  window.HouseAccount = {
    statusLabel(status) {
      return messages[status] || status || "状態不明";
    },
    async request(url, options = {}) {
      const response = await fetch(url, {
        ...options,
        headers: { Accept: "application/json", ...(options.headers || {}) },
      });
      let body = null;
      if (response.status !== 204) {
        body = await response.json().catch(() => null);
      }
      if (!response.ok) {
        const error = new Error(body?.error?.message || "通信に失敗しました。");
        error.status = response.status;
        error.code = body?.error?.code || "request_failed";
        error.fields = body?.error?.fields || {};
        throw error;
      }
      return body;
    },
    formatDate(value) {
      if (!value) return "";
      return new Intl.DateTimeFormat("ja-JP", {
        year: "numeric",
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      }).format(new Date(value));
    },
  };
})();
