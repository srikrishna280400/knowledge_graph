const API_URL = "http://127.0.0.1:12108/api/imports/push-urls";
const SOURCE_KEY = "linkedin_saved_post_hero";
const TARGET_URL = "https://www.linkedin.com/my-items/saved-posts/";

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  console.log("BG message", {
    action: message?.action,
    senderTabId: sender?.tab?.id ?? null,
    payload: message?.payload ?? null
  });

  if (!message?.action) return false;

  if (message.action === "PING_FROM_PAGE_BRIDGE") {
    sendResponse({ status: "bridge_ready", source_key: SOURCE_KEY });
    return false;
  }

  if (
    message.action === "INIT_LINKEDIN_ORCHESTRATION_FROM_PAGE_BRIDGE" ||
    message.action === "INIT_ORCHESTRATION"
  ) {
    handleInitOrchestration(message.settings || {})
      .then((result) => sendResponse({ status: "orchestration_started", ...result }))
      .catch((error) => {
        console.error("BG INIT error", error);
        sendResponse({ status: "error", error: error.message });
      });
    return true;
  }

  if (message.action === "SAVE_LINKEDIN_CHECKPOINT") {
    console.log("BG SAVE_LINKEDIN_CHECKPOINT", {
      postsCount: Array.isArray(message.payload?.posts) ? message.payload.posts.length : 0,
      senderTabId: sender?.tab?.id ?? null
    });

    saveLinkedInCheckpoint(message.payload?.posts || [], sender.tab?.id || null)
      .then((result) => {
        console.log("BG SAVE_LINKEDIN_CHECKPOINT result", result);
        sendResponse({ status: "checkpoint_saved", ...result });
      })
      .catch((error) => {
        console.error("BG SAVE_LINKEDIN_CHECKPOINT error", error);
        sendResponse({ status: "error", error: error.message });
      });
    return true;
  }

  if (message.action === "FINALIZE_LINKEDIN_CHECKPOINT") {
    const reason = message.payload?.reason || "manual_finalize";
    console.log("BG FINALIZE_LINKEDIN_CHECKPOINT", { reason });

    finalizePendingImport(reason)
      .then((result) => {
        console.log("BG FINALIZE_LINKEDIN_CHECKPOINT result", result);
        sendResponse({ status: "done", ...result });
      })
      .catch((error) => {
        console.error("BG FINALIZE_LINKEDIN_CHECKPOINT error", error);
        sendResponse({ status: "error", error: error.message });
      });
    return true;
  }

  if (message.action === "GET_LINKEDIN_CHECKPOINT_STATUS") {
    getCheckpointStatus()
      .then((result) => sendResponse({ status: "ok", ...result }))
      .catch((error) => {
        console.error("BG GET_LINKEDIN_CHECKPOINT_STATUS error", error);
        sendResponse({ status: "error", error: error.message });
      });
    return true;
  }

  if (message.action === "CLEAR_LINKEDIN_CHECKPOINT") {
    clearCheckpoint()
      .then(() => sendResponse({ status: "cleared" }))
      .catch((error) => {
        console.error("BG CLEAR_LINKEDIN_CHECKPOINT error", error);
        sendResponse({ status: "error", error: error.message });
      });
    return true;
  }

  return false;
});

chrome.tabs.onRemoved.addListener((tabId) => {
  getStorage(["linkedin_active_tab_id", "pending_import"])
    .then((stored) => {
      console.log("TAB CLOSED", { tabId, stored });

      if (tabId !== stored.linkedin_active_tab_id) return;
      if (!stored.pending_import) return;

      console.log("TAB CLOSED -> finalizePendingImport(tab_closed)");
      return finalizePendingImport("tab_closed");
    })
    .catch((error) => {
      const msg = error?.message || String(error);
      console.error("LinkedIn finalize on tab close failed message:", msg);
      console.error("LinkedIn finalize on tab close failed stack:", error?.stack || null);
      debugger;
    });
});

chrome.runtime.onStartup.addListener(() => {
  console.log("BG onStartup");
  finalizeIfPending("startup_resume").catch((error) => {
    console.error("LinkedIn startup resume finalize failed:", error);
  });
});

