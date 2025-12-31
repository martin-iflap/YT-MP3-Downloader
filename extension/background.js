/* global browser */

const NATIVE_HOST_NAME = 'app_manifest';
const DEFAULT_TIMEOUT_MS = 40000;

// Create context menu when extension is installed
browser.runtime.onInstalled.addListener(() => {
  console.log('YouTube MP3 Downloader installed');

  try {
    browser.contextMenus.create({
      id: 'download-mp3',
      title: 'Download as MP3',
      contexts: ['link'],
      targetUrlPatterns: [
        '*://www.youtube.com/watch*',
        '*://youtube.com/watch*',
        '*://youtu.be/*',
        '*://m.youtube.com/watch*',
        '*://www.youtube.com/shorts/*',
        '*://youtube.com/shorts/*'
      ]
    });
  } catch (e) {
    console.warn('Could not create context menu:', e);
  }
});

function isYouTubeUrl(url) {
  return url && (
    url.includes('youtube.com/watch') ||
    url.includes('youtu.be/') ||
    url.includes('youtube.com/shorts/')
  );
}

// Check if sidebar is open
function isSidebarOpen() {
  try {
    if (browser.sidebarAction && browser.sidebarAction.isOpen) {
      return browser.sidebarAction.isOpen({});
    }
  } catch (e) {
    // fallback: check if any sidebar views exist
    try {
      if (browser.extension && browser.extension.getViews) {
        const views = browser.extension.getViews({ type: 'sidebar' });
        return Array.isArray(views) && views.length > 0;
      }
    } catch (err) {
      // ignore
    }
  }
  return false;
}

// Simple notification helper - skip if sidebar is open
async function showNotification(title, message, type) {
  // Don't show notifications if sidebar is visible
  const sidebarOpen = await isSidebarOpen().catch(() => false);
  if (sidebarOpen) {
    console.log('Sidebar open — skipping notification:', title, message);
    return;
  }

  const iconMap = {
    success: 'icons/icon-48.png',
    error: 'icons/icon-48.png',
    warning: 'icons/icon-48.png',
    info: 'icons/icon-48.png'
  };
  try {
    browser.notifications.create({
      type: 'basic',
      iconUrl: iconMap[type] || iconMap.info,
      title,
      message
    });
  } catch (e) {
    console.warn('Notification create failed:', e);
  }
}

// Handle context menu clicks
browser.contextMenus.onClicked.addListener((info) => {
  if (info.menuItemId !== 'download-mp3') return;
  const url = info.linkUrl;
  if (isYouTubeUrl(url)) {
    // use same flow as popup: fire off native download
    initiateDownload({ action: 'download', url: url });
  } else {
    showNotification('Invalid URL', 'Not a valid YouTube URL', 'error');
  }
});

// Core: for each download request, connect to native host and forward messages
function initiateDownload(payload, opts = {}) {
  // payload should include at least { action: 'download', url: '...', folder?: '...' }
  const requestId = payload.requestId || (typeof crypto !== 'undefined' && crypto.randomUUID ? crypto.randomUUID() : 'r' + Date.now());
  // attach requestId so native can echo it
  payload.requestId = requestId;

  let port;
  try {
    port = browser.runtime.connectNative(NATIVE_HOST_NAME);
  } catch (e) {
    console.error('connectNative failed:', e);
    // forward error to any listeners
    try { browser.runtime.sendMessage({ type: 'native_response', requestId, status: 'error', message: 'Could not connect to native host', final: true }); } catch (err) {}
    showNotification('Connection Error', 'Could not connect to native host', 'error');
    return;
  }

  // optional timeout to notify if no final message arrives
  const timeoutMs = opts.timeoutMs || DEFAULT_TIMEOUT_MS;
  const timeoutHandle = setTimeout(() => {
    try { browser.runtime.sendMessage({ type: 'native_response', requestId, status: 'error', message: 'Native host timeout', final: true }); } catch (e) {}
    showNotification('Timeout', 'Native host did not respond in time', 'warning');
    // do not forcefully disconnect port; let it close naturally
  }, timeoutMs);

  port.onMessage.addListener((response) => {
    // forward to popup(s)
    try { browser.runtime.sendMessage(Object.assign({ type: 'native_response', requestId }, response)); } catch (e) { console.warn('Failed to forward native response', e); }

    // If response is explicit final, clear timeout and show notification
    const isFinal = Object.prototype.hasOwnProperty.call(response, 'final') ? Boolean(response.final) : (response && (response.status === 'success' || response.status === 'error'));
    if (isFinal) {
      try { clearTimeout(timeoutHandle); } catch (e) {}
      // Show summary notification
      if (response.status === 'success') {
        showNotification('Download Complete', `${response.filename || 'YouTube MP3'} saved successfully!`, 'success');
      } else if (response.status === 'error') {
        showNotification('Download Failed', response.message || 'An error occurred during download', 'error');
      } else if (response.status === 'warning') {
        showNotification('Warning', response.message || 'Download completed with warnings', 'warning');
      }
      // Do NOT call port.disconnect() here to avoid potential focus changes; allow native host lifecycle to control it.
    }
  });

  port.onDisconnect.addListener(() => {
    try { clearTimeout(timeoutHandle); } catch (e) {}
    const err = browser.runtime.lastError;
    if (err) {
      console.error('Native messaging error:', err);
      try { browser.runtime.sendMessage({ type: 'native_response', requestId, status: 'error', message: err.message || String(err), final: true }); } catch (e) {}
      showNotification('Connection Error', 'Native host disconnected', 'error');
    }
    // listeners removed by browser automatically when port closed; nothing else to do
  });

  // post the payload
  try {
    port.postMessage(payload);
  } catch (e) {
    console.error('Failed to post message to native host:', e);
    try { browser.runtime.sendMessage({ type: 'native_response', requestId, status: 'error', message: 'Failed to send to native host', final: true }); } catch (err) {}
    showNotification('Send Failed', 'Could not send message to native host', 'error');
    try { clearTimeout(timeoutHandle); } catch (ignore) {}
  }
}

// Bridge: listen for popup/sidebar requests (support all mp3_2.py actions)
browser.runtime.onMessage.addListener((msg, sender) => {
  if (!msg || msg.type !== 'native_action') return;

  const action = msg.action;
  const allowedActions = ['download', 'download_gui', 'select_folder', 'get_current_folder'];

  if (!allowedActions.includes(action)) {
    try { browser.runtime.sendMessage({ type: 'native_response', requestId: msg.requestId || null, status: 'error', message: 'Unsupported action', final: true }); } catch (e) {}
    return;
  }

  const payload = { action: action };
  if (msg.url) payload.url = msg.url;
  if (msg.folder) payload.folder = msg.folder;
  if (msg.requestId) payload.requestId = msg.requestId;

  initiateDownload(payload, { timeoutMs: msg.timeoutMs || DEFAULT_TIMEOUT_MS });

  return true;
});

// End of background.js
