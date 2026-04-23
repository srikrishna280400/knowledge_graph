// scripts/content.js
console.log("CONTENT BUILD 2026-04-04 NEW-REDDIT-NATIVE");
console.log("Reddit Downloader Content Script Loaded on:", window.location.href);

const SCRAPE_STATE = {
  running: false,
  cancelled: false,
};

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  console.log("CONTENT message:", message?.action, message);

  if (message.action === "PING") {
    sendResponse({ status: "pong" });
    return true;
  }

  if (message.action === "START_SCRAPE") {
    startDomScraping(message.settings || {});
    sendResponse({ status: "started" });
    return true;
  }

  if (message.action === "CONTINUE_SCRAPE") {
    sendResponse({ status: "not_used" });
    return true;
  }

  return false;
});

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

function notifyProgress(processed, total, status) {
  safeSendMessage({
    action: "UPDATE_PROGRESS",
    payload: { processed, total, status },
  });
}

function updateOverlay(text) {
  let overlay = document.getElementById("reddit-downloader-overlay");

  if (!overlay) {
    overlay = document.createElement("div");
    overlay.id = "reddit-downloader-overlay";
    overlay.style.cssText = `
      position: fixed;
      top: 50%;
      left: 50%;
      transform: translate(-50%, -50%);
      background: rgba(0, 0, 0, 0.88);
      color: white;
      padding: 24px 48px;
      border-radius: 12px;
      z-index: 2147483647;
      text-align: center;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      box-shadow: 0 10px 30px rgba(0,0,0,0.35);
      pointer-events: none;
      min-width: 300px;
    `;

    const spinner = document.createElement("div");
    spinner.style.cssText = `
      border: 4px solid rgba(255,255,255,0.25);
      border-radius: 50%;
      border-top: 4px solid #FF4500;
      width: 32px;
      height: 32px;
      margin: 0 auto 12px auto;
      animation: reddit-downloader-spin 1s linear infinite;
    `;

    if (!document.getElementById("reddit-downloader-style")) {
      const styleSheet = document.createElement("style");
      styleSheet.id = "reddit-downloader-style";
      styleSheet.innerText = `
        @keyframes reddit-downloader-spin {
          0% { transform: rotate(0deg); }
          100% { transform: rotate(360deg); }
        }
      `;
      document.head.appendChild(styleSheet);
    }

    overlay.appendChild(spinner);

    const msg = document.createElement("div");
    msg.id = "reddit-downloader-msg";
    msg.style.fontSize = "16px";
    msg.style.fontWeight = "600";
    overlay.appendChild(msg);

    const subMsg = document.createElement("div");
    subMsg.style.fontSize = "12px";
    subMsg.style.marginTop = "8px";
    subMsg.style.opacity = "0.82";
    subMsg.innerText = "Scrolling saved feed — do not close this tab.";
    overlay.appendChild(subMsg);

    document.body.appendChild(overlay);
  }

  const msgEl = document.getElementById("reddit-downloader-msg");
  if (msgEl) msgEl.innerText = text;
}

function removeOverlay() {
  const overlay = document.getElementById("reddit-downloader-overlay");
  if (overlay) overlay.remove();
}

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function isSavedPageUrl(url = window.location.href) {
  try {
    const u = new URL(url, window.location.origin);
    const hostOk = /(^|\.)reddit\.com$/i.test(u.hostname);
    const pathOk = /\/user\/[^/]+\/saved\/?$/i.test(u.pathname);
    return hostOk && pathOk;
  } catch {
    return false;
  }
}

function normalizeRedditUrl(url) {
  if (!url || typeof url !== "string") return "";
  try {
    const u = new URL(url, window.location.origin);
    if (!/^https?:$/i.test(u.protocol)) return "";
    if (!/(^|\.)reddit\.com$/i.test(u.hostname)) return "";
    if (!u.pathname.includes("/comments/")) return "";

    u.protocol = "https:";
    u.hostname = "www.reddit.com";
    u.hash = "";
    u.search = "";

    return u.toString().replace(/\/+$/, "");
  } catch {
    return "";
  }
}

