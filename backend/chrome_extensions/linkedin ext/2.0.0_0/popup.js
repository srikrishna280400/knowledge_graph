// LinkedIn Saved Post Hero - Popup Script
// Displays stats and provides actions for managing collected posts

document.addEventListener('DOMContentLoaded', () => {
  checkCurrentPage();

  // Set up button listeners (these elements might not exist yet, so we'll add them conditionally)
  const exportJsonBtn = document.getElementById('export-json-btn');
  const exportCsvBtn = document.getElementById('export-csv-btn');
  const viewBtn = document.getElementById('view-btn');
  const clearBtn = document.getElementById('clear-btn');

  if (exportJsonBtn) exportJsonBtn.addEventListener('click', exportJSON);
  if (exportCsvBtn) exportCsvBtn.addEventListener('click', exportCSV);
  if (viewBtn) viewBtn.addEventListener('click', viewPosts);
  if (clearBtn) clearBtn.addEventListener('click', clearData);
});

// Check if user is on LinkedIn saved posts page
function checkCurrentPage() {
  chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
    const currentTab = tabs[0];

    if (!currentTab || !currentTab.url) {
      loadStats(); // Fallback to showing stats
      return;
    }

    const url = currentTab.url;
    const isOnSavedPostsPage = url.includes('linkedin.com/my-items/saved-posts');

    if (!isOnSavedPostsPage) {
      // Show navigation prompt INSTEAD of stats
      showNavigationPrompt();
    } else {
      // On the correct page, show stats
      loadStats();
    }
  });
}

// Show prompt to navigate to saved posts page
function showNavigationPrompt() {
  const content = document.getElementById('content');
  const emptyState = document.getElementById('empty-state');
  const loading = document.getElementById('loading');

  // Hide other sections
  loading.style.display = 'none';
  content.style.display = 'none';
  emptyState.style.display = 'none';

  // Create navigation prompt
  let navPrompt = document.getElementById('nav-prompt');
  if (!navPrompt) {
    navPrompt = document.createElement('div');
    navPrompt.id = 'nav-prompt';
    navPrompt.style.cssText = `
      padding: 24px;
      text-align: center;
    `;

    navPrompt.innerHTML = `
      <div style="display: flex; flex-direction: column; min-height: 300px;">
        <div style="flex: 1; padding: 24px 0;">
          <div style="font-size: 48px; margin-bottom: 16px;">📍</div>
          <h2 style="color: #0a66c2; margin-bottom: 12px; font-size: 16px;">Ready to Collect?</h2>
          <p style="color: #666; font-size: 13px; margin-bottom: 16px; line-height: 1.5;">
            You need to be on your LinkedIn Saved Posts page to start collecting.
          </p>
          <button id="go-to-saved-posts" style="
            background: linear-gradient(135deg, #0a66c2 0%, #004182 100%);
            color: white;
            border: none;
            padding: 12px 24px;
            border-radius: 24px;
            font-size: 14px;
            font-weight: 600;
            cursor: pointer;
            box-shadow: 0 4px 12px rgba(10, 102, 194, 0.4);
            transition: all 0.3s ease;
          ">
            🚀 Go to Saved Posts
          </button>
        </div>
        <div style="margin-top: auto; padding-top: 16px; border-top: 1px solid #ddd; text-align: center;">
          <a href="https://www.buymeacoffee.com/hartman.coffee" target="_blank" style="
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 6px 12px;
            background: linear-gradient(135deg, #FFDD00 0%, #FBB034 100%);
            color: #000;
            text-decoration: none;
            border-radius: 6px;
            font-size: 12px;
            font-weight: 600;
            transition: transform 0.2s ease, box-shadow 0.2s ease;
            box-shadow: 0 2px 4px rgba(251, 176, 52, 0.3);
          ">
            <span style="font-size: 14px;">☕</span>
            <span>Buy Me A Coffee</span>
          </a>
        </div>
      </div>
    `;

    document.body.appendChild(navPrompt);

    // Add click handler for the button
    document.getElementById('go-to-saved-posts').addEventListener('click', () => {
      // First, check if the saved posts page is already open in another tab
      chrome.tabs.query({}, (allTabs) => {
        const savedPostsTab = allTabs.find(tab =>
          tab.url && tab.url.includes('linkedin.com/my-items/saved-posts')
        );

        if (savedPostsTab) {
          // Tab already exists, switch to it
          chrome.tabs.update(savedPostsTab.id, { active: true }, () => {
            chrome.windows.update(savedPostsTab.windowId, { focused: true });
          });
          window.close(); // Close popup
        } else {
          // Tab doesn't exist, navigate current tab or create new one
          chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
            if (tabs[0]) {
              chrome.tabs.update(tabs[0].id, {
                url: 'https://www.linkedin.com/my-items/saved-posts/'
              });
            } else {
              // Fallback: create new tab
              chrome.tabs.create({
                url: 'https://www.linkedin.com/my-items/saved-posts/'
              });
            }
            window.close(); // Close popup after navigation
          });
        }
      });
    });

    // Add hover effect
    const btn = document.getElementById('go-to-saved-posts');
    btn.addEventListener('mouseenter', () => {
      btn.style.transform = 'translateY(-2px)';
      btn.style.boxShadow = '0 6px 16px rgba(10, 102, 194, 0.5)';
    });
    btn.addEventListener('mouseleave', () => {
      btn.style.transform = 'translateY(0)';
      btn.style.boxShadow = '0 4px 12px rgba(10, 102, 194, 0.4)';
    });
  }

  navPrompt.style.display = 'block';
}

