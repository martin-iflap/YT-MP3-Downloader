// @ts-check
/// <reference types="firefox-webext-browser" />
/* global browser */

// Get DOM elements
const downloadBtn = document.getElementById('DownloadBtn');
const resultDiv = document.getElementById('result');
const loader = document.getElementById('loader');
const urlInput = document.getElementById('urlInput');
const folderInput = document.getElementById('folderInput');
const container = document.querySelector('.container');

// remember initial body min-height so we can restore it later
const INITIAL_BODY_MIN_HEIGHT = (function(){
  const v = window.getComputedStyle(document.body).minHeight || '320px';
  return parseInt(v, 10) || 320;
})();

let currentTabUrl = '';
let isYouTubePage = false;

// Check if URL is a YouTube URL
function isYouTubeUrl(url) {
  return url && (
    url.includes('youtube.com/watch') ||
    url.includes('youtu.be/') ||
    url.includes('youtube.com/shorts/')
  );
}

browser.tabs.query({ active: true, currentWindow: true }).then((tabs) => {
  if (tabs[0]) {
    currentTabUrl = tabs[0].url;
    isYouTubePage = isYouTubeUrl(currentTabUrl);

    // Autofill URL input if on YouTube page
    const urlLabel = document.querySelector('label[for="urlInput"]');
    if (urlInput) {
      urlInput.value = isYouTubePage ? currentTabUrl : '';
      if (isYouTubePage) {
        try { urlInput.focus(); urlInput.select(); } catch (e) {}
      } else {
        try { urlInput.focus(); } catch (e) {}
      }
    }
    // Update label text based on page type
    if (urlLabel) urlLabel.textContent = isYouTubePage ? 'Current URL' : 'YouTube URL';
  }

  // Load folder - simplified approach with guaranteed display
  const loadFolder = () => {
    console.log('[FOLDER] Starting folder load...');
    console.log('[FOLDER] folderInput exists?', !!folderInput);

    if (!folderInput) {
      console.error('[FOLDER] ❌ folderInput element is NULL - cannot set folder!');
      return;
    }

    // Try storage first
    browser.storage.local.get('lastFolder').then((result) => {
      console.log('[FOLDER] Storage query result:', result);

      if (result && result.lastFolder) {
        console.log('[FOLDER] ✅ Found in storage:', result.lastFolder);
        folderInput.value = result.lastFolder;
        console.log('[FOLDER] ✅ Display value:', folderInput.value);
      } else {
        console.log('[FOLDER] ⚠️ Storage empty, querying backend...');
        getCurrentFolder();
      }
    }).catch((err) => {
      console.error('[FOLDER] ❌ Storage error:', err);
      getCurrentFolder();
    });
  };

  // Small delay to ensure DOM is ready
  setTimeout(loadFolder, 100);

}).catch((error) => {
  console.error('Error getting tab info:', error);
  try { getCurrentFolder(); } catch (e) {}
});

// Handle download button click (guard DOM element presence)
if (downloadBtn) {
  downloadBtn.addEventListener('click', () => {
    // Declare urlToDownload locally instead of implicitly
    const urlToDownload = isYouTubePage ? currentTabUrl : (urlInput ? urlInput.value.trim() : '');

    // Validate URL
    if (!urlToDownload) {
      showResult('Please enter a YouTube URL', 'error');
      return;
    }

    if (!isYouTubeUrl(urlToDownload)) {
      showResult('Please enter a valid YouTube URL', 'error');
      return;
    }

    // Start download
    startDownload(urlToDownload);
  });
}

// Pending native requests map (requestId -> {onMessage, timer, cleanup})
const pendingNativeRequests = new Map();

function genRequestId() {
  return 'r' + Date.now() + Math.floor(Math.random() * 10000);
}

// Listen for native responses forwarded by background
browser.runtime.onMessage.addListener((msg) => {
  if (!msg || msg.type !== 'native_response') return;
  const id = msg.requestId;
  if (!id) return;
  const entry = pendingNativeRequests.get(id);
  if (!entry) return;
  try {
    entry.onMessage(msg);
  } catch (e) { console.error('Error in native response handler:', e); }

  // determine finality (explicit final flag is authoritative, else infer from status)
  const isFinal = Object.prototype.hasOwnProperty.call(msg, 'final')
    ? Boolean(msg.final)
    : Boolean(msg && (msg.status === 'success' || msg.status === 'error'));

  if (isFinal) {
    try { clearTimeout(entry.timer); } catch (e) {}
    pendingNativeRequests.delete(id);
  }
});

