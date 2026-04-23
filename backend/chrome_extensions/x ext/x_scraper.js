console.log("X Bookmarks Scraper helper loaded");

function normalizeStatusUrl(rawUrl) {
  if (typeof rawUrl !== "string" || !rawUrl.trim()) return null;

  try {
    const u = new URL(rawUrl, window.location.origin);
    if (!/^https?:$/.test(u.protocol)) return null;

    u.search = "";
    u.hash = "";

    const parts = u.pathname.split("/").filter(Boolean);
    const statusIndex = parts.indexOf("status");

    if (statusIndex < 1) return null;
    if (statusIndex >= parts.length - 1) return null;

    const username = parts[statusIndex - 1];
    const statusId = parts[statusIndex + 1];
    const trailing = parts[statusIndex + 2] || "";

    if (!username || !/^\d+$/.test(statusId)) return null;
    if (["analytics", "retweets", "quotes", "likes"].includes(trailing)) return null;

    return `https://x.com/${username}/status/${statusId}`;
  } catch {
    return null;
  }
}

function collectVisibleBookmarkUrls() {
  const tweets = document.querySelectorAll("article");
  const links = new Set();

  tweets.forEach((tweet) => {
    const anchors = tweet.querySelectorAll('a[href*="/status/"]');
    anchors.forEach((a) => {
      const normalized = normalizeStatusUrl(a.href);
      if (normalized) {
        links.add(normalized);
      }
    });
  });

  return Array.from(links.values());
}

window.collectVisibleBookmarkUrls = collectVisibleBookmarkUrls;
window.normalizeStatusUrl = normalizeStatusUrl;
