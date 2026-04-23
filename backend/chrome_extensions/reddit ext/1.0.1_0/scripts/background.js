// scripts/background.js

const API_URL    = "http://127.0.0.1:8000/api/imports/push-urls";
const SOURCE_KEY = "reddit_saved_downloader";
const TARGET_SAVED_URL = "https://www.reddit.com/user/me/saved/";

let scrapingState = {
  inProgress: false,
  settings:   null,
  tabId:      null,
};

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  console.log("BG message:", message?.action, message);

  if (!message?.action) return false;

  if (message.action === "PING_FROM_PAGE_BRIDGE") {
    sendResponse({ status: "bridge_ready", source_key: SOURCE_KEY });
    return false;
  }

  if (message.action === "INIT_ORCHESTRATION") {
    handleInitOrchestration(message.settings || {})
      .then((result) => {
        console.log("INIT_ORCHESTRATION success", result);
        sendResponse({ status: "orchestration_started", ...result });
      })
      .catch((error) => {
        console.error("INIT_ORCHESTRATION error:", error);
        resetScrapingState();
        safeSendMessage({
          action:  "DOWNLOAD_ERROR",
          payload: { error: error.message || String(error) },
        });
        sendResponse({ status: "error", error: error.message || String(error) });
      });
    return true;
  }

  if (message.action === "PROCESS_DOWNLOAD") {
    console.log(
      "BG PROCESS_DOWNLOAD received",
      Array.isArray(message.payload?.posts) ? message.payload.posts.length : 0
    );

    handleBackendUpload(message.payload)
      .then((result) => {
        console.log("BG upload success", result);
        resetScrapingState();
        sendResponse({ status: "done", ...result });
      })
      .catch((error) => {
        console.error("PROCESS_DOWNLOAD error:", error);
        resetScrapingState();
        safeSendMessage({
          action:  "DOWNLOAD_ERROR",
          payload: { error: error.message || String(error) },
        });
        sendResponse({ status: "error", error: error.message || String(error) });
      });
    return true;
  }

  return false;
});

function resetScrapingState() {
  scrapingState = { inProgress: false, settings: null, tabId: null };
}

function safeSendMessage(message) {
  chrome.runtime.sendMessage(message, () => {
    const err = chrome.runtime.lastError;
    if (!err) return;
    const msg = err.message || "";
    if (
      msg.includes("Receiving end does not exist") ||
      msg.includes("The message port closed before a response was received")
    ) return;
    console.error("safeSendMessage unexpected error:", msg);
  });
}

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// Validates that a URL points to a Reddit saved page — used only for tab routing,
// NOT for post URL validation (post URLs are checked via normalizeRedditPostUrl).
function isRedditSavedUrl(url) {
  try {
    const u = new URL(url);
    return (
      /(^|\.)reddit\.com$/i.test(u.hostname) &&
      /\/user\/[^/]+\/saved\/?$/i.test(u.pathname)
    );
  } catch {
    return false;
  }
}

// Normalizes a Reddit saved-page / tab URL (no /comments/ requirement).
function normalizeRedditTabUrl(url) {
  if (!url || typeof url !== "string") return "";
  try {
    const u = new URL(url);
    if (!/^https?:$/i.test(u.protocol))            return "";
    if (!/(^|\.)reddit\.com$/i.test(u.hostname))   return "";

    u.protocol = "https:";
    u.hostname = "www.reddit.com";
    u.hash   = "";
    u.search = "";
    return u.toString().replace(/\/+$/, "");
  } catch {
    return "";
  }
}

// Normalizes a Reddit post/comment URL.
// Returns "" for anything that is not a valid /comments/ URL.
function normalizeRedditPostUrl(url) {
  if (!url || typeof url !== "string") return "";
  try {
    const u = new URL(url);
    if (!/^https?:$/i.test(u.protocol))            return "";
    if (!/(^|\.)reddit\.com$/i.test(u.hostname))   return "";
    if (!u.pathname.includes("/comments/"))        return "";

    u.protocol = "https:";
    u.hostname = "www.reddit.com";
    u.hash   = "";
    u.search = "";
    return u.toString().replace(/\/+$/, "");
  } catch {
    return "";
  }
}