function sendNativeAction(message, onMessage, opts = {}) {
  const requestId = genRequestId();
  const timeoutMs = opts.timeoutMs || 30000;
  const timer = setTimeout(() => {
     try { onMessage({ status: 'error', message: 'Native host timeout', final: true }); } catch (e) {}
    pendingNativeRequests.delete(requestId);
     }, timeoutMs);

  pendingNativeRequests.set(requestId, { onMessage: onMessage, timer: timer });

  try {
    browser.runtime.sendMessage(Object.assign({ type: 'native_action', requestId: requestId }, message));
  } catch (e) {
    clearTimeout(timer);
    pendingNativeRequests.delete(requestId);
    throw e;
  }

  return {
    requestId: requestId,
    cleanup: () => {
      const e = pendingNativeRequests.get(requestId);
      if (e) {
        try { clearTimeout(e.timer); } catch (err) {}
        pendingNativeRequests.delete(requestId);
      }
    }
  };
}

// Request current folder from native host and populate folderInput
function getCurrentFolder() {
  console.log('[FOLDER] Requesting folder from backend...');

  sendNativeAction({ action: 'get_current_folder' }, (resp) => {
    console.log('[FOLDER] Backend response:', resp);

    if (!resp) {
      console.error('[FOLDER] ❌ No response from backend');
      return;
    }

    if (resp.status === 'success' && resp.folder) {
      console.log('[FOLDER] ✅ Received folder from backend:', resp.folder);

      if (folderInput) {
        folderInput.value = resp.folder;
        console.log('[FOLDER] ✅ Display value:', folderInput.value);
      } else {
        console.error('[FOLDER] ❌ folderInput is NULL when backend responded!');
      }

      // Save to storage for next time
      try {
        browser.storage.local.set({ lastFolder: resp.folder });
        console.log('[FOLDER] 💾 Saved to storage');
      } catch (e) {
        console.error('[FOLDER] ❌ Storage save error:', e);
      }
    } else if (resp.status === 'error') {
      console.error('[FOLDER] ❌ Backend error:', resp.message);
    }
  }, { timeoutMs: 5000 });
}

// Start download process
function startDownload(url) {
  if (!downloadBtn || !loader || !resultDiv) {
    console.error('Missing UI elements for download');
    return;
  }

  console.log('Starting download for:', url);

  const folderPath = folderInput ? folderInput.value.trim() : '';

  downloadBtn.disabled = true;
  loader.style.display = 'block';
  resultDiv.classList.remove('show', 'success', 'error', 'warning', 'info');

  try {
    sendNativeAction({ action: 'download', url: url, folder: folderPath || undefined }, (response) => {
      if (!response) return;
      if (typeof response !== 'object') return;

      console.log('Native response:', response);

      // Handle warnings as non-final intermediate messages
      if (response.status === 'warning') {
        showResult(response.message || 'Warning', 'warning');
        return; // wait for final
      }

      // Handle success
      if (response.status === 'success') {
        loader.style.display = 'none';
        downloadBtn.disabled = false;
        showResult(response.message || `Downloaded: ${response.filename || 'YouTube MP3'}`, 'success');
        return;
      }

      // Handle errors
      if (response.status === 'error') {
        loader.style.display = 'none';
        downloadBtn.disabled = false;
        if (urlInput) urlInput.disabled = false;
        showResult(response.message || 'Download failed', 'error');
        return;
      }

      // Handle any other status (info, progress, etc.) as non-final
      if (response.message) {
        showResult(response.message, response.status || 'info');
      }
    }, { timeoutMs: 120000 });
  } catch (e) {
    loader.style.display = 'none';
    downloadBtn.disabled = false;
    showResult('Failed to start native download', 'error');
  }
}

// Add handlers for new UI buttons (Select folder, Open in app)
const selectFolderBtn = document.getElementById('selectFolderBtn');
const openInAppBtn = document.getElementById('openInAppBtn');

// show/hide helpers only when hovering the actual input or the helper itself (with a short hover delay)
const inputGroups = document.querySelectorAll('.input-group');
if (inputGroups && inputGroups.length) {
  inputGroups.forEach(group => {
    const inputEl = group.querySelector('input[type="text"]');
    const helper = group.querySelector('.helper');
    if (!inputEl || !helper) return;

    let hoverTimer = null;
    const HOVER_DELAY_MS = 140;

    inputEl.addEventListener('mouseenter', () => {
      if (group.classList.contains('no-helper')) return;
      if (hoverTimer) { clearTimeout(hoverTimer); hoverTimer = null; }
      hoverTimer = setTimeout(() => {
        if (!group.classList.contains('no-helper')) helper.classList.add('helper-visible');
        hoverTimer = null;
      }, HOVER_DELAY_MS);
    });

    inputEl.addEventListener('mouseleave', () => {
      if (hoverTimer) {
        clearTimeout(hoverTimer);
        hoverTimer = null;
      } else {
        try { if (!helper.matches(':hover')) helper.classList.remove('helper-visible'); } catch (e) { helper.classList.remove('helper-visible'); }
      }
    });

    helper.addEventListener('mouseenter', () => {
      if (hoverTimer) { clearTimeout(hoverTimer); hoverTimer = null; }
      if (!group.classList.contains('no-helper')) helper.classList.add('helper-visible');
    });

    helper.addEventListener('mouseleave', () => {
      helper.classList.remove('helper-visible');
    });
  });
}