// Load and display stats
function loadStats() {
  chrome.runtime.sendMessage({ type: 'GET_STATS' }, (response) => {
    const loading = document.getElementById('loading');
    const content = document.getElementById('content');
    const emptyState = document.getElementById('empty-state');

    loading.style.display = 'none';

    if (response.success && response.stats.total_count > 0) {
      // Show content with stats
      content.style.display = 'block';
      emptyState.style.display = 'none';

      document.getElementById('total-count').textContent = response.stats.total_count;

      // Format last collect date
      if (response.stats.last_scrape_date) {
        const date = new Date(response.stats.last_scrape_date);
        const formattedDate = formatRelativeTime(date);
        document.getElementById('last-collect').textContent = formattedDate;
      } else {
        document.getElementById('last-collect').textContent = 'Never';
      }
    } else {
      // Show empty state
      content.style.display = 'none';
      emptyState.style.display = 'block';
    }
  });
}

// Export data as JSON file
function exportJSON() {
  chrome.runtime.sendMessage({ type: 'EXPORT_DATA' }, (response) => {
    if (response.success) {
      const data = response.data;
      const jsonString = JSON.stringify(data, null, 2);
      const blob = new Blob([jsonString], { type: 'application/json' });
      const url = URL.createObjectURL(blob);

      // Create download link
      const a = document.createElement('a');
      a.href = url;
      a.download = `linkedin-saved-posts-${new Date().toISOString().split('T')[0]}.json`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);

      showToast('JSON exported successfully!', 'success');
    } else {
      showToast('Failed to export data', 'error');
    }
  });
}

// Export data as CSV file
function exportCSV() {
  chrome.runtime.sendMessage({ type: 'EXPORT_DATA' }, (response) => {
    if (response.success) {
      const data = response.data;

      if (!data || data.length === 0) {
        showToast('No data to export', 'error');
        return;
      }

      // Convert JSON to CSV
      const csvString = convertToCSV(data);
      const blob = new Blob([csvString], { type: 'text/csv;charset=utf-8;' });
      const url = URL.createObjectURL(blob);

      // Create download link
      const a = document.createElement('a');
      a.href = url;
      a.download = `linkedin-saved-posts-${new Date().toISOString().split('T')[0]}.csv`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);

      showToast('CSV exported successfully!', 'success');
    } else {
      showToast('Failed to export data', 'error');
    }
  });
}

