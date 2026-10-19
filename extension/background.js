/* global browser */

const NATIVE_HOST_NAME = 'app_manifest';
const DEFAULT_TIMEOUT_MS = 40000;

// Sidebar-only mode: no toolbar icon, focus on sidebar and context menu
// Track active requests and whether sidebar was open when they were initiated
const activeRequests = new Map();


// Create context menu when extension is installed
browser.runtime.onInstalled.addListener(() => {
  console.log('YouTube MP3 Downloader installed (sidebar-only mode)');

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
async function isSidebarOpen() {
  try {
    if (browser.sidebarAction && browser.sidebarAction.isOpen) {
      return await browser.sidebarAction.isOpen({});
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

// In sidebar-only mode, show notifications only when sidebar is closed
async function showNotification(title, message, type) {
  // Skip notification if sidebar is visible since user will see status there
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

// Handle context menu clicks - primary interaction point for sidebar-only mode
browser.contextMenus.onClicked.addListener((info) => {
  if (info.menuItemId !== 'download-mp3') return;
  const url = info.linkUrl;
  if (isYouTubeUrl(url)) {
    // Initiate download via native host
    initiateDownload({ action: 'download', url: url }).catch(err => console.error('Download initiation error:', err));
  } else {
    showNotification('Invalid URL', 'Not a valid YouTube URL', 'error');
  }
});

// Core: for each download request, connect to native host and forward messages
async function initiateDownload(payload, opts = {}) {
  // payload should include at least { action: 'download', url: '...', folder?: '...' }
  const requestId = payload.requestId || (typeof crypto !== 'undefined' && crypto.randomUUID ? crypto.randomUUID() : 'r' + Date.now());
  // attach requestId so native can echo it
  payload.requestId = requestId;

  // Track whether sidebar was open when this request was initiated
  // Only show notifications for requests initiated with sidebar closed
  const sidebarWasOpenAtInitiation = await isSidebarOpen().catch(() => false);
  activeRequests.set(requestId, {
    sidebarWasOpen: sidebarWasOpenAtInitiation,
    startTime: Date.now()
  });

  let port;
  try {
    port = browser.runtime.connectNative(NATIVE_HOST_NAME);
  } catch (e) {
    console.error('connectNative failed:', e);
    // forward error to any listeners
    try { browser.runtime.sendMessage({ type: 'native_response', requestId, status: 'error', message: 'Could not connect to native host', final: true }); } catch (err) {}
    // Only show notification if sidebar was closed at initiation
    if (!sidebarWasOpenAtInitiation) {
      showNotification('Connection Error', 'Could not connect to native host', 'error');
    }
    activeRequests.delete(requestId);
    return;
  }

  // optional timeout to notify if no final message arrives
  const timeoutMs = opts.timeoutMs || DEFAULT_TIMEOUT_MS;
  const timeoutHandle = setTimeout(() => {
    try { browser.runtime.sendMessage({ type: 'native_response', requestId, status: 'error', message: 'Native host timeout', final: true }); } catch (e) {}
    // Only show notification if sidebar was closed at initiation
    if (!sidebarWasOpenAtInitiation) {
      showNotification('Timeout', 'Native host did not respond in time', 'warning');
    }
    // do not forcefully disconnect port; let it close naturally
  }, timeoutMs);

  port.onMessage.addListener((response) => {
    // forward to sidebar/popup listeners
    try { browser.runtime.sendMessage(Object.assign({ type: 'native_response', requestId }, response)); } catch (e) { console.warn('Failed to forward native response', e); }

    // If response is explicit final, clear timeout and show notification
    const isFinal = Object.prototype.hasOwnProperty.call(response, 'final') ? Boolean(response.final) : (response && (response.status === 'success' || response.status === 'error'));
    if (isFinal) {
      try { clearTimeout(timeoutHandle); } catch (e) {}

      // Only show notifications if sidebar was closed when request was initiated
      // This ensures sidebar can display its own UI without duplicate notifications
      const requestInfo = activeRequests.get(requestId);
      const shouldNotify = requestInfo && !requestInfo.sidebarWasOpen;

      if (shouldNotify) {
        // Show all final states: success, error, and any other final status
        if (response.status === 'success') {
          showNotification('Download Complete', `${response.filename || 'YouTube MP3'} saved successfully!`, 'success');
        } else if (response.status === 'error') {
          showNotification('Download Failed', response.message || 'An error occurred during download', 'error');
        }
      }

      // Clean up
      activeRequests.delete(requestId);
      // Do NOT call port.disconnect() here to avoid potential focus changes; allow native host lifecycle to control it.
    }
  });

  port.onDisconnect.addListener(() => {
    try { clearTimeout(timeoutHandle); } catch (e) {}
    const err = browser.runtime.lastError;
    if (err) {
      console.error('Native messaging error:', err);
      try { browser.runtime.sendMessage({ type: 'native_response', requestId, status: 'error', message: err.message || String(err), final: true }); } catch (e) {}
      // Only show notification if sidebar was closed at initiation
      const requestInfo = activeRequests.get(requestId);
      if (requestInfo && !requestInfo.sidebarWasOpen) {
        showNotification('Connection Error', 'Native host disconnected', 'error');
      }
    }
    // listeners removed by browser automatically when port closed; nothing else to do
  });

  // post the payload
  try {
    port.postMessage(payload);
  } catch (e) {
    console.error('Failed to post message to native host:', e);
    try { browser.runtime.sendMessage({ type: 'native_response', requestId, status: 'error', message: 'Failed to send to native host', final: true }); } catch (err) {}
    // Only show notification if sidebar was closed at initiation
    if (!sidebarWasOpenAtInitiation) {
      showNotification('Send Failed', 'Could not send message to native host', 'error');
    }
    try { clearTimeout(timeoutHandle); } catch (ignore) {}
    activeRequests.delete(requestId);
  }
}

// Bridge: listen for sidebar/popup requests (support all MP3.py actions)
browser.runtime.onMessage.addListener((msg) => {
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
  if (msg.filename) payload.filename = msg.filename;
  if (msg.requestId) payload.requestId = msg.requestId;

  initiateDownload(payload, { timeoutMs: msg.timeoutMs || DEFAULT_TIMEOUT_MS }).catch(err => console.error('Download error:', err));

  return true;
});
