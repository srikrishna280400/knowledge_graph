console.log("LinkedIn Saved Post Hero: content script loaded");

let isCollecting = false;
let shouldStop = false;
let currentSeen = new Map();
let hasFinalizedDuringThisRun = false;

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!message?.action) return false;

  if (message.action === "PING") {
    sendResponse({ status: "pong" });
    return false;
  }

  if (message.action === "START_SCRAPE") {
    startScrape(message.settings || {})
      .then((result) => sendResponse({ status: "started", ...result }))
      .catch((error) => sendResponse({ status: "error", error: error.message }));
    return true;
  }

  if (message.action === "STOP_SCRAPE") {
    console.log("STOP_SCRAPE clicked", {
      currentSeenSize: currentSeen.size,
      firstPost: Array.from(currentSeen.values())[0] || null
    });

    shouldStop = true;

    checkpointAndFinalize("stopped_by_user")
      .then((result) => {
        hasFinalizedDuringThisRun = Boolean(result?.uploaded);
        showOverlay(
          result?.uploaded
            ? `Stopped. Sent ${result.count} LinkedIn URLs.`
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

  if (typeof collectAllSavedPosts !== "function") {
    throw new Error("collectAllSavedPosts is not available");
  }

  if (!window.location.href.includes("/my-items/saved-posts")) {
    throw new Error("Not on LinkedIn saved posts page");
  }

  isCollecting = true;
  shouldStop = false;
  hasFinalizedDuringThisRun = false;
  currentSeen = new Map();

  showOverlay("Starting LinkedIn scrape...");

  try {
    let round = 0;

    while (!shouldStop) {
      round += 1;
      await wait(1200);

      const currentPosts = collectAllSavedPosts().filter(Boolean);
      let newThisRound = 0;

      for (const post of currentPosts) {
        const key = post.post_urn || post.post_url;
        if (!key) continue;
        if (currentSeen.has(key)) continue;
        currentSeen.set(key, post);
        newThisRound += 1;
      }

      showOverlay(`Collected ${currentSeen.size} LinkedIn posts (round ${round}, +${newThisRound})...`);
      await saveCheckpointSnapshot();

      const loadMoreButton = document.querySelector(".scaffold-finite-scroll__load-button");
      const canLoadMore =
        loadMoreButton &&
        loadMoreButton.offsetParent !== null &&
        !loadMoreButton.disabled;

      if (!canLoadMore) {
        break;
      }

      const previousDomCount = document.querySelectorAll("[data-chameleon-result-urn]").length;

      loadMoreButton.scrollIntoView({ behavior: "smooth", block: "center" });
      await wait(700);
      loadMoreButton.click();
      await waitForDomGrowth(previousDomCount, 5000);
    }

    if (hasFinalizedDuringThisRun) {
      return {
        ok: true,
        stopped: true,
        count: currentSeen.size,
      };
    }

    showOverlay(`Finalizing ${currentSeen.size} LinkedIn posts...`);
    const response = await checkpointAndFinalize(shouldStop ? "stopped_by_user" : "completed");

    if (!response?.uploaded) {
      throw new Error(response?.error || response?.reason || "Backend upload failed");
    }

    hasFinalizedDuringThisRun = true;

    showOverlay(`Done. Sent ${response.count} LinkedIn URLs.`);
    setTimeout(removeOverlay, 2500);

    return {
      ok: true,
      count: response.count,
      apiResult: response.apiResult,
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
  console.log("checkpointAndFinalize start", {
    reason,
    currentSeenSize: currentSeen.size
  });

  await saveCheckpointSnapshot();

  const result = await sendRuntimeMessage({
    action: "FINALIZE_LINKEDIN_CHECKPOINT",
    payload: { reason },
  });

  console.log("checkpointAndFinalize result", result);
  return result;
}

async function saveCheckpointSnapshot() {
  const posts = Array.from(currentSeen.values());

  console.log("CHECKPOINT save start", {
    count: posts.length,
    firstPost: posts[0] || null
  });

  if (posts.length === 0) {
    return { totalCount: 0, newCount: 0 };
  }

  const result = await sendRuntimeMessage({
    action: "SAVE_LINKEDIN_CHECKPOINT",
    payload: { posts },
  });

  console.log("CHECKPOINT save result", result);
  return result;
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

async function waitForDomGrowth(previousCount, timeoutMs = 5000) {
  const started = Date.now();

  while (Date.now() - started < timeoutMs) {
    const count = document.querySelectorAll("[data-chameleon-result-urn]").length;
    if (count > previousCount) return;
    await wait(400);
  }
}

function wait(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function showOverlay(text) {
  let overlay = document.getElementById("linkedin-saved-post-hero-overlay");

  if (!overlay) {
    overlay = document.createElement("div");
    overlay.id = "linkedin-saved-post-hero-overlay";
    overlay.style.cssText = `
      position: fixed;
      top: 24px;
      right: 24px;
      z-index: 999999;
      background: rgba(10, 102, 194, 0.95);
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
  console.log("PAGEHIDE checkpoint", {
    isCollecting,
    currentSeenSize: currentSeen.size
  });

  if (!isCollecting || currentSeen.size === 0) return;

  chrome.runtime.sendMessage(
    {
      action: "SAVE_LINKEDIN_CHECKPOINT",
      payload: { posts: Array.from(currentSeen.values()) },
    },
    (response) => {
      if (chrome.runtime.lastError) {
        console.warn("PAGEHIDE checkpoint failed", chrome.runtime.lastError.message);
        return;
      }
      console.log("PAGEHIDE checkpoint result", response);
    }
  );
});

function removeOverlay() {
  const overlay = document.getElementById("linkedin-saved-post-hero-overlay");
  if (overlay) overlay.remove();
}
