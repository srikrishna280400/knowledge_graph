console.log("LINKEDIN_PAGE_BRIDGE_LOADED", window.location.href);

(() => {
  const PAGE_SOURCE = "linkedin-saved-post-hero-page";
  const EXT_SOURCE = "linkedin-saved-post-hero-extension";

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

if (data.type === "INIT_LINKEDIN_ORCHESTRATION") {
  console.log("PAGE_BRIDGE init send start", data);

  chrome.runtime.sendMessage(
    {
      action: "INIT_LINKEDIN_ORCHESTRATION_FROM_PAGE_BRIDGE",
      settings: data.settings || {}
    },
    (response) => {
      if (chrome.runtime.lastError) {
        console.error(
          "PAGE_BRIDGE init lastError:",
          chrome.runtime.lastError.message
        );

        sendToPage({
          type: "INIT_RESULT",
          ok: false,
          error: chrome.runtime.lastError.message
        });
        return;
      }

      console.log("PAGE_BRIDGE init response", response);

      sendToPage({
        type: "INIT_RESULT",
        ok: true,
        response
      });
    }
  );
}
});
})();