async function handleInitOrchestration(settings) {
  console.log("handleInitOrchestration start", { settings });

  const tabId = await getOrCreateSavedPostsTab();

  await setStorage({
    linkedin_active_tab_id: tabId,
  });

  await waitForTabReady(tabId);
  await waitForContentScript(tabId);

  chrome.tabs.sendMessage(
    tabId,
    {
      action: "START_SCRAPE",
      settings,
    },
    () => {
      if (chrome.runtime.lastError) {
        console.warn("START_SCRAPE send warning:", chrome.runtime.lastError.message);
        return;
      }
      console.log("START_SCRAPE sent to tab", tabId);
    }
  );

  return { tabId };
}

async function getOrCreateSavedPostsTab() {
  const tabs = await chrome.tabs.query({
    url: ["https://www.linkedin.com/my-items/saved-posts/*"],
  });

  if (tabs.length > 0 && tabs[0].id) {
    await chrome.tabs.update(tabs[0].id, { active: true });
    console.log("Reusing LinkedIn saved posts tab", tabs[0].id);
    return tabs[0].id;
  }

  const tab = await chrome.tabs.create({
    url: TARGET_URL,
    active: true,
  });

  if (!tab?.id) {
    throw new Error("Failed to create LinkedIn saved posts tab");
  }

  console.log("Created LinkedIn saved posts tab", tab.id);
  return tab.id;
}

function waitForTabReady(tabId, timeoutMs = 45000) {
  return new Promise((resolve, reject) => {
    const started = Date.now();

    const onUpdated = (updatedTabId, info) => {
      if (updatedTabId !== tabId) return;

      if (info.status === "complete") {
        chrome.tabs.onUpdated.removeListener(onUpdated);
        console.log("waitForTabReady complete", { tabId });
        resolve();
        return;
      }

      if (Date.now() - started > timeoutMs) {
        chrome.tabs.onUpdated.removeListener(onUpdated);
        reject(new Error("Timed out waiting for LinkedIn tab to load"));
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
        console.log("waitForTabReady already complete", { tabId });
        resolve();
      }
    });
  });
}

async function waitForContentScript(tabId, maxRetries = 20, delayMs = 1000) {
  for (let i = 0; i < maxRetries; i++) {
    try {
      const response = await sendMessageToTab(tabId, { action: "PING" });
      console.log("waitForContentScript success", { tabId, response, attempt: i + 1 });
      return;
    } catch (error) {
      console.warn("waitForContentScript retry", { tabId, attempt: i + 1, error: error.message });
      await delay(delayMs);
    }
  }

  throw new Error("Failed to connect to LinkedIn content script after retries");
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

async function saveLinkedInCheckpoint(posts, tabId = null) {
  console.log("saveLinkedInCheckpoint start", {
    incomingPostsCount: Array.isArray(posts) ? posts.length : 0,
    tabId
  });

  const cleanedPosts = normalizePosts(posts);
  const stored = await getStorage([
    "scraped_posts",
    "total_count",
    "linkedin_active_tab_id",
  ]);

  const merged = new Map();

  for (const post of Array.isArray(stored.scraped_posts) ? stored.scraped_posts : []) {
    const key = post?.post_urn || post?.post_url;
    if (!key) continue;
    merged.set(key, post);
  }

  for (const post of cleanedPosts) {
    const key = post.post_urn || post.post_url;
    if (!key) continue;
    merged.set(key, post);
  }

  const mergedPosts = Array.from(merged.values());
  console.log("saveLinkedInCheckpoint merged", {
    mergedCount: mergedPosts.length,
    firstPost: mergedPosts[0] || null
  });

  const previousCount = Number(stored.total_count || 0);
  const totalCount = mergedPosts.length;

  await setStorage({
    scraped_posts: mergedPosts,
    total_count: totalCount,
    last_scrape_date: new Date().toISOString(),
    pending_import: totalCount > 0,
    pending_source_key: SOURCE_KEY,
    linkedin_active_tab_id: tabId ?? stored.linkedin_active_tab_id ?? null,
  });

  console.log("saveLinkedInCheckpoint stored", {
    totalCount,
    activeTabId: tabId ?? stored.linkedin_active_tab_id ?? null
  });

  return {
    totalCount,
    newCount: Math.max(0, totalCount - previousCount),
  };
}

async function finalizeIfPending(reason = "resume") {
  const stored = await getStorage(["pending_import"]);
  console.log("finalizeIfPending", { reason, pendingImport: stored.pending_import });

  if (!stored.pending_import) {
    return { uploaded: false, reason: "nothing_pending", count: 0 };
  }
  return finalizePendingImport(reason);
}

async function finalizePendingImport(reason = "manual_finalize") {
  console.log("FINALIZE called", { reason });

  const stored = await getStorage([
    "scraped_posts",
    "pending_import",
    "linkedin_active_tab_id",
  ]);

  console.log("FINALIZE storage", {
    pendingImport: stored.pending_import,
    activeTabId: stored.linkedin_active_tab_id,
    scrapedPostsCount: Array.isArray(stored.scraped_posts) ? stored.scraped_posts.length : 0
  });

  const posts = Array.isArray(stored.scraped_posts) ? stored.scraped_posts : [];
  const urls = posts
    .map((post) => {
      if (!post) return null;
      if (typeof post.post_url === "string" && post.post_url.trim()) return post.post_url.trim();
      if (typeof post.url === "string" && post.url.trim()) return post.url.trim();
      return null;
    })
    .filter(Boolean);

  console.log("FINALIZE urls built", {
    urlsLength: urls.length,
    firstUrl: urls[0] || null
  });

  if (urls.length === 0) {
    await setStorage({
      pending_import: false,
      scraped_posts: [],
      total_count: 0,
      linkedin_active_tab_id: null,
    });

    console.log("FINALIZE no urls -> cleared storage");

    return {
      uploaded: false,
      reason: "no_urls",
      count: 0,
    };
  }

  const label = `linkedin_saved_${Date.now()}`;

  console.log("FINALIZE start", {
    reason,
    urlsLength: urls.length,
    firstUrl: urls[0] || null
  });

  const resp = await fetch(API_URL, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      source_key: SOURCE_KEY,
      label,
      payload: { urls },
    }),
  });

  console.log("FINALIZE response", {
    status: resp.status,
    ok: resp.ok
  });

  const text = await resp.text();
  console.log("FINALIZE body", text);

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
    pending_import: false,
    pending_source_key: null,
    scraped_posts: [],
    total_count: 0,
    last_import_date: new Date().toISOString(),
    last_finalize_reason: reason,
    last_import_result: apiResult,
    linkedin_active_tab_id: null,
  });

  console.log("FINALIZE success", {
    uploaded: true,
    count: urls.length,
    reason
  });

  return {
    uploaded: true,
    count: urls.length,
    apiResult,
  };
}

async function getCheckpointStatus() {
  const stored = await getStorage([
    "scraped_posts",
    "total_count",
    "pending_import",
    "last_scrape_date",
    "last_import_date",
    "last_finalize_reason",
    "last_import_result",
  ]);

  return {
    totalCount: Number(stored.total_count || 0),
    pendingImport: Boolean(stored.pending_import),
    lastScrapeDate: stored.last_scrape_date || null,
    lastImportDate: stored.last_import_date || null,
    lastFinalizeReason: stored.last_finalize_reason || null,
    lastImportResult: stored.last_import_result || null,
    hasPosts: Array.isArray(stored.scraped_posts) && stored.scraped_posts.length > 0,
  };
}

async function clearCheckpoint() {
  await setStorage({
    scraped_posts: [],
    total_count: 0,
    pending_import: false,
    pending_source_key: null,
    linkedin_active_tab_id: null,
  });

  console.log("clearCheckpoint done");
}

function normalizePosts(posts) {
  if (!Array.isArray(posts)) return [];

  return posts.filter((post) => {
    if (!post || typeof post !== "object") return false;
    if (typeof post.post_url === "string" && post.post_url.trim()) return true;
    if (typeof post.url === "string" && post.url.trim()) return true;
    return false;
  });
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
