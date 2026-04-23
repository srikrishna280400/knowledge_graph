console.log("X_PAGE_BRIDGE_LOADED", window.location.href);

(() => {
  const PAGE_SOURCE = "x-bookmarks-importer-page";
  const EXT_SOURCE = "x-bookmarks-importer-extension";

  function sendToPage(payload) {
    window.postMessage(
      {
        source: EXT_SOURCE,
        ...payload
      },
      window.location.origin
    );
  }

  sendToPage({ type: "BRIDGE_READY" });

  window.addEventListener("message", (event) => {
    if (event.source !== window) return;
    if (event.origin !== window.location.origin) return;

    const data = event.data;
    if (!data || data.source !== PAGE_SOURCE) return;

    if (data.type === "PING_EXTENSION") {
      chrome.runtime.sendMessage(
        { action: "PING_FROM_PAGE_BRIDGE" },
        (response) => {
          if (chrome.runtime.lastError) {
            sendToPage({
              type: "PING_RESULT",
              ok: false,
              error: chrome.runtime.lastError.message
            });
            return;
          }

          sendToPage({
            type: "PING_RESULT",
            ok: true,
            response
          });
        }
      );
      return;
    }

    if (data.type === "INIT_X_ORCHESTRATION") {
      chrome.runtime.sendMessage(
        {
          action: "INIT_X_ORCHESTRATION_FROM_PAGE_BRIDGE",
          settings: data.settings || {}
        },
        (response) => {
          if (chrome.runtime.lastError) {
            sendToPage({
              type: "INIT_RESULT",
              ok: false,
              error: chrome.runtime.lastError.message
            });
            return;
          }

          sendToPage({
            type: "INIT_RESULT",
            ok: true,
            response
          });
        }
      );
      return;
    }

    if (data.type === "GET_X_CHECKPOINT_STATUS") {
      chrome.runtime.sendMessage(
        { action: "GET_X_CHECKPOINT_STATUS" },
        (response) => {
          if (chrome.runtime.lastError) {
            sendToPage({
              type: "STATUS_RESULT",
              ok: false,
              error: chrome.runtime.lastError.message
            });
            return;
          }

          sendToPage({
            type: "STATUS_RESULT",
            ok: true,
            response
          });
        }
      );
      return;
    }

    if (data.type === "CLEAR_X_CHECKPOINT") {
      chrome.runtime.sendMessage(
        { action: "CLEAR_X_CHECKPOINT" },
        (response) => {
          if (chrome.runtime.lastError) {
            sendToPage({
              type: "CLEAR_RESULT",
              ok: false,
              error: chrome.runtime.lastError.message
            });
            return;
          }

          sendToPage({
            type: "CLEAR_RESULT",
            ok: true,
            response
          });
        }
      );
    }
  });
})();
