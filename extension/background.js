// @ts-check
/// <reference types="firefox-webext-browser" />
/* global browser */
/* noinspection JSDeprecatedSymbols,JSUnresolvedVariable */

/**
 * JSDoc help for the runtime message shape we forward to popups.
 * @typedef {{status?:string, message?:string, filename?:string, folder?:string, final?:boolean}} NativeResponse
 */

/**
 * Send a native request queued via the background native port.
 * @param {Object} message
 * @param {string} requestId
 * @param {{timeoutMs?:number}=} opts
 * @returns {Promise<NativeResponse>}
 */

// Create context menu when extension is installed
browser.runtime.onInstalled.addListener(() => {
  console.log('YouTube MP3 Downloader installed');

  // Create right-click context menu for YouTube links
  browser.contextMenus.create({
    id: "download-mp3",
    title: "Download as MP3",
    contexts: ["link"],
    targetUrlPatterns: [
      "*://www.youtube.com/watch*",
      "*://youtube.com/watch*",
      "*://youtu.be/*",
      "*://m.youtube.com/watch*",
      "*://www.youtube.com/shorts/*",
      "*://youtube.com/shorts/*"
    ]
  });
});

// Handle context menu clicks
browser.contextMenus.onClicked.addListener((info) => {
  if (info.menuItemId === "download-mp3") {
    const url = info.linkUrl;
    console.log('Context menu download initiated for:', url);

    // Validate it's a YouTube URL
    if (isYouTubeUrl(url)) {
      downloadMP3(url);
    } else {
      showNotification('Invalid URL', 'Not a valid YouTube URL', 'error');
    }
  }
});

// Check if URL is a valid YouTube URL
function isYouTubeUrl(url) {
  return url && (
    url.includes('youtube.com/watch') ||
    url.includes('youtu.be/') ||
    url.includes('youtube.com/shorts/')
  );
}

// Persistent native port and queued dispatch so the native process stays alive
let nativePort = null;
let nativeBusy = false;
let nativeQueue = [];
let currentRequest = null;

function connectNative() {
  if (nativePort) return;
  try {
    nativePort = browser.runtime.connectNative('app_manifest');
    console.log('Connected native port');

    nativePort.onMessage.addListener((response) => {
      // forward native responses to the current requester
      if (!currentRequest) {
        console.warn('Native response received with no current request:', response);
        return;
      }
      // forward to popup(s) via runtime.sendMessage
      try {
        browser.runtime.sendMessage(Object.assign({ type: 'native_response', requestId: currentRequest.requestId }, response));
      } catch (e) {
        console.warn('Could not forward native response to popup:', e);
      }

      // determine finality
      let isFinal = false;
      if (Object.prototype.hasOwnProperty.call(response, 'final')) {
        isFinal = Boolean(response.final);
      } else {
        isFinal = (response && (response.status === 'success' || response.status === 'error' || response.status === 'folder_selected' || response.status === 'launched'));
      }

      if (isFinal) {
        // resolve the pending promise
        try { currentRequest.resolve(response); } catch (e) {}
        currentRequest = null;
        nativeBusy = false;
        processNativeQueue();
      }
    });

    nativePort.onDisconnect.addListener(() => {
      console.error('Native port disconnected');
      // notify pending request of disconnect
      if (currentRequest && currentRequest.reject) {
        try { currentRequest.reject(new Error('Native host disconnected')); } catch (e) {}
      }
      currentRequest = null;
      nativeBusy = false;
      nativePort = null;
      // try to reconnect after a short delay
      setTimeout(() => { connectNative(); processNativeQueue(); }, 1500);
    });
  } catch (e) {
    console.error('Failed to connect native port:', e);
    nativePort = null;
    setTimeout(() => { connectNative(); }, 3000);
  }
}

function processNativeQueue() {
  if (nativeBusy) return;
  if (!nativePort) connectNative();
  if (!nativePort) return; // will retry
  if (nativeQueue.length === 0) return;

  const item = nativeQueue.shift();
  currentRequest = item;
  nativeBusy = true;
  try {
    nativePort.postMessage(item.message);
  } catch (e) {
    console.error('Failed to post message to native port:', e);
    // reject and continue
    try { item.reject(e); } catch (err) {}
    currentRequest = null;
    nativeBusy = false;
    setTimeout(processNativeQueue, 50);
  }
}

function sendNativeRequest(message, requestId, opts = {}) {
  return new Promise((resolve, reject) => {
    nativeQueue.push({ message: message, requestId: requestId, resolve: resolve, reject: reject, opts: opts });
    processNativeQueue();
  });
}

// Start a persistent native connection immediately
connectNative();