// Convert JSON data to CSV format
function convertToCSV(data) {
  if (!data || data.length === 0) return '';

  // Define CSV headers based on the data structure
  const headers = [
    'post_urn',
    'author_name',
    'author_profile_url',
    'author_headline',
    'author_profile_image_url',
    'is_company_post',
    'connection_degree',
    'posted_time',
    'post_url',
    'post_text',
    'has_image',
    'post_image_url',
    'post_image_alt',
    'has_video'
  ];

  // Create CSV header row
  const headerRow = headers.map(h => `"${h}"`).join(',');

  // Create CSV data rows
  const dataRows = data.map(post => {
    return headers.map(header => {
      let value = post[header];

      // Handle null/undefined
      if (value === null || value === undefined) {
        return '""';
      }

      // Handle booleans
      if (typeof value === 'boolean') {
        return value ? 'true' : 'false';
      }

      // Handle strings - escape quotes and wrap in quotes
      value = String(value);
      value = value.replace(/"/g, '""'); // Escape quotes
      return `"${value}"`;
    }).join(',');
  });

  // Combine header and data rows
  return [headerRow, ...dataRows].join('\n');
}

// View all posts in a new tab
function viewPosts() {
  chrome.runtime.sendMessage({ type: 'GET_STATS' }, (response) => {
    if (response.success && response.stats.posts.length > 0) {
      // Create a simple HTML page to display posts
      const posts = response.stats.posts;
      const html = generatePostsHTML(posts);

      // Open in new tab
      const blob = new Blob([html], { type: 'text/html' });
      const url = URL.createObjectURL(blob);
      chrome.tabs.create({ url: url });
    } else {
      showToast('No posts to view', 'error');
    }
  });
}

// Clear all stored data
function clearData() {
  if (confirm('Are you sure you want to delete all saved posts? This cannot be undone.')) {
    chrome.runtime.sendMessage({ type: 'CLEAR_DATA' }, (response) => {
      if (response.success) {
        showToast('All data cleared', 'success');
        // Reload stats
        setTimeout(() => {
          loadStats();
        }, 500);
      } else {
        showToast('Failed to clear data', 'error');
      }
    });
  }
}

// Generate HTML for viewing posts
function generatePostsHTML(posts) {
  const postsHTML = posts.map((post, index) => `
    <div class="post-card">
      <div class="post-header">
        <div class="author-info">
          ${post.author_profile_image_url ? `<img src="${post.author_profile_image_url}" alt="${post.author_name}" class="author-image">` : ''}
          <div>
            <h3><a href="${post.author_profile_url}" target="_blank">${post.author_name || 'Unknown Author'}</a></h3>
            ${post.author_headline ? `<p class="headline">${post.author_headline}</p>` : ''}
            ${post.connection_degree ? `<span class="badge">${post.connection_degree}</span>` : ''}
            ${post.is_company_post ? `<span class="badge company">Company</span>` : ''}
          </div>
        </div>
        <span class="timestamp">${post.posted_time || 'Unknown'}</span>
      </div>
      <div class="post-content">
        ${post.post_text ? `<p>${post.post_text}</p>` : ''}
        ${post.has_image && post.post_image_url ? `<img src="${post.post_image_url}" alt="${post.post_image_alt || 'Post image'}" class="post-image">` : ''}
        ${post.has_video ? `<div class="video-badge">🎥 Contains Video</div>` : ''}
      </div>
      <div class="post-footer">
        <a href="${post.post_url}" target="_blank" class="view-link">View on LinkedIn →</a>
      </div>
    </div>
  `).join('');

  return `
    <!DOCTYPE html>
    <html>
    <head>
      <meta charset="utf-8">
      <title>LinkedIn Saved Posts - ${posts.length} posts</title>
      <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
          font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
          background: #f3f2ef;
          padding: 20px;
        }
        .container {
          max-width: 800px;
          margin: 0 auto;
        }
        .header {
          background: white;
          padding: 24px;
          border-radius: 8px;
          margin-bottom: 20px;
          box-shadow: 0 1px 3px rgba(0,0,0,0.12);
        }
        h1 { color: #0a66c2; margin-bottom: 8px; }
        .subtitle { color: #666; font-size: 14px; }
        .post-card {
          background: white;
          border-radius: 8px;
          padding: 20px;
          margin-bottom: 16px;
          box-shadow: 0 1px 3px rgba(0,0,0,0.12);
        }
        .post-header {
          display: flex;
          justify-content: space-between;
          margin-bottom: 16px;
        }
        .author-info {
          display: flex;
          gap: 12px;
          align-items: flex-start;
        }
        .author-image {
          width: 48px;
          height: 48px;
          border-radius: 50%;
        }
        .author-info h3 {
          font-size: 16px;
          margin-bottom: 4px;
        }
        .author-info h3 a {
          color: #000;
          text-decoration: none;
        }
        .author-info h3 a:hover {
          color: #0a66c2;
          text-decoration: underline;
        }
        .headline {
          font-size: 13px;
          color: #666;
          margin-bottom: 4px;
        }
        .badge {
          display: inline-block;
          background: #f3f6f9;
          color: #0a66c2;
          padding: 2px 8px;
          border-radius: 4px;
          font-size: 12px;
          margin-right: 4px;
        }
        .badge.company {
          background: #e8f5e9;
          color: #2e7d32;
        }
        .timestamp {
          font-size: 13px;
          color: #666;
        }
        .post-content p {
          line-height: 1.6;
          color: #333;
          margin-bottom: 12px;
        }
        .post-image {
          max-width: 100%;
          border-radius: 4px;
          margin-top: 12px;
        }
        .video-badge {
          background: #f3f6f9;
          padding: 12px;
          border-radius: 4px;
          text-align: center;
          color: #0a66c2;
          margin-top: 12px;
        }
        .post-footer {
          margin-top: 16px;
          padding-top: 12px;
          border-top: 1px solid #eee;
        }
        .view-link {
          color: #0a66c2;
          text-decoration: none;
          font-size: 14px;
          font-weight: 600;
        }
        .view-link:hover {
          text-decoration: underline;
        }
      </style>
    </head>
    <body>
      <div class="container">
        <div class="header">
          <h1>📥 LinkedIn Saved Posts</h1>
          <p class="subtitle">${posts.length} posts saved • Exported ${new Date().toLocaleDateString()}</p>
        </div>
        ${postsHTML}
      </div>
    </body>
    </html>
  `;
}

// Format relative time (e.g., "2 hours ago")
function formatRelativeTime(date) {
  const now = new Date();
  const diffMs = now - date;
  const diffMins = Math.floor(diffMs / 60000);
  const diffHours = Math.floor(diffMs / 3600000);
  const diffDays = Math.floor(diffMs / 86400000);

  if (diffMins < 1) return 'Just now';
  if (diffMins < 60) return `${diffMins} minute${diffMins > 1 ? 's' : ''} ago`;
  if (diffHours < 24) return `${diffHours} hour${diffHours > 1 ? 's' : ''} ago`;
  if (diffDays < 7) return `${diffDays} day${diffDays > 1 ? 's' : ''} ago`;

  return date.toLocaleDateString();
}

// Show toast notification
function showToast(message, type = 'info') {
  const toast = document.createElement('div');
  toast.style.cssText = `
    position: fixed;
    bottom: 20px;
    left: 50%;
    transform: translateX(-50%);
    background: ${type === 'success' ? '#10b981' : type === 'error' ? '#ef4444' : '#3b82f6'};
    color: white;
    padding: 12px 24px;
    border-radius: 6px;
    font-size: 13px;
    z-index: 10000;
    animation: slideUp 0.3s ease;
  `;
  toast.textContent = message;
  document.body.appendChild(toast);

  setTimeout(() => {
    toast.style.animation = 'slideDown 0.3s ease';
    setTimeout(() => toast.remove(), 300);
  }, 2000);
}