// Prevent selectFolderBtn hover from showing helper: add no-helper to parent input-group while hovering the button
if (selectFolderBtn) {
  selectFolderBtn.addEventListener('mouseenter', (e) => {
    const group = e.currentTarget.closest('.input-group');
    if (group) group.classList.add('no-helper');
  });
  selectFolderBtn.addEventListener('mouseleave', (e) => {
    const group = e.currentTarget.closest('.input-group');
    if (group) group.classList.remove('no-helper');
  });

  selectFolderBtn.addEventListener('click', () => {
    showResult('Opening folder chooser...', 'info');
    try {
      sendNativeAction({ action: 'select_folder' }, (resp) => {
        if (!resp) return;
        if (typeof resp !== 'object') return;

        // mp3_2.py sends back success status with folder field after folder selection
        if (resp.status === 'success' && resp.folder) {
          console.log('📁 Received folder from backend:', resp.folder);

          if (folderInput) {
            folderInput.value = resp.folder;
            console.log('✅ Updated folderInput.value to:', folderInput.value);
          }

          try {
            browser.storage.local.set({ lastFolder: resp.folder }).then(() => {
              console.log('💾 Successfully saved folder to storage.local');
              // Verify it was saved
              browser.storage.local.get('lastFolder').then((result) => {
                console.log('🔍 Verification - storage now contains:', result);
              });
            }).catch((e) => {
              console.error('❌ Failed to save to storage:', e);
            });
          } catch (e) {
            console.error('❌ Exception saving to storage:', e);
          }

          showResult('Folder selected', 'success');
        } else if (resp.status === 'error') {
          showResult(resp.message || 'Folder selection failed', 'error');
        }
      }, { timeoutMs: 120000 });
    } catch (e) {
      showResult('Failed to open folder chooser', 'error');
    }
  });
}

if (openInAppBtn) {
  openInAppBtn.addEventListener('click', () => {
    // Get URL if available, but don't require it - user can paste in GUI
    const urlToOpen = isYouTubePage ? currentTabUrl : (urlInput ? urlInput.value.trim() : '');

    // Get current folder from input (user might have selected a custom folder)
    const folderToOpen = folderInput ? folderInput.value.trim() : '';

    // Optional: validate URL only if provided
    if (urlToOpen && !isYouTubeUrl(urlToOpen)) {
      showResult('Invalid YouTube URL (will open GUI anyway)', 'warning');
    }

    showResult('Launching app...', 'info');
    try {
      // Send URL and folder to backend - both optional
      sendNativeAction({ action: 'download_gui', url: urlToOpen, folder: folderToOpen || undefined }, (resp) => {
        if (!resp) return;
        if (typeof resp !== 'object') return;

        // mp3_2.py sends back success status after launching GUI
        if (resp.status === 'success') {
          showResult(resp.message || 'App launched', 'success');
        } else if (resp.status === 'error') {
          showResult(resp.message || 'Failed to launch app', 'error');
        }
      }, { timeoutMs: 120000 });
    } catch (e) {
      showResult('Failed to launch app', 'error');
    }
  });
}

// Show result message
function showResult(message, type) {
  if (!message || !message.toString().trim()) return;
  if (!resultDiv) return; // guard to avoid runtime exceptions if DOM missing

  resultDiv.classList.remove('show', 'success', 'error', 'warning', 'info');

  resultDiv.textContent = message;
  resultDiv.classList.add('show', type);

  try {
    const padding = 60;
    const needed = (container ? container.scrollHeight : document.documentElement.scrollHeight) + padding;
    const finalH = Math.max(INITIAL_BODY_MIN_HEIGHT, Math.ceil(needed));
    document.body.style.minHeight = finalH + 'px';
    if (container) container.scrollTop = container.scrollHeight;
  } catch (e) {
    console.warn('Could not adjust popup size:', e);
  }

  if (type === 'success') {
    setTimeout(() => {
      if (!resultDiv) return;
      resultDiv.classList.remove('show', 'success', 'error', 'warning', 'info');
      try {
        document.body.style.minHeight = INITIAL_BODY_MIN_HEIGHT + 'px';
      } catch (e) { /* ignore */ }
    }, 5000);
  }
}
