document.addEventListener('DOMContentLoaded', () => {
  // Load saved settings
  loadSettings();

  // Attach event listener
  document.getElementById('start-btn').addEventListener('click', startDownload);
});

function loadSettings() {
  chrome.storage.local.get(
    ['limit', 'limitAll', 'subreddits', 'filterMode', 'formats'],
    (result) => {
      const hasStoredLimitAll = typeof result.limitAll === 'boolean';
      const limitAll = hasStoredLimitAll ? result.limitAll : true;

      document.getElementById('limit-all').checked = limitAll;
      document.getElementById('limit').disabled = limitAll;

      if (!limitAll) {
        document.getElementById('limit').value = result.limit || '100';
      } else {
        document.getElementById('limit').value = '';
      }

      if (result.subreddits) {
        document.getElementById('subreddits').value = result.subreddits;
      }

      if (result.filterMode) {
        const radio = document.querySelector(
          `input[name="filterMode"][value="${result.filterMode}"]`
        );
        if (radio) radio.checked = true;
      }

      if (result.formats) {
        document.getElementById('format-csv').checked = !!result.formats.csv;
        document.getElementById('format-json').checked = !!result.formats.json;
        document.getElementById('format-html').checked = !!result.formats.html;
      }
    }
  );
}

// Add event listener for the "Download All" checkbox to toggle the input
document.getElementById('limit-all').addEventListener('change', (e) => {
  document.getElementById('limit').disabled = e.target.checked;
});

function saveSettings() {
  const isAll = document.getElementById('limit-all').checked;
  const settings = {
    limit: isAll ? '' : document.getElementById('limit').value,
    limitAll: isAll,
    subreddits: document.getElementById('subreddits').value,
    filterMode: document.querySelector('input[name="filterMode"]:checked').value,
    formats: {
      csv: document.getElementById('format-csv').checked,
      json: document.getElementById('format-json').checked,
      html: document.getElementById('format-html').checked
    }
  };
  chrome.storage.local.set(settings);
  return settings;
}

async function startDownload() {
  const settings = saveSettings();
  const btn = document.getElementById('start-btn');
  const statusArea = document.getElementById('status-area');
  const statusText = document.getElementById('status-text');
  const progressBar = document.getElementById('progress-bar');

  // UI Updates
  btn.disabled = true;
  btn.innerText = 'Initializing...';
  statusArea.classList.remove('hidden');
  statusText.innerText = 'Checking connection to Reddit...';
  progressBar.style.width = '5%';

  try {
    // Delegate the entire orchestration to the Background Script
    // This is crucial because if we open a new tab, this Popup will close and die.
    // The background script persists and can handle the tab opening + messaging.
    
    statusText.innerText = 'Handing off to background worker...';
    progressBar.style.width = '20%';

    chrome.runtime.sendMessage({
        action: 'INIT_ORCHESTRATION',
        settings: settings
    }, (response) => {
        if (chrome.runtime.lastError) {
             statusText.innerText = 'Error: ' + chrome.runtime.lastError.message;
             btn.disabled = false;
        } else {
             statusText.innerText = 'Started! Check the Reddit tab.';
             // Optionally close popup
             // window.close();
        }
    });

  } catch (error) {
    console.error(error);
    statusText.innerText = 'Error: ' + error.message;
    btn.disabled = false;
    btn.innerText = 'Start Download';
  }
}

// Listen for progress updates
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  const progressBar = document.getElementById('progress-bar');
  const statusText = document.getElementById('status-text');
  const startBtn = document.getElementById('start-btn');

  // Helper to safely update text
  const safeText = (el, text) => { if (el) el.innerText = text; };
  const safeStyle = (el, prop, val) => { if (el) el.style[prop] = val; };
  const safeDisabled = (el, val) => { if (el) el.disabled = val; };

  if (message.action === 'UPDATE_PROGRESS') {
    const { processed, total, status } = message.payload;
    const percentage = total > 0 ? Math.round((processed / total) * 100) : 0;
    
    safeStyle(progressBar, 'width', `${percentage}%`);
    safeText(statusText, status || `Processed ${processed} posts...`);
  }
  
  if (message.action === 'DOWNLOAD_COMPLETE') {
    safeStyle(progressBar, 'width', '100%');
    safeText(statusText, 'Download Complete!');
    safeDisabled(startBtn, false);
    safeText(startBtn, 'Start Download');
  }

  if (message.action === 'DOWNLOAD_ERROR') {
      safeText(statusText, 'Error: ' + message.payload.error);
      safeDisabled(startBtn, false);
      safeText(startBtn, 'Start Download');
  }
});
