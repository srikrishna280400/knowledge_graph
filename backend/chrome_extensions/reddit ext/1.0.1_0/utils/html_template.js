// utils/html_template.js

export function generateHTML(posts) {
  const cards = posts.map(post => {
    // Determine media type
    let mediaContent = '';
    
    if (post.previewImage) {
      if (post.isVideo) {
          mediaContent = `
            <div class="media-container video">
                <a href="${post.url}" target="_blank">
                    <img src="${post.previewImage}" alt="Video Thumbnail" loading="lazy">
                    <div class="play-icon">▶</div>
                </a>
            </div>`;
      } else {
        mediaContent = `
            <div class="media-container">
                <img src="${post.previewImage}" alt="Post Image" loading="lazy">
            </div>`;
      }
    }

    return `
      <article class="tweet-card ${post.type || 'post'}" data-subreddit="${post.subreddit}" data-type="${post.type || 'post'}">
        <div class="header">
          <div class="meta-left">
             <span class="type-badge ${post.type || 'post'}">${(post.type || 'post').toUpperCase()}</span>
             <span class="subreddit">${post.subreddit_name_prefixed}</span>
          </div>
          <div class="meta-right">
             <span class="date">${new Date(post.created_utc * 1000).toLocaleDateString()}</span>
             <a href="${post.permalink}" target="_blank" class="link-icon">↗</a>
          </div>
        </div>
        <div class="content">
          <h3 class="title">${post.title}</h3>
          ${post.selftext ? `<div class="text-body">${post.selftext}</div>` : ''}
        </div>
        ${mediaContent}
      </article>
    `;
  }).join('');

  return `
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Reddit Saved Posts Export</title>
  <style>
    :root {
      --bg: #F0F2F5;
      --card-bg: #FFFFFF;
      --text: #1A1A1B;
      --text-secondary: #7c7c7c;
      --primary: #FF4500;
      --border: #EDEFF1;
    }
    
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      background-color: var(--bg);
      color: var(--text);
      margin: 0;
      padding: 40px 20px;
    }

    .container {
      max-width: 800px;
      margin: 0 auto;
    }

    h1 {
      text-align: center;
      margin-bottom: 20px;
      color: var(--text);
    }
    
    /* Controls Bar */
    .controls {
        display: flex;
        gap: 12px;
        margin-bottom: 24px;
        background: white;
        padding: 16px;
        border-radius: 12px;
        border: 1px solid var(--border);
        box-shadow: 0 2px 4px rgba(0,0,0,0.05);
        flex-wrap: wrap;
        align-items: center;
    }

    .search-box {
        flex: 1;
        min-width: 200px;
    }

    .search-box input {
        width: 100%;
        padding: 10px;
        border: 1px solid var(--border);
        border-radius: 6px;
        font-size: 14px;
        box-sizing: border-box;
    }

    .filter-group {
        display: flex;
        gap: 8px;
    }

    select {
        padding: 10px;
        border: 1px solid var(--border);
        border-radius: 6px;
        font-size: 14px;
        background: white;
    }

    .tweet-card {
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 16px;
      margin-bottom: 20px;
      box-shadow: 0 1px 2px rgba(0,0,0,0.05);
      transition: transform 0.2s;
    }

    .tweet-card:hover {
      box-shadow: 0 4px 12px rgba(0,0,0,0.1);
    }

    .header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 12px;
      font-size: 14px;
    }

    .meta-left, .meta-right {
        display: flex;
        align-items: center;
        gap: 8px;
    }

    .type-badge {
        font-size: 10px;
        padding: 2px 6px;
        border-radius: 4px;
        font-weight: 800;
        text-transform: uppercase;
        letter-spacing: 0.5px;
    }

    .type-badge.post {
        background-color: #e3f2fd;
        color: #1976d2;
    }

    .type-badge.comment {
        background-color: #f3e5f5;
        color: #7b1fa2;
    }

    .subreddit {
      font-weight: 700;
      color: var(--text);
    }

    .date {
      color: var(--text-secondary);
      font-size: 13px;
    }

    .link-icon {
      text-decoration: none;
      color: var(--text-secondary);
    }

    .title {
      font-size: 16px;
      margin: 0 0 8px 0;
      line-height: 1.4;
    }

    .text-body {
      font-size: 14px;
      line-height: 1.5;
      color: #333;
      margin-bottom: 12px;
      white-space: pre-wrap;
      max-height: 200px;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .media-container {
      margin-top: 12px;
      border-radius: 8px;
      overflow: hidden;
      border: 1px solid var(--border);
      position: relative;
    }

    .media-container img {
      width: 100%;
      height: auto;
      display: block;
      max-height: 500px;
      object-fit: cover;
    }

    .video .play-icon {
        position: absolute;
        top: 50%;
        left: 50%;
        transform: translate(-50%, -50%);
        font-size: 40px;
        color: white;
        text-shadow: 0 2px 4px rgba(0,0,0,0.5);
        pointer-events: none;
    }
    
    .hidden {
        display: none;
    }

  </style>
</head>
<body>
  <div class="container">
    <h1>Saved Reddit Posts</h1>
    
    <div class="controls">
        <div class="search-box">
            <input type="text" id="searchInput" placeholder="Search titles, subreddits...">
        </div>
        <div class="filter-group">
            <select id="typeFilter">
                <option value="all">All Types</option>
                <option value="post">Posts</option>
                <option value="comment">Comments</option>
            </select>
            <select id="subredditFilter">
                <option value="all">All Subreddits</option>
                <!-- populated by js -->
            </select>
        </div>
    </div>

    <div id="posts-list">
      ${cards}
    </div>
  </div>

  <script>
    // Embedded logic for filtering
    const searchInput = document.getElementById('searchInput');
    const typeFilter = document.getElementById('typeFilter');
    const subredditFilter = document.getElementById('subredditFilter');
    const cards = document.querySelectorAll('.tweet-card');

    // Populate Subreddits
    const subs = new Set();
    cards.forEach(card => subs.add(card.dataset.subreddit));
    [...subs].sort().forEach(sub => {
        const opt = document.createElement('option');
        opt.value = sub;
        opt.innerText = 'r/' + sub;
        subredditFilter.appendChild(opt);
    });

    function filter() {
        const term = searchInput.value.toLowerCase();
        const type = typeFilter.value;
        const sub = subredditFilter.value;

        cards.forEach(card => {
            const text = card.innerText.toLowerCase();
            const cardType = card.dataset.type;
            const cardSub = card.dataset.subreddit;

            const matchesSearch = text.includes(term);
            const matchesType = type === 'all' || cardType === type;
            const matchesSub = sub === 'all' || cardSub === sub;

            if (matchesSearch && matchesType && matchesSub) {
                card.classList.remove('hidden');
            } else {
                card.classList.add('hidden');
            }
        });
    }

    searchInput.addEventListener('input', filter);
    typeFilter.addEventListener('change', filter);
    subredditFilter.addEventListener('change', filter);
  </script>
</body>
</html>
  `;
}