function parseSubredditFromUrl(url) {
  try {
    const u = new URL(url, window.location.origin);
    const match = u.pathname.match(/^\/r\/([^/]+)\/comments\//i);
    return match ? match[1].toLowerCase() : "";
  } catch {
    return "";
  }
}

// New Reddit comment permalinks look like:
//   /r/sub/comments/postId/post_title/commentId/   ← 6 path segments
// Old Reddit used /comment/ (singular) which no longer applies.
function isCommentPermalink(url) {
  try {
    const u = new URL(url, window.location.origin);
    const parts = u.pathname.split("/").filter(Boolean);
    // r / sub / comments / postId / title / commentId  → length 6
    return parts.length >= 6 && parts[2] === "comments";
  } catch {
    return false;
  }
}

function getSubredditFilters(settings) {
  if (!settings?.subreddits) return [];
  return String(settings.subreddits)
    .split(",")
    .map((s) => s.trim().toLowerCase().replace(/^r\//, ""))
    .filter(Boolean);
}

function getFilterMode(settings) {
  return settings?.filterMode === "exclude" ? "exclude" : "include";
}

function getLimit(settings) {
  if (settings?.limitAll) return Infinity;
  const raw = String(settings?.limit ?? "").trim();
  if (!raw) return Infinity;
  const parsed = parseInt(raw, 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : Infinity;
}

function passesSubredditFilter(subreddit, filters, filterMode) {
  if (!filters.length) return true;
  if (!subreddit) return filterMode !== "include";
  const included = filters.includes(subreddit);
  return filterMode === "include" ? included : !included;
}

// ─── New Reddit (shreddit) DOM queries ──────────────────────────────────────
// New Reddit renders posts as <shreddit-post permalink="/r/..."> custom elements
// and saved comments as <shreddit-comment permalink="/r/..."> custom elements.
// Their data lives in element *attributes* — not in shadow DOM text nodes —
// so regular querySelectorAll works without any shadow-piercing.

function getShredditPosts() {
  return Array.from(document.querySelectorAll("shreddit-post[permalink]"));
}

function getShredditComments() {
  return Array.from(document.querySelectorAll("shreddit-comment[permalink]"));
}

// Fallback: plain <a> tags — catches anything the shreddit selectors miss
// (e.g. hybrid/transitional Reddit rendering or future layout changes).
function getFallbackAnchors() {
  return Array.from(document.querySelectorAll('a[href*="/comments/"]'));
}

function countCandidates() {
  const shredditCount = getShredditPosts().length + getShredditComments().length;
  return shredditCount > 0 ? shredditCount : getFallbackAnchors().length;
}

// ─── Title extraction ────────────────────────────────────────────────────────

function titleFromShredditElement(el) {
  // shreddit-post and shreddit-comment both expose post-title as an attribute
  return (
    el.getAttribute("post-title") ||
    el.getAttribute("aria-label") ||
    ""
  ).trim().slice(0, 300);
}

function titleFromAnchor(anchor) {
  const direct = (anchor.textContent || "").trim();
  if (direct.length >= 6) return direct.slice(0, 300);

  // Walk up to the nearest shreddit container and read its attribute
  const container =
    anchor.closest("shreddit-post") ||
    anchor.closest("shreddit-comment") ||
    anchor.closest("faceplate-tracker");

  if (container) {
    const attrTitle = container.getAttribute("post-title");
    if (attrTitle) return attrTitle.slice(0, 300);

    const lines = (container.innerText || "")
      .split("\n")
      .map((s) => s.trim())
      .filter(Boolean);
    if (lines.length) return lines.slice(0, 3).join(" — ").slice(0, 300);
  }

  return "";
}

// ─── Main collection pass ────────────────────────────────────────────────────

function collectPostsFromDom(collectedMap, settings) {
  const filters    = getSubredditFilters(settings);
  const filterMode = getFilterMode(settings);
  const limit      = getLimit(settings);
  let added = 0;

  // 1. shreddit-post elements (saved posts on new Reddit)
  for (const el of getShredditPosts()) {
    if (collectedMap.size >= limit) break;

    const normalized = normalizeRedditUrl(el.getAttribute("permalink") || "");
    if (!normalized || collectedMap.has(normalized)) continue;

    const subreddit =
      (el.getAttribute("subreddit-prefixed-name") || "")
        .replace(/^r\//i, "")
        .toLowerCase() || parseSubredditFromUrl(normalized);

    if (!passesSubredditFilter(subreddit, filters, filterMode)) continue;

    collectedMap.set(normalized, {
      url:       normalized,
      permalink: normalized,
      subreddit,
      type:  isCommentPermalink(normalized) ? "comment" : "post",
      title: titleFromShredditElement(el),
    });
    added++;
  }

  // 2. shreddit-comment elements (saved comments on new Reddit)
  for (const el of getShredditComments()) {
    if (collectedMap.size >= limit) break;

    const normalized = normalizeRedditUrl(el.getAttribute("permalink") || "");
    if (!normalized || collectedMap.has(normalized)) continue;

    const subreddit = parseSubredditFromUrl(normalized);
    if (!passesSubredditFilter(subreddit, filters, filterMode)) continue;

    collectedMap.set(normalized, {
      url:       normalized,
      permalink: normalized,
      subreddit,
      type:  "comment",
      title: titleFromShredditElement(el),
    });
    added++;
  }

  // 3. Fallback anchor scan — only runs if shreddit elements found nothing
  if (getShredditPosts().length === 0 && getShredditComments().length === 0) {
    for (const anchor of getFallbackAnchors()) {
      if (collectedMap.size >= limit) break;

      const normalized = normalizeRedditUrl(anchor.href);
      if (!normalized || collectedMap.has(normalized)) continue;

      const subreddit = parseSubredditFromUrl(normalized);
      if (!passesSubredditFilter(subreddit, filters, filterMode)) continue;

      collectedMap.set(normalized, {
        url:       normalized,
        permalink: normalized,
        subreddit,
        type:  isCommentPermalink(normalized) ? "comment" : "post",
        title: titleFromAnchor(anchor),
      });
      added++;
    }
  }

  return added;
}

// ─── Wait helpers ────────────────────────────────────────────────────────────

async function waitForInitialContent(timeoutMs = 30000) {
  const started = Date.now();

  while (Date.now() - started < timeoutMs) {
    if (getShredditPosts().length > 0 || getShredditComments().length > 0) return true;
    if (getFallbackAnchors().length > 0) return true;

    // New Reddit empty-saved-list states
    const bodyText = (document.body?.innerText || "").toLowerCase();
    const emptySignals = [
      "you haven't saved",
      "nothing saved yet",
      "nothing to see here",
      "save posts",          // "Save posts, comments, and links" CTA
      "hmm...",
    ];
    if (emptySignals.some((s) => bodyText.includes(s))) return true;

    await delay(500);
  }

  return false;
}

async function waitForMoreContent(prevCount, prevHeight, timeoutMs = 7000) {
  const started = Date.now();
  let stableChecks = 0;

  while (Date.now() - started < timeoutMs) {
    await delay(400);

    const nextCount  = countCandidates();
    const nextHeight = document.body.scrollHeight;

    if (nextCount > prevCount || nextHeight > prevHeight) return true;

    stableChecks++;
    if (stableChecks % 4 === 0) {
      window.scrollBy(0, Math.max(Math.floor(window.innerHeight * 0.9), 900));
    }
  }

  return false;
}

// ─── Core scrape loop ────────────────────────────────────────────────────────

function isNearPageBottom(threshold = 24) {
  return window.innerHeight + window.scrollY >= document.body.scrollHeight - threshold;
}

function getScrollSnapshot() {
  return {
    y: window.scrollY,
    height: document.body.scrollHeight,
    candidates: countCandidates(),
  };
}

async function scrapeInfiniteSavedFeed(settings) {
  const limit = getLimit(settings);
  const collected = new Map();

  let idleRounds = 0;
  let bottomStuckRounds = 0;
  let rounds = 0;

  window.scrollTo(0, 0);
  await delay(1200);

  while (true) {
    rounds++;

    collectPostsFromDom(collected, settings);

    const statusText = Number.isFinite(limit)
      ? `Collected ${collected.size} / ${limit} saved items...`
      : `Collected ${collected.size} saved items...`;

    notifyProgress(collected.size, Number.isFinite(limit) ? limit : 0, statusText);
    updateOverlay(statusText);

    const before = getScrollSnapshot();
    const beforeCollected = collected.size;

    console.log("[SCRAPE]", {
      round: rounds,
      collected: collected.size,
      shredditPosts: getShredditPosts().length,
      shredditComments: getShredditComments().length,
      fallbackAnchors: getFallbackAnchors().length,
      scrollY: before.y,
      scrollHeight: before.height,
      idleRounds,
      bottomStuckRounds,
      nearBottom: isNearPageBottom(),
    });

    if (collected.size >= limit) break;

    window.scrollTo({ top: document.body.scrollHeight, behavior: "auto" });
    await delay(1200);

    let changed = await waitForMoreContent(before.candidates, before.height, 5000);
    collectPostsFromDom(collected, settings);

    const after = getScrollSnapshot();
    const afterCollected = collected.size;

    const gainedItems = afterCollected > beforeCollected;
    const grewHeight = after.height > before.height;
    const grewCandidates = after.candidates > before.candidates;
    const atBottom = isNearPageBottom();

    if (gainedItems || grewHeight || grewCandidates || changed) {
      idleRounds = 0;
    } else {
      idleRounds++;
    }

    if (atBottom && !gainedItems && !grewHeight && !grewCandidates) {
      bottomStuckRounds++;
    } else {
      bottomStuckRounds = 0;
    }

    if (bottomStuckRounds >= 3 || idleRounds >= 6) {
      console.log("Stopping: bottom reached and no more content is loading.", {
        rounds,
        collected: collected.size,
        idleRounds,
        bottomStuckRounds,
        scrollY: after.y,
        scrollHeight: after.height,
        candidates: after.candidates,
      });
      break;
    }

    if (rounds >= 1000) {
      console.log("Stopping: max rounds reached.");
      break;
    }
  }

  const posts = Array.from(collected.values());

  if (!posts.length) {
    throw new Error(
      "No saved Reddit post/comment URLs were detected. Make sure you are logged in and the Saved page has fully loaded."
    );
  }

  return Number.isFinite(limit) ? posts.slice(0, limit) : posts;
}

// ─── Entry point ─────────────────────────────────────────────────────────────

async function startDomScraping(settings) {
  settings = { ...(settings || {}), limitAll: true, limit: "" };

  if (SCRAPE_STATE.running) {
    console.warn("Scrape already running, ignoring duplicate start.");
    return;
  }

  SCRAPE_STATE.running   = true;
  SCRAPE_STATE.cancelled = false;

  try {
    console.log("startDomScraping()", window.location.href, settings);

    if (!isSavedPageUrl()) throw new Error("Not on a Reddit saved page.");

    updateOverlay("Loading saved items...");

    const ready = await waitForInitialContent(30000);
    if (!ready) throw new Error("Timed out waiting for saved items to appear.");

    const posts = await scrapeInfiniteSavedFeed(settings);

    console.log("Finished scraping.", posts.length, posts.slice(0, 3));
    updateOverlay("Processing… Almost done!");
    
    chrome.runtime.sendMessage(
  {
    action: "PROCESS_DOWNLOAD",
    payload: {
      posts,
      formats: settings.formats || {},
    },
  },
  () => {
    if (chrome.runtime.lastError) {
      console.error("PROCESS_DOWNLOAD send failed:", chrome.runtime.lastError.message);
      safeSendMessage({
        action: "DOWNLOAD_ERROR",
        payload: { error: chrome.runtime.lastError.message },
      });
      removeOverlay();
      return;
    }

    console.log("PROCESS_DOWNLOAD handed off to background.");
    removeOverlay();
  }
);

  } catch (error) {
    console.error("startDomScraping error:", error);
    safeSendMessage({
      action: "DOWNLOAD_ERROR",
      payload: { error: error.message || String(error) },
    });
    removeOverlay();
  } finally {
    SCRAPE_STATE.running = false;
  }
}