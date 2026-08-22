// Get DOM elements
const downloadBtn = document.getElementById('DownloadBtn');
const resultDiv = document.getElementById('result');
const loader = document.getElementById('loader');
const currentUrlContainer = document.getElementById('currentUrlContainer');
const urlInputContainer = document.getElementById('urlInputContainer');
const currentUrlText = document.getElementById('currentUrl');
const urlInput = document.getElementById('urlInput');
const folderInput = document.getElementById('folderInput');
const filenameInput = document.getElementById('filenameInput');

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

// Pending native requests map
const pendingRequests = new Map();

function genRequestId() {
  try { return crypto.randomUUID(); } catch (e) { return 'r' + Date.now() + Math.floor(Math.random()*10000); }
}

// Listen for native responses forwarded by background
browser.runtime.onMessage.addListener((msg) => {
  if (!msg || msg.type !== 'native_response') return;
  const id = msg.requestId;
  if (!id) return;
  const entry = pendingRequests.get(id);
  if (!entry) return;

  // call handler
  try { entry.onMessage(msg); } catch (e) { console.error('Error in native response handler:', e); }

  // decide finality (explicit final authoritative)
  const isFinal = Object.prototype.hasOwnProperty.call(msg, 'final')
    ? Boolean(msg.final)
    : Boolean(msg && (msg.status === 'success' || msg.status === 'error'));

  if (isFinal) {
    try { clearTimeout(entry.timer); } catch (e) {}
    pendingRequests.delete(id);

    // Reset filename input only on successful final message
    if (msg.status === 'success' && filenameInput) {
      filenameInput.value = '';
    }
  }
});

function sendNativeAction(message, onMessage, opts = {}) {
  const requestId = genRequestId();
  const timeoutMs = opts.timeoutMs || 60000;
  const timer = setTimeout(() => {
    try { onMessage({ status: 'error', message: 'Native host timeout', final: true }); } catch (e) {}
    pendingRequests.delete(requestId);
  }, timeoutMs);

  pendingRequests.set(requestId, { onMessage, timer });

  try {
    browser.runtime.sendMessage(Object.assign({ type: 'native_action', requestId }, message));
  } catch (e) {
    clearTimeout(timer);
    pendingRequests.delete(requestId);
    throw e;
  }

  return {
    requestId,
    cleanup: () => {
      const e = pendingRequests.get(requestId);
      if (e) {
        try { clearTimeout(e.timer); } catch (err) {}
        pendingRequests.delete(requestId);
      }
    }
  };
}

// Initialize popup - get current tab URL
browser.tabs.query({ active: true, currentWindow: true }).then((tabs) => {
  if (tabs[0]) {
    currentTabUrl = tabs[0].url;
    isYouTubePage = isYouTubeUrl(currentTabUrl);

    if (isYouTubePage) {
      // Show current URL container
      if (currentUrlText) currentUrlText.textContent = currentTabUrl;
      if (currentUrlContainer) currentUrlContainer.style.display = 'block';
      if (urlInputContainer) urlInputContainer.classList.remove('show');
    } else {
      // Show URL input container
      if (currentUrlContainer) currentUrlContainer.style.display = 'none';
      if (urlInputContainer) urlInputContainer.classList.add('show');
    }
  }

  // Try to load folder from localStorage first
  try {
    browser.storage.local.get('lastFolder').then((result) => {
      if (result && result.lastFolder && folderInput) {
        folderInput.value = result.lastFolder;
        console.log('Loaded folder from localStorage:', result.lastFolder);
      }
    }).catch((e) => {
      console.warn('Could not read storage for lastFolder:', e);
    });
  } catch (e) {
    console.warn('localStorage not available:', e);
  }
}).catch((error) => {
  console.error('Error getting tab info:', error);
  if (currentUrlText) currentUrlText.textContent = 'Error loading URL';
});

// Handle download button click
if (downloadBtn) {
  downloadBtn.addEventListener('click', () => {
    const urlToDownload = isYouTubePage ? currentTabUrl : (urlInput ? urlInput.value.trim() : '');

    if (!urlToDownload) { showResult('Please enter a YouTube URL', 'error'); return; }
    if (!isYouTubeUrl(urlToDownload)) { showResult('Please enter a valid YouTube URL', 'error'); return; }

    startDownload(urlToDownload);
  });
}

// Start download process
function startDownload(url) {
  console.log('Starting download for:', url);

  const folderPath = folderInput ? folderInput.value.trim() : '';
  const filename = filenameInput ? filenameInput.value.trim() : '';

  // Show loading state
  if (downloadBtn) downloadBtn.disabled = true;
  if (loader) loader.style.display = 'block';
  if (resultDiv) resultDiv.classList.remove('show', 'success', 'error', 'warning');

  try {
    sendNativeAction({
      action: 'download',
      url: url,
      folder: folderPath || undefined,
      filename: filename || undefined
    }, (response) => {
      console.log('Native response:', response);

      // display according to status
      if (response.status === 'progress' || response.status === 'info') {
        showResult(response.message || 'Downloading...', 'info');
        return; // wait for final
      }

      if (response.status === 'warning') {
        showResult(response.message || 'Warning', 'warning');
        return; // wait for final
      }

      if (response.status === 'success') {
        // final success
        if (loader) loader.style.display = 'none';
        if (downloadBtn) downloadBtn.disabled = false;
        showResult(response.message || (`Downloaded: ${response.filename || 'YouTube MP3'}`), 'success');
        return;
      }

      if (response.status === 'error') {
        // final error
        if (loader) loader.style.display = 'none';
        if (downloadBtn) downloadBtn.disabled = false;
        showResult(response.message || 'Download failed', 'error');
        return;
      }

      // fallback: if explicit final flag present and true, conclude
      if (response.final === true) {
        if (loader) loader.style.display = 'none';
        if (downloadBtn) downloadBtn.disabled = false;
      }
    }, { timeoutMs: 120000 });

  } catch (e) {
    console.error('Error starting download:', e);
    if (loader) loader.style.display = 'none';
    if (downloadBtn) downloadBtn.disabled = false;
    showResult(`Error: ${e.message || e}`, 'error');
  }
}

// Show result message
function showResult(message, type) {
  if (!resultDiv) return;
  if (!message || !message.toString().trim()) return;

  resultDiv.textContent = message;
  resultDiv.classList.remove('show', 'success', 'error', 'warning', 'info');
  resultDiv.classList.add('show', type);

  // Auto-hide after 5 seconds for success messages
  if (type === 'success') {
    setTimeout(() => {
      if (!resultDiv) return;
      resultDiv.classList.remove('show', 'success', 'error', 'warning', 'info');
    }, 5000);
  }
}