/* global browser, isYouTubeUrl */

const downloadBtn = document.getElementById('DownloadBtn');
const openInAppBtn = document.getElementById('openInAppBtn');
const selectFolderBtn = document.getElementById('selectFolderBtn');
const resultDiv = document.getElementById('result');
const loader = document.getElementById('loader');
const urlInput = document.getElementById('urlInput');
const urlLabel = document.querySelector('label[for="urlInput"]');
const folderInput = document.getElementById('folderInput');
const filenameInput = document.getElementById('filenameInput');

// ----- Native requests (relayed through background.js) -----

// requestId -> response handler. background.js guarantees one final response per request.
const pendingRequests = new Map();

browser.runtime.onMessage.addListener((msg) => {
  if (!msg || msg.type !== 'native_response') return;
  const handler = pendingRequests.get(msg.requestId);
  if (!handler) return;
  if (msg.final) pendingRequests.delete(msg.requestId);
  try {
    handler(msg);
  } catch (e) {
    console.error('Error in native response handler:', e);
  }
});

function sendNativeAction(message, onResponse) {
  const requestId = crypto.randomUUID();
  pendingRequests.set(requestId, onResponse);
  browser.runtime.sendMessage(Object.assign({ type: 'native_action', requestId }, message)).catch((e) => {
    pendingRequests.delete(requestId);
    onResponse({ status: 'error', message: `Could not reach the extension background: ${e.message}`, final: true });
  });
}

// ----- URL field -----

// The sidebar outlives tab switches and YouTube's in-page navigation, so keep the URL
// field in sync with the active tab, unless the user has typed a URL of their own.
let autofilledUrl = '';

async function syncUrlFromActiveTab() {
  const current = urlInput.value.trim();
  if (current && current !== autofilledUrl) return;

  const [tab] = await browser.tabs.query({ active: true, currentWindow: true });
  const url = tab && isYouTubeUrl(tab.url) ? tab.url : '';
  urlInput.value = url;
  autofilledUrl = url;
  urlLabel.textContent = url ? 'Current URL' : 'YouTube URL';
}

browser.tabs.onActivated.addListener(() => syncUrlFromActiveTab().catch(console.error));
browser.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (tab.active) syncUrlFromActiveTab().catch(console.error);
}, { properties: ['url'] });

syncUrlFromActiveTab()
  .catch(e => console.error('Error getting tab info:', e))
  .finally(() => {
    urlInput.focus();
    urlInput.select();
  });

// ----- Folder field -----

function saveFolder(folder) {
  const op = folder ? browser.storage.local.set({ lastFolder: folder }) : browser.storage.local.remove('lastFolder');
  op.catch(e => console.error('Could not save folder:', e));
}

function setFolder(folder) {
  folderInput.value = folder;
  saveFolder(folder);
}

async function loadFolder() {
  const { lastFolder } = await browser.storage.local.get('lastFolder');
  if (lastFolder) {
    folderInput.value = lastFolder;
    return;
  }
  sendNativeAction({ action: 'get_current_folder' }, (resp) => {
    if (resp.status === 'success' && resp.folder) setFolder(resp.folder);
    else if (resp.status === 'error') console.error('Could not get default folder:', resp.message);
  });
}

loadFolder().catch(e => console.error('Could not load folder:', e));

// A typed folder becomes the new default too; clearing it falls back to the host default next time.
folderInput.addEventListener('change', () => saveFolder(folderInput.value.trim()));

selectFolderBtn.addEventListener('click', () => {
  showResult('Opening folder chooser...', 'info');
  sendNativeAction({ action: 'select_folder' }, (resp) => {
    if (resp.status === 'success' && resp.folder) {
      setFolder(resp.folder);
      showResult('Folder selected', 'success');
    } else if (resp.status === 'error') {
      showResult(resp.message || 'Folder selection failed', 'error');
    }
  });
});

// ----- Actions -----

function setBusy(busy) {
  downloadBtn.disabled = busy;
  loader.style.display = busy ? 'block' : 'none';
}

downloadBtn.addEventListener('click', () => {
  const url = urlInput.value.trim();
  if (!url) {
    showResult('Please enter a YouTube URL', 'error');
    return;
  }
  if (!isYouTubeUrl(url)) {
    showResult('Please enter a valid YouTube URL', 'error');
    return;
  }

  setBusy(true);
  clearResult();
  sendNativeAction({
    action: 'download',
    url,
    folder: folderInput.value.trim() || undefined,
    filename: filenameInput.value.trim() || undefined
  }, (resp) => {
    if (!resp.final) {
      if (resp.message) showResult(resp.message, resp.status === 'warning' ? 'warning' : 'info');
      return;
    }
    setBusy(false);
    if (resp.status === 'success') {
      filenameInput.value = '';
      showResult(resp.message || `Downloaded: ${resp.filename || 'YouTube MP3'}`, 'success');
    } else {
      showResult(resp.message || 'Download failed', 'error');
    }
  });
});

openInAppBtn.addEventListener('click', () => {
  // Everything is optional here; the user can fill it in inside the app.
  showResult('Launching app...', 'info');
  sendNativeAction({
    action: 'download_gui',
    url: urlInput.value.trim() || undefined,
    folder: folderInput.value.trim() || undefined,
    filename: filenameInput.value.trim() || undefined
  }, (resp) => {
    if (resp.status === 'success') showResult(resp.message || 'App launched', 'success');
    else if (resp.status === 'error') showResult(resp.message || 'Failed to launch app', 'error');
  });
});

// ----- Result pill -----

let hideTimer = null;

function clearResult() {
  clearTimeout(hideTimer);
  resultDiv.className = '';
  resultDiv.textContent = '';
}

function showResult(message, type) {
  clearTimeout(hideTimer);
  resultDiv.textContent = message;
  resultDiv.className = `show ${type}`;
  resultDiv.scrollIntoView({ block: 'nearest' });
  if (type === 'success') hideTimer = setTimeout(clearResult, 5000);
}
