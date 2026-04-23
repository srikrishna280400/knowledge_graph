const API_URL = "http://127.0.0.1:12108/api/imports/push-urls";
const SOURCE_KEY = "x_bookmarks";
const TARGET_URL = "https://x.com/i/bookmarks";

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!message?.action) return false;

  if (message.action === "PING_FROM_PAGE_BRIDGE") {
    sendResponse({ status: "bridge_ready", source_key: SOURCE_KEY });
    return false;
  }

  if (
    message.action === "INIT_X_ORCHESTRATION_FROM_PAGE_BRIDGE" ||
    message.action === "INIT_ORCHESTRATION"
  ) {
    handleInitOrchestration(message.settings || {})
      .then((result) => sendResponse({ status: "orchestration_started", ...result }))
      .catch((error) => sendResponse({ status: "error", error: error.message }));
    return true;
  }

  if (message.action === "SAVE_X_CHECKPOINT") {
    saveXCheckpoint(message.payload?.urls || [], sender.tab?.id || null)
      .then((result) => sendResponse({ status: "checkpoint_saved", ...result }))
      .catch((error) => sendResponse({ status: "error", error: error.message }));
    return true;
  }

  if (message.action === "FINALIZE_X_CHECKPOINT") {
    finalizePendingImport(message.payload?.reason || "manual_finalize")
      .then((result) => sendResponse({ status: "done", ...result }))
      .catch((error) => sendResponse({ status: "error", error: error.message }));
    return true;
  }

  if (message.action === "GET_X_CHECKPOINT_STATUS") {
    getCheckpointStatus()
      .then((result) => sendResponse({ status: "ok", ...result }))
      .catch((error) => sendResponse({ status: "error", error: error.message }));
    return true;
  }

  if (message.action === "CLEAR_X_CHECKPOINT") {
    clearCheckpoint()
      .then(() => sendResponse({ status: "cleared" }))
      .catch((error) => sendResponse({ status: "error", error: error.message }));
    return true;
  }

  return false;
});

chrome.tabs.onRemoved.addListener((tabId) => {
  getStorage(["x_active_tab_id", "x_pending_import"])
    .then((stored) => {
      if (tabId !== stored.x_active_tab_id) return;
      if (!stored.x_pending_import) return;

      return finalizePendingImport("tab_closed");
    })
    .catch((error) => {
      console.error("X finalize on tab close failed:", error);
    });
});

chrome.runtime.onStartup.addListener(() => {
  finalizeIfPending("startup_resume").catch((error) => {
    console.error("X startup resume finalize failed:", error);
  });
});

async function handleInitOrchestration(settings) {
  const tabId = await getOrCreateBookmarksTab();

  await setStorage({
    x_active_tab_id: tabId
  });

  await waitForTabReady(tabId);
  await waitForContentScript(tabId);

  const startResponse = await sendMessageToTab(tabId, {
    action: "START_X_BOOKMARK_SCRAPE",
    settings
  });

  if (!startResponse) {
    throw new Error("No response from X content script after START_X_BOOKMARK_SCRAPE");
  }

  if (startResponse.status === "error") {
    throw new Error(startResponse.error || "X content script failed to start");
  }

  return { tabId, startResponse };
}

async function getOrCreateBookmarksTab() {
  const tabs = await chrome.tabs.query({
    url: [
      "https://x.com/i/bookmarks*",
      "https://twitter.com/i/bookmarks*"
    ]
  });

  if (tabs.length > 0 && tabs[0].id) {
    await chrome.tabs.update(tabs[0].id, { active: true });
    return tabs[0].id;
  }

  const tab = await chrome.tabs.create({
    url: TARGET_URL,
    active: true
  });

  if (!tab?.id) {
    throw new Error("Failed to create X bookmarks tab");
  }

  return tab.id;
}

function waitForTabReady(tabId, timeoutMs = 45000) {
  return new Promise((resolve, reject) => {
    const started = Date.now();

    const onUpdated = (updatedTabId, info) => {
      if (updatedTabId !== tabId) return;

      if (info.status === "complete") {
        chrome.tabs.onUpdated.removeListener(onUpdated);
        resolve();
        return;
      }

      if (Date.now() - started > timeoutMs) {
        chrome.tabs.onUpdated.removeListener(onUpdated);
        reject(new Error("Timed out waiting for X bookmarks tab to load"));
      }
    };

    chrome.tabs.onUpdated.addListener(onUpdated);

    chrome.tabs.get(tabId, (tab) => {
      if (chrome.runtime.lastError) {
        chrome.tabs.onUpdated.removeListener(onUpdated);
        reject(new Error(chrome.runtime.lastError.message));
        return;
      }

      if (tab?.status === "complete") {
        chrome.tabs.onUpdated.removeListener(onUpdated);
        resolve();
      }
    });
  });
}

async function waitForContentScript(tabId, maxRetries = 25, delayMs = 1000) {
  for (let i = 0; i < maxRetries; i++) {
    try {
      await sendMessageToTab(tabId, { action: "PING" });
      return;
    } catch {
      await delay(delayMs);
    }
  }

  throw new Error("Failed to connect to X content script after retries");
}

