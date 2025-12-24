// @ts-check
/// <reference types="firefox-webext-browser" />
/* global browser */

// Get DOM elements
const downloadBtn = document.getElementById('DownloadBtn');
const resultDiv = document.getElementById('result');
const loader = document.getElementById('loader');
const currentUrlContainer = document.getElementById('currentUrlContainer');
const currentUrlText = document.getElementById('currentUrl');
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

    if (isYouTubePage) {
      if (urlInput) {
        urlInput.value = currentTabUrl;
        try { urlInput.focus(); urlInput.select(); } catch (e) {}
      }
      if (currentUrlContainer) currentUrlContainer.style.display = 'none';
    } else {
      if (currentUrlContainer) currentUrlContainer.style.display = 'none';
      if (urlInput) {
        urlInput.value = '';
        try { urlInput.focus(); } catch (e) {}
      }
    }
  }
  try { getCurrentFolder(); } catch (e) { console.warn('Could not request current folder:', e); }
}).catch((error) => {
  console.error('Error getting tab info:', error);
  if (currentUrlText) currentUrlText.textContent = 'Error loading URL';
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

  // determine finality (declare as a local const)
  const isFinal = Object.prototype.hasOwnProperty.call(msg, 'final')
    ? Boolean(msg.final)
    : Boolean(msg && (msg.status === 'success' || msg.status === 'error' || msg.status === 'folder_selected' || msg.status === 'launched'));

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
  try {
    sendNativeAction({ action: 'get_current_folder' }, (resp) => {
      if (!resp) return;
      if (resp.status === 'success' && resp.folder) {
        if (folderInput) folderInput.value = resp.folder;
        try { browser.storage.local.set({ lastFolder: resp.folder }); } catch (e) { console.warn('Could not persist lastFolder', e); }
      } else if (resp.status === 'folder_selected' && resp.folder) {
        if (folderInput) folderInput.value = resp.folder;
        try { browser.storage.local.set({ lastFolder: resp.folder }); } catch (e) { console.warn('Could not persist lastFolder', e); }
      } else if (resp.status === 'error') {
        console.warn('get_current_folder error:', resp.message);
      }
    }, { timeoutMs: 5000 });
  } catch (e) {
    console.warn('getCurrentFolder send failed:', e);
  }
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
  resultDiv.classList.remove('show', 'success', 'error', 'warning');

  try {
    // keep handle because callback references it for cleanup on non-final error
    const handle = sendNativeAction({ action: 'download', url: url, folder: folderPath || undefined }, (response) => {
      if (!response) return;
      if (typeof response !== 'object') return;
      if (response.status === 'success') {
        if (Object.prototype.hasOwnProperty.call(response, 'final') && response.final === false) {
          showResult(`Downloaded (in-progress): ${response.filename || 'YouTube MP3'}`, 'info');
        } else {
          loader.style.display = 'none';
          downloadBtn.disabled = false;
          showResult(`Downloaded: ${response.filename || 'YouTube MP3'}`, 'success');
        }
      } else if (response.status === 'error') {
        if (Object.prototype.hasOwnProperty.call(response, 'final') && response.final === false) {
          showResult(`Error: ${response.message || 'Download failed'}`, 'error');
          try { loader.style.display = 'none'; downloadBtn.disabled = false; if (urlInput) urlInput.disabled = false; } catch (e) {}
          try { if (handle && typeof handle.cleanup === 'function') handle.cleanup(); } catch (e) {}
        } else {
          loader.style.display = 'none';
          downloadBtn.disabled = false;
          showResult(`Error: ${response.message || 'Download failed'}`, 'error');
        }
      } else if (response.status === 'warning') {
        showResult(`Warning: ${response.message || 'Download warning'}`, 'warning');
      } else if (response.status === 'progress' && response.message) {
        showResult(response.message, 'info');
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

// show/hide helpers on mouse hover only
const inputGroups = document.querySelectorAll('.input-group');
if (inputGroups && inputGroups.length) {
  inputGroups.forEach(group => {
    group.addEventListener('mouseenter', () => {
      const helper = group.querySelector('.helper');
      if (helper && !group.classList.contains('no-helper')) helper.classList.add('helper-visible');
    });
    group.addEventListener('mouseleave', () => {
      const helper = group.querySelector('.helper');
      if (helper) helper.classList.remove('helper-visible');
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
      // removed unused `handle` assignment
      sendNativeAction({ action: 'select_folder' }, (resp) => {
        if (!resp) return;
        if (typeof resp !== 'object') return;
        if ((resp.status === 'folder_selected' || resp.status === 'success') && resp.folder) {
          if (folderInput) folderInput.value = resp.folder;
          try { browser.storage.local.set({ lastFolder: resp.folder }); } catch (e) { console.warn('Could not persist lastFolder', e); }
          showResult('Folder selected', 'success');
        } else if (resp.status === 'error') {
          showResult(`Folder selection failed: ${resp.message || 'cancelled'}`, 'error');
        }
      }, { timeoutMs: 120000 });
    } catch (e) {
      showResult('Failed to open folder chooser', 'error');
    }
  });
}

if (openInAppBtn) {
  openInAppBtn.addEventListener('click', () => {
    // declare urlToOpen locally
    const urlToOpen = isYouTubePage ? currentTabUrl : (urlInput ? urlInput.value.trim() : '');
    if (!urlToOpen) { showResult('Please enter a YouTube URL', 'error'); return; }
    if (!isYouTubeUrl(urlToOpen)) { showResult('Please enter a valid YouTube URL', 'error'); return; }
    showResult('Launching app...', 'info');
    try {
      // removed unused `handle` assignment
      sendNativeAction({ action: 'download_gui', url: urlToOpen }, (resp) => {
        if (!resp) return;
        if (typeof resp !== 'object') return;
        if (resp.status === 'launched' || resp.status === 'success') {
          showResult('App launched', 'success');
        } else if (resp.status === 'error') {
          showResult(`Failed to launch: ${resp.message || 'error'}`, 'error');
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

  resultDiv.classList.remove('show', 'success', 'error', 'warning');

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
      resultDiv.classList.remove('show', 'success', 'error', 'warning');
      try {
        document.body.style.minHeight = INITIAL_BODY_MIN_HEIGHT + 'px';
      } catch (e) { /* ignore */ }
    }, 5000);
  }
}
