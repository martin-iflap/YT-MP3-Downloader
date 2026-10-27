/* global isKnownSiteUrl, isWebUrl */

const downloadBtn = document.getElementById('DownloadBtn');
const openInAppBtn = document.getElementById('openInAppBtn');
const selectFolderBtn = document.getElementById('selectFolderBtn');
const resultDiv = document.getElementById('result');
const hostNotice = document.getElementById('hostNotice');
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
  const url = tab && isKnownSiteUrl(tab.url) ? tab.url : '';
  urlInput.value = url;
  autofilledUrl = url;
  urlLabel.textContent = url ? 'Current URL' : 'Video URL';
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
    // a missing app is shown by checkHost()
    if (resp.status === 'success' && resp.folder) setFolder(resp.folder);
    else if (resp.status === 'error') console.error('Could not get default folder:', resp.message);
  });
}

loadFolder().catch(e => console.error('Could not load folder:', e));

// A typed folder becomes the new default too; clearing it falls back to the host default next time.
folderInput.addEventListener('change', () => saveFolder(folderInput.value.trim()));

selectFolderBtn.addEventListener('click', () => {
  showResult('Opening folder chooser...', 'info');
  // The picker starts at the current folder and returns it unchanged on cancel.
  sendNativeAction({ action: 'select_folder', folder: folderInput.value.trim() || undefined }, (resp) => {
    if (resp.status === 'success' && resp.folder) {
      setFolder(resp.folder);
      showResult('Folder selected', 'success');
    } else if (resp.status === 'error') {
      showResult(resp.message || 'Folder selection failed', 'error', resp.helpUrl);
    }
  });
});

// ----- App status -----

// Checked on every sidebar open. background.js repairs what it can (updates the app and
// yt-dlp, reporting each step as an intermediate response) and then turns the result into
// one status: success, or the most important problem left (not installed, out of date,
// missing or broken tools).
function checkHost() {
  sendNativeAction({ action: 'get_version' }, (resp) => {
    if (resp.final && resp.status === 'success') {
      hostNotice.className = '';
      hostNotice.textContent = '';
      return;
    }
    const type = !resp.final ? 'info' : resp.status === 'warning' ? 'warning' : 'error';
    hostNotice.replaceChildren(messageWithLink(resp.message || 'Could not check the app', resp.helpUrl));
    hostNotice.className = `show ${type}`;
  });
}

checkHost();

// ----- Toolbar button tip -----

// background.js says whether to show it: the sidebar was opened without the toolbar
// button while that is pinned, so the user may want to free its space.
const toolbarTip = document.getElementById('toolbarTip');

browser.runtime.sendMessage({ type: 'toolbar_tip' })
  .then((show) => { if (show) toolbarTip.className = 'show info'; })
  .catch(e => console.error('Could not check the toolbar tip:', e));

document.getElementById('toolbarTipDismiss').addEventListener('click', () => {
  toolbarTip.className = '';
  browser.storage.local.set({ toolbarTipDismissed: true }).catch(e => console.error('Could not save the tip:', e));
});

// ----- Actions -----

function setBusy(busy) {
  downloadBtn.disabled = busy;
  loader.style.display = busy ? 'block' : 'none';
}

downloadBtn.addEventListener('click', () => {
  const url = urlInput.value.trim();
  if (!url) {
    showResult('Please enter a video URL', 'error');
    return;
  }
  if (!isWebUrl(url)) {
    showResult('Please enter a valid URL (starting with https://)', 'error');
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
      showResult(resp.message || `Downloaded: ${resp.filename || 'the MP3'}`, 'success', undefined, resp.path);
    } else {
      showResult(resp.message || 'Download failed', 'error', resp.helpUrl);
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
    else if (resp.status === 'error') showResult(resp.message || 'Failed to launch app', 'error', resp.helpUrl);
  });
});

// ----- Result pill -----

let hideTimer = null;

function clearResult() {
  clearTimeout(hideTimer);
  resultDiv.className = '';
  resultDiv.textContent = '';
}

// The installer is not code-signed, so Firefox and Windows SmartScreen warn about it on first download/run.
const UNSIGNED_INSTALLER_HINT = 'Firefox or Windows may warn that the installer is uncommon or from an unknown publisher, '
  + 'because it is not code-signed. In Firefox allow the download; in "Windows protected your PC" click "More info", then "Run anyway".';

// helpUrl (set by background.js, e.g. when the app is not installed) adds a link after the message.
function messageWithLink(message, helpUrl) {
  const text = document.createElement('span');
  text.textContent = message;
  if (helpUrl) {
    const link = document.createElement('a');
    link.href = helpUrl;
    link.target = '_blank';
    link.rel = 'noopener';
    link.textContent = 'Download the installer';
    const hint = document.createElement('small');
    hint.textContent = UNSIGNED_INSTALLER_HINT;
    text.append(' ', link, hint);
  }
  return text;
}

// "Show in folder" and "Play" for a downloaded MP3; the host opens it.
function fileActions(path) {
  const row = document.createElement('div');
  row.className = 'file-actions';
  for (const [label, action] of [['Show in folder', 'show_in_folder'], ['Play', 'play']]) {
    const button = document.createElement('button');
    button.className = 'secondary-btn secondary-narrow';
    button.textContent = label;
    button.addEventListener('click', () => sendNativeAction({ action, path }, (resp) => {
      if (resp.final && resp.status === 'error') showResult(resp.message || 'Could not open the MP3', 'error', resp.helpUrl);
    }));
    row.append(button);
  }
  return row;
}

// path (a downloaded MP3) adds its file buttons and keeps the result until the next one.
function showResult(message, type, helpUrl, path) {
  clearTimeout(hideTimer);
  const content = messageWithLink(message, helpUrl);
  if (path) content.append(fileActions(path));
  resultDiv.replaceChildren(content);
  resultDiv.className = `show ${type}`;
  resultDiv.scrollIntoView({ block: 'nearest' });
  if (type === 'success' && !path) hideTimer = setTimeout(clearResult, 5000);
}
