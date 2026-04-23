console.log("X Bookmarks Importer: content script loaded");

let isCollecting = false;
let shouldStop = false;
let currentSeen = new Set();
let hasFinalizedDuringThisRun = false;

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!message?.action) return false;

  if (message.action === "PING") {
    sendResponse({ status: "pong" });
    return false;
  }

  if (message.action === "START_X_BOOKMARK_SCRAPE") {
    startScrape(message.settings || {})
      .then((result) => sendResponse({ status: "started", ...result }))
      .catch((error) => sendResponse({ status: "error", error: error.message }));
    return true;
  }

  if (message.action === "STOP_X_BOOKMARK_SCRAPE") {
    shouldStop = true;

    checkpointAndFinalize("stopped_by_user")
      .then((result) => {
        hasFinalizedDuringThisRun = Boolean(result?.uploaded);
        showOverlay(
          result?.uploaded
            ? `Stopped. Sent ${result.count} X bookmark URLs.`
            : `Stopped. No URLs were ready to send.`
        );
        setTimeout(removeOverlay, 2500);
        sendResponse({ status: "stopping", ...result });
      })
      .catch((error) => {
        showOverlay(`Error: ${error.message}`);
        setTimeout(removeOverlay, 4000);
        sendResponse({ status: "error", error: error.message });
      });

    return true;
  }

  return false;
});

async function startScrape(settings = {}) {
  if (isCollecting) {
    return { alreadyRunning: true };
  }

  if (typeof collectVisibleBookmarkUrls !== "function") {
    throw new Error("collectVisibleBookmarkUrls is not available");
  }

  if (!window.location.href.includes("/i/bookmarks")) {
    throw new Error("Not on X bookmarks page");
  }

  isCollecting = true;
  shouldStop = false;
  hasFinalizedDuringThisRun = false;
  currentSeen = new Set();

  showOverlay("Starting X/Twitter bookmarks scrape...");

  try {
    let round = 0;
    let staleRounds = 0;
    let lastSeenCount = 0;
    let lastScrollY = window.scrollY || document.documentElement.scrollTop || 0;

    while (!shouldStop) {
      round += 1;

      const currentUrls = collectVisibleBookmarkUrls();
      let newThisRound = 0;

      for (const url of currentUrls) {
        if (currentSeen.has(url)) continue;
        currentSeen.add(url);
        newThisRound += 1;
      }

      showOverlay(`Collected ${currentSeen.size} X bookmark URLs (round ${round}, +${newThisRound})...`);
      await saveCheckpointSnapshot();

      const scrollAmount = randomBetween(400, 700);
      window.scrollBy(0, scrollAmount);

      const waitTime = randomBetween(1500, 3500);
      await wait(waitTime);

      const currentScrollY = window.scrollY || document.documentElement.scrollTop || 0;

      if (currentSeen.size === lastSeenCount && currentScrollY === lastScrollY) {
        staleRounds += 1;
      } else {
        staleRounds = 0;
      }

      lastSeenCount = currentSeen.size;
      lastScrollY = currentScrollY;

      if (staleRounds > 10) {
        break;
      }
    }

    if (hasFinalizedDuringThisRun) {
      return {
        ok: true,
        stopped: true,
        count: currentSeen.size
      };
    }

    showOverlay(`Finalizing ${currentSeen.size} X bookmark URLs...`);
    const response = await checkpointAndFinalize(shouldStop ? "stopped_by_user" : "completed");

    if (!response?.uploaded) {
      throw new Error(response?.error || response?.reason || "Backend upload failed");
    }

    hasFinalizedDuringThisRun = true;

    showOverlay(`Done. Sent ${response.count} X bookmark URLs.`);
    setTimeout(removeOverlay, 2500);

    return {
      ok: true,
      count: response.count,
      apiResult: response.apiResult
    };
  } catch (error) {
    showOverlay(`Error: ${error.message}`);
    setTimeout(removeOverlay, 4000);
    throw error;
  } finally {
    isCollecting = false;
    shouldStop = false;
  }
}

async function checkpointAndFinalize(reason) {
  await saveCheckpointSnapshot();

  return await sendRuntimeMessage({
    action: "FINALIZE_X_CHECKPOINT",
    payload: { reason }
  });
}

async function saveCheckpointSnapshot() {
  const urls = Array.from(currentSeen.values());

  if (urls.length === 0) {
    return { totalCount: 0, newCount: 0 };
  }

  return await sendRuntimeMessage({
    action: "SAVE_X_CHECKPOINT",
    payload: { urls }
  });
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

function randomBetween(min, max) {
  return Math.floor(Math.random() * (max - min + 1) + min);
}

function wait(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function showOverlay(text) {
  let overlay = document.getElementById("x-bookmarks-importer-overlay");

  if (!overlay) {
    overlay = document.createElement("div");
    overlay.id = "x-bookmarks-importer-overlay";
    overlay.style.cssText = `
      position: fixed;
      top: 24px;
      right: 24px;
      z-index: 999999;
      background: rgba(29, 155, 240, 0.95);
      color: #fff;
      padding: 14px 18px;
      border-radius: 10px;
      box-shadow: 0 8px 24px rgba(0,0,0,0.2);
      font: 14px/1.4 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      max-width: 360px;
    `;
    document.body.appendChild(overlay);
  }

  overlay.textContent = text;
}

window.addEventListener("pagehide", () => {
  if (!isCollecting || currentSeen.size === 0) return;

  chrome.runtime.sendMessage(
    {
      action: "SAVE_X_CHECKPOINT",
      payload: { urls: Array.from(currentSeen.values()) }
    },
    () => {
      void chrome.runtime.lastError;
    }
  );
});

function removeOverlay() {
  const overlay = document.getElementById("x-bookmarks-importer-overlay");
  if (overlay) overlay.remove();
}