// Extracts a normalized URL from a post object sent by content.js.
// content.js always sends { url, permalink, subreddit, type, title }.
// Both `permalink` and `url` contain the same value; prefer `permalink`.
function extractUrl(post) {
  if (!post) return "";

  if (typeof post === "string") {
    return normalizeRedditPostUrl(post.trim());
  }

  if (typeof post.permalink === "string" && post.permalink.trim()) {
    return normalizeRedditPostUrl(post.permalink.trim());
  }

  if (typeof post.url === "string" && post.url.trim()) {
    return normalizeRedditPostUrl(post.url.trim());
  }

  return "";
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
        reject(new Error("Timed out waiting for tab to finish loading"));
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

async function waitForContentScript(tabId, maxRetries = 20, delayMs = 1000) {
  for (let i = 0; i < maxRetries; i++) {
    try {
      const response = await sendMessageToTab(tabId, { action: "PING" });
      console.log("Content script ping response:", response);
      return;
    } catch (err) {
      console.warn(`PING retry ${i + 1}/${maxRetries} failed:`, err.message);
      await delay(delayMs);
    }
  }
  throw new Error("Failed to connect to content script after retries");
}

async function getOrCreateSavedTab() {
  const tabs      = await chrome.tabs.query({ active: true, currentWindow: true });
  const activeTab = tabs[0];

  if (activeTab?.id && activeTab.url && isRedditSavedUrl(activeTab.url)) {
    console.log("Reusing active Reddit saved tab:", activeTab.id);
    await chrome.tabs.update(activeTab.id, { active: true });
    return activeTab.id;
  }

  const newTab = await chrome.tabs.create({ url: TARGET_SAVED_URL, active: true });
  console.log("Created Reddit saved tab:", newTab.id);
  return newTab.id;
}

async function handleInitOrchestration(settings) {
  if (scrapingState.inProgress) {
    throw new Error("A scrape is already in progress.");
  }

  const normalizedSettings = { ...(settings || {}), limitAll: true, limit: "" };

  scrapingState.inProgress = true;
  scrapingState.settings   = normalizedSettings;

  const tabId = await getOrCreateSavedTab();
  scrapingState.tabId = tabId;

  await waitForTabReady(tabId);
  await waitForContentScript(tabId);

  const response = await sendMessageToTab(tabId, {
    action:   "START_SCRAPE",
    settings: scrapingState.settings,
  });

  console.log("START_SCRAPE ack:", response);
  return { tabId };
}

async function handleBackendUpload(payload) {
  const posts = Array.isArray(payload?.posts) ? payload.posts : [];

  console.log("Raw posts count:", posts.length);
  console.log("First raw post:", posts[0]);

  // Deduplicate and validate — only /comments/ URLs pass normalizeRedditPostUrl
  const urls = Array.from(
    new Set(
      posts
        .map(extractUrl)
        .filter((url) => Boolean(url))
    )
  );

  console.log("Normalized URLs count:", urls.length);
  console.log("First few URLs:", urls.slice(0, 5));
  console.log("POSTing to", API_URL);

  if (!urls.length) {
    throw new Error("No valid Reddit URLs found to import.");
  }

  const label = `reddit_saved_${Date.now()}`;

  const resp = await fetch(API_URL, {
    method:  "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      source_key: SOURCE_KEY,
      label,
      payload: { urls },
    }),
  });

  const text = await resp.text();
  console.log("API status:", resp.status, text);

  if (!resp.ok) {
    throw new Error(`API upload failed: ${resp.status} ${text}`);
  }

  let apiResult;
  try {
    apiResult = JSON.parse(text);
  } catch {
    apiResult = { ok: true, raw: text };
  }

  safeSendMessage({
    action:  "DOWNLOAD_COMPLETE",
    payload: { uploaded: true, count: urls.length, apiResult },
  });

  return { uploaded: true, count: urls.length, apiResult };
}