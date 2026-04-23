document.addEventListener("DOMContentLoaded", () => {
  const startBtn = document.getElementById("start-btn");
  const stopBtn = document.getElementById("stop-btn");
  const refreshBtn = document.getElementById("refresh-btn");
  const clearBtn = document.getElementById("clear-btn");

  const statusMessage = document.getElementById("status-message");
  const totalCount = document.getElementById("total-count");
  const pendingImport = document.getElementById("pending-import");
  const lastScrape = document.getElementById("last-scrape");
  const lastImport = document.getElementById("last-import");
  const lastReason = document.getElementById("last-reason");
  const lastResult = document.getElementById("last-result");

  startBtn.addEventListener("click", startImport);
  stopBtn.addEventListener("click", stopImport);
  refreshBtn.addEventListener("click", loadStatus);
  clearBtn.addEventListener("click", clearCheckpoint);

  loadStatus();

  async function startImport() {
    setStatus("Starting X bookmarks import...");

    try {
      const result = await sendRuntimeMessage({
        action: "INIT_X_ORCHESTRATION_FROM_PAGE_BRIDGE",
        settings: {}
      });

      if (result?.status === "error") {
        throw new Error(result.error || "Failed to start import");
      }

      setStatus(`Started. Tab ${result.tabId || "opened"}.`);
      await loadStatus();
    } catch (error) {
      setStatus(`Error: ${error.message}`);
    }
  }

  async function stopImport() {
    setStatus("Stopping X bookmarks import...");

    try {
      const tabId = await findXBookmarksTabId();

      if (!tabId) {
        throw new Error("No X bookmarks tab found");
      }

      const result = await sendTabMessage(tabId, {
        action: "STOP_X_BOOKMARK_SCRAPE"
      });

      if (result?.status === "error") {
        throw new Error(result.error || "Stop failed");
      }

      setStatus("Stop requested.");
      await loadStatus();
    } catch (error) {
      setStatus(`Error: ${error.message}`);
    }
  }

  async function loadStatus() {
    try {
      const result = await sendRuntimeMessage({
        action: "GET_X_CHECKPOINT_STATUS"
      });

      if (result?.status === "error") {
        throw new Error(result.error || "Failed to load status");
      }

      totalCount.textContent = String(result.totalCount || 0);
      pendingImport.textContent = String(Boolean(result.pendingImport));
      lastScrape.textContent = result.lastScrapeDate || "-";
      lastImport.textContent = result.lastImportDate || "-";
      lastReason.textContent = result.lastFinalizeReason || "-";
      lastResult.textContent = result.lastImportResult
        ? JSON.stringify(result.lastImportResult)
        : "-";

      setStatus("Status refreshed.");
    } catch (error) {
      setStatus(`Error: ${error.message}`);
    }
  }

  async function clearCheckpoint() {
    setStatus("Clearing checkpoint...");

    try {
      const result = await sendRuntimeMessage({
        action: "CLEAR_X_CHECKPOINT"
      });

      if (result?.status === "error") {
        throw new Error(result.error || "Clear failed");
      }

      setStatus("Checkpoint cleared.");
      await loadStatus();
    } catch (error) {
      setStatus(`Error: ${error.message}`);
    }
  }

  function setStatus(text) {
    statusMessage.textContent = text;
  }

  function sendRuntimeMessage(message) {
    return new Promise((resolve, reject) => {
      chrome.runtime.sendMessage(message, (response) => {
        if (chrome.runtime.lastError) {
          reject(new Error(chrome.runtime.lastError.message));
          return;
        }
        resolve(response);
      });
    });
  }

  function sendTabMessage(tabId, message) {
    return new Promise((resolve, reject) => {
      chrome.tabs.sendMessage(tabId, message, (response) => {
        if (chrome.runtime.lastError) {
          reject(new Error(chrome.runtime.lastError.message));
          return;
        }
        resolve(response);
      });
    });
  }

  async function findXBookmarksTabId() {
    const tabs = await chrome.tabs.query({
      url: [
        "https://x.com/i/bookmarks*",
        "https://twitter.com/i/bookmarks*"
      ]
    });

    if (!tabs.length) return null;
    return tabs[0].id || null;
  }
});