// Now handle messages from popup/background pages asking to send native actions
browser.runtime.onMessage.addListener((msg, sender) => {
  if (!msg || msg.type !== 'native_action') return;
  const requestId = msg.requestId || `r${Date.now()}`;
  const payload = Object.assign({}, msg);
  // strip internal fields before sending to native
  delete payload.type;
  delete payload.requestId;

  // send via queue and respond when final arrives
  sendNativeRequest(payload, requestId, { timeoutMs: msg.timeoutMs || 30000 })
    .then((finalResp) => {
      // final response already forwarded to popup via runtime.sendMessage; also send a direct response to the sender if needed
      // reply using sendResponse via returning a Promise
    }).catch((err) => {
      // notify the popup of the error
      try {
        browser.runtime.sendMessage({ type: 'native_response', requestId: requestId, status: 'error', message: err && err.message ? err.message : 'Native host error', final: true });
      } catch (e) {}
    });
  // Indicate we will respond asynchronously (not using sendResponse here because we forward messages)
  return true;
});

// Try to populate stored lastFolder at startup if missing
async function ensureStoredFolder() {
  try {
    const data = await browser.storage.local.get('lastFolder');
    if (data && data.lastFolder) return; // already present
  } catch (e) {
    console.warn('Could not read storage for lastFolder:', e);
  }

  // ask native host for current folder (best-effort)
  try {
    sendNativeRequest({ action: 'get_current_folder' }, 'get_current_folder_request')
      .then((resp) => {
        if (!resp) return;
        if ((resp.status === 'success' || resp.status === 'folder_selected') && resp.folder) {
          try { browser.storage.local.set({ lastFolder: resp.folder }); } catch (e) { console.warn('Could not persist lastFolder', e); }
        }
      });
  } catch (e) {
    console.warn('Could not request get_current_folder from host:', e);
  }
}

// Run ensureStoredFolder at install and startup
browser.runtime.onInstalled.addListener(() => {
  ensureStoredFolder();
});
// also run once when background loads
ensureStoredFolder();

// Download MP3 from YouTube URL
async function downloadMP3(url) {
  console.log('Starting download for:', url);

  // Show initial notification
  showNotification('Download Started', 'Downloading MP3...', 'info');

  // try to get lastFolder from storage
  let folder = null;
  try {
    const data = await browser.storage.local.get('lastFolder');
    if (data && data.lastFolder) folder = data.lastFolder;
  } catch (e) {
    console.warn('Error reading lastFolder from storage:', e);
  }

  // If we don't have a folder, try to request it from host (best-effort short timeout)
  if (!folder) {
    try {
      await new Promise((resolve) => {
        sendNativeRequest({ action: 'get_current_folder' }, 'get_current_folder_request')
          .then((resp) => {
            if (!resp) return resolve();
            if ((resp.status === 'success' || resp.status === 'folder_selected') && resp.folder) {
              folder = resp.folder;
              try { browser.storage.local.set({ lastFolder: folder }); } catch (e) { console.warn('Could not persist lastFolder', e); }
            }
            resolve();
          });
      });
    } catch (e) {
      console.warn('get_current_folder fallback failed:', e);
    }
  }

  // Now send the download action with optional folder
  try {
    sendNativeRequest({ action: 'download', url: url, folder: folder || undefined }, 'download_request')
      .then((response) => {
        console.log('Received response from native app:', response);
        if (!response) return;
        if (response.status === 'success') {
          // if response has explicit final:false, treat as informational (in-progress)
          if (Object.prototype.hasOwnProperty.call(response, 'final') && response.final === false) {
            showNotification('Downloading', response.message || (response.filename ? `Processing ${response.filename}` : 'Download in progress'), 'info');
          } else {
            showNotification('Download Complete', `${response.filename || 'YouTube MP3'} saved successfully!`, 'success');
          }
        } else if (response.status === 'error') {
          // if non-final error, show notification but keep connection open (sendOneOffNative controls cleanup)
          if (Object.prototype.hasOwnProperty.call(response, 'final') && response.final === false) {
            showNotification('Download Issue', response.message || 'Download reported an issue', 'warning');
          } else {
            showNotification('Download Failed', response.message || 'An error occurred during download', 'error');
          }
        } else if (response.status === 'warning') {
          showNotification('Warning', response.message || 'Download completed with warnings', 'warning');
        } else if (response.status === 'progress' && response.message) {
          // transient info/progress notifications — show as info
          showNotification('Downloading', response.message, 'info');
        }
      });

  } catch (error) {
    console.error('Error initiating download:', error);
    showNotification(
      'Error',
      `Failed to start download: ${error.message}`,
      'error'
    );
  }
}

// Show browser notification
function showNotification(title, message, type) {
  const iconMap = {
    success: 'icons/icon-48.png',
    error: 'icons/icon-48.png',
    warning: 'icons/icon-48.png',
    info: 'icons/icon-48.png'
  };

  browser.notifications.create({
    type: 'basic',
    iconUrl: iconMap[type] || 'icons/icon-48.png',
    title: title,
    message: message
  });
}
