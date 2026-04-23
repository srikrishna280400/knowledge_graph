
(() => {
  const PAGE_SOURCE = "reddit-saved-downloader-page";
  const EXT_SOURCE = "reddit-saved-downloader-extension";

  function sendToPage(payload) {
    window.postMessage(
      {
        source: EXT_SOURCE,
        ...payload,
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
          error: chrome.runtime.lastError.message,
        });
        return;
      }

      sendToPage({
        type: "PING_RESULT",
        ok: true,
        response,
      });
    }
  );
  return;
}

    if (data.type === "INIT_ORCHESTRATION") {
      chrome.runtime.sendMessage(
        {
          action: "INIT_ORCHESTRATION",
          settings: data.settings || {},
        },
        (response) => {
          if (chrome.runtime.lastError) {
            sendToPage({
              type: "INIT_RESULT",
              ok: false,
              error: chrome.runtime.lastError.message,
            });
            return;
          }

          sendToPage({
            type: "INIT_RESULT",
            ok: true,
            response,
          });
        }
      );
    }
  });
})();