function sendMessageToTab(tabId, message) {
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

async function saveXCheckpoint(urls, tabId = null) {
  const cleanedUrls = normalizeUrls(urls);
  const stored = await getStorage([
    "x_scraped_urls",
    "x_total_count",
    "x_active_tab_id"
  ]);

  const merged = new Set(Array.isArray(stored.x_scraped_urls) ? stored.x_scraped_urls : []);
  for (const url of cleanedUrls) {
    merged.add(url);
  }

  const mergedUrls = Array.from(merged.values());
  const previousCount = Number(stored.x_total_count || 0);
  const totalCount = mergedUrls.length;

  await setStorage({
    x_scraped_urls: mergedUrls,
    x_total_count: totalCount,
    x_last_scrape_date: new Date().toISOString(),
    x_pending_import: totalCount > 0,
    x_pending_source_key: SOURCE_KEY,
    x_active_tab_id: tabId ?? stored.x_active_tab_id ?? null
  });

  return {
    totalCount,
    newCount: Math.max(0, totalCount - previousCount)
  };
}

async function finalizeIfPending(reason = "resume") {
  const stored = await getStorage(["x_pending_import"]);
  if (!stored.x_pending_import) {
    return { uploaded: false, reason: "nothing_pending", count: 0 };
  }
  return finalizePendingImport(reason);
}

async function finalizePendingImport(reason = "manual_finalize") {
  const stored = await getStorage([
    "x_scraped_urls",
    "x_pending_import",
    "x_active_tab_id"
  ]);

  const urls = normalizeUrls(Array.isArray(stored.x_scraped_urls) ? stored.x_scraped_urls : []);

  if (urls.length === 0) {
    await setStorage({
      x_pending_import: false,
      x_scraped_urls: [],
      x_total_count: 0,
      x_active_tab_id: null
    });

    return {
      uploaded: false,
      reason: "no_urls",
      count: 0
    };
  }

  const label = `x_bookmarks_${Date.now()}`;

  const resp = await fetch(API_URL, {
    method: "POST",
    headers: {
      "Content-Type": "application/json"
    },
    body: JSON.stringify({
      source_key: SOURCE_KEY,
      label,
      payload: { urls }
    })
  });

  const text = await resp.text();

  if (!resp.ok) {
    throw new Error(`API upload failed: ${resp.status} ${text}`);
  }

  let apiResult;
  try {
    apiResult = JSON.parse(text);
  } catch {
    apiResult = { ok: true, raw: text };
  }

  await setStorage({
    x_pending_import: false,
    x_pending_source_key: null,
    x_scraped_urls: [],
    x_total_count: 0,
    x_last_import_date: new Date().toISOString(),
    x_last_finalize_reason: reason,
    x_last_import_result: apiResult,
    x_active_tab_id: null
  });

  return {
    uploaded: true,
    count: urls.length,
    apiResult
  };
}

async function getCheckpointStatus() {
  const stored = await getStorage([
    "x_scraped_urls",
    "x_total_count",
    "x_pending_import",
    "x_last_scrape_date",
    "x_last_import_date",
    "x_last_finalize_reason",
    "x_last_import_result"
  ]);

  return {
    totalCount: Number(stored.x_total_count || 0),
    pendingImport: Boolean(stored.x_pending_import),
    lastScrapeDate: stored.x_last_scrape_date || null,
    lastImportDate: stored.x_last_import_date || null,
    lastFinalizeReason: stored.x_last_finalize_reason || null,
    lastImportResult: stored.x_last_import_result || null,
    hasUrls: Array.isArray(stored.x_scraped_urls) && stored.x_scraped_urls.length > 0
  };
}

async function clearCheckpoint() {
  await setStorage({
    x_scraped_urls: [],
    x_total_count: 0,
    x_pending_import: false,
    x_pending_source_key: null,
    x_active_tab_id: null
  });
}

function normalizeUrls(urls) {
  if (!Array.isArray(urls)) return [];

  const cleaned = new Set();

  for (const raw of urls) {
    if (typeof raw !== "string" || !raw.trim()) continue;

    try {
      const u = new URL(raw.trim());
      if (!/^https?:$/.test(u.protocol)) continue;

      u.search = "";
      u.hash = "";

      if (u.hostname === "twitter.com" || u.hostname === "www.twitter.com") {
        u.hostname = "x.com";
      }

      cleaned.add(u.toString());
    } catch {
      continue;
    }
  }

  return Array.from(cleaned.values());
}

function getStorage(keys) {
  return new Promise((resolve, reject) => {
    chrome.storage.local.get(keys, (result) => {
      if (chrome.runtime.lastError) {
        reject(new Error(chrome.runtime.lastError.message));
        return;
      }
      resolve(result || {});
    });
  });
}

function setStorage(obj) {
  return new Promise((resolve, reject) => {
    chrome.storage.local.set(obj, () => {
      if (chrome.runtime.lastError) {
        reject(new Error(chrome.runtime.lastError.message));
        return;
      }
      resolve();
    });
  });
}

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
