/* global browser, isYouTubeUrl */

const NATIVE_HOST_NAME = 'app_manifest';
const MENU_ID = 'download-mp3';
const NOTIFICATION_ICON = 'icons/icon-48.png';

// Per-action timeouts. The host allows 15s for the title lookup plus 300s for yt-dlp,
// and select_folder waits on the user, so these must stay well above that.
const ACTION_TIMEOUTS_MS = {
  download: 6 * 60 * 1000,
  download_gui: 30 * 1000,
  select_folder: 10 * 60 * 1000,
  get_current_folder: 15 * 1000
};

// download_gui keeps the host process alive to run the Tk window after its final
// response; disconnecting the port would make Firefox kill it.
const KEEP_ALIVE_ACTIONS = new Set(['download_gui']);

// Menus are not persisted across browser restarts for MV2 persistent backgrounds,
// so create it on every startup rather than in onInstalled.
browser.contextMenus.create({
  id: MENU_ID,
  title: 'Download as MP3',
  contexts: ['link'],
  targetUrlPatterns: [
    '*://www.youtube.com/watch*',
    '*://youtube.com/watch*',
    '*://m.youtube.com/watch*',
    '*://youtu.be/*',
    '*://www.youtube.com/shorts/*',
    '*://youtube.com/shorts/*',
    '*://m.youtube.com/shorts/*'
  ]
}, () => {
  if (browser.runtime.lastError) console.warn('Could not create context menu:', browser.runtime.lastError);
});

// Forward a host response to extension pages; rejects when none are open, which is fine.
function relay(message) {
  browser.runtime.sendMessage(Object.assign({ type: 'native_response' }, message)).catch(() => {});
}

async function isSidebarOpen() {
  try {
    return await browser.sidebarAction.isOpen({});
  } catch (e) {
    return false;
  }
}

function notify(title, message) {
  browser.notifications.create({ type: 'basic', iconUrl: NOTIFICATION_ICON, title, message })
    .catch(e => console.warn('Notification create failed:', e));
}

// The sidebar shows results for its own requests; notify only when nobody would see them.
async function notifyDownloadResult(origin, response) {
  if (origin === 'sidebar' && await isSidebarOpen()) return;
  if (response.status === 'success') {
    notify('Download Complete', `${response.filename || 'YouTube MP3'} saved successfully!`);
  } else {
    notify('Download Failed', response.message || 'An error occurred during download');
  }
}

// Run one action on a fresh host process. Guarantees exactly one final response
// per requestId: from the host, a disconnect, or the timeout.
function runNativeAction(payload, requestId, origin) {
  const action = payload.action;
  let port = null;
  let done = false;

  const finish = (response, disconnect) => {
    if (done) return;
    done = true;
    clearTimeout(timer);
    relay(Object.assign({}, response, { requestId, final: true }));
    if (action === 'download') notifyDownloadResult(origin, response);
    if (disconnect && port && !KEEP_ALIVE_ACTIONS.has(action)) port.disconnect();
  };

  const timer = setTimeout(() => {
    finish({ status: 'error', message: 'Native host did not respond in time' }, true);
  }, ACTION_TIMEOUTS_MS[action]);

  try {
    port = browser.runtime.connectNative(NATIVE_HOST_NAME);
  } catch (e) {
    finish({ status: 'error', message: 'Could not connect to native host' }, false);
    return;
  }

  port.onMessage.addListener((response) => {
    if (done) return;
    const isFinal = Object.prototype.hasOwnProperty.call(response, 'final')
      ? Boolean(response.final)
      : response.status === 'success' || response.status === 'error';
    if (isFinal) {
      finish(response, true);
    } else {
      relay(Object.assign({}, response, { requestId, final: false }));
    }
  });

  // Firefox reports why the host went away (e.g. not installed) on port.error, not runtime.lastError.
  port.onDisconnect.addListener((p) => {
    finish({ status: 'error', message: p.error ? p.error.message : 'Native host exited unexpectedly' }, false);
  });

  try {
    port.postMessage(payload);
  } catch (e) {
    finish({ status: 'error', message: 'Failed to send to native host' }, true);
  }
}

browser.contextMenus.onClicked.addListener(async (info) => {
  if (info.menuItemId !== MENU_ID) return;
  if (!isYouTubeUrl(info.linkUrl)) {
    notify('Invalid URL', 'Not a valid YouTube URL');
    return;
  }

  // Use the default folder chosen in the sidebar; without one the host falls back to its own.
  const payload = { action: 'download', url: info.linkUrl };
  const { lastFolder } = await browser.storage.local.get('lastFolder').catch(() => ({}));
  if (lastFolder) payload.folder = lastFolder;

  runNativeAction(payload, crypto.randomUUID(), 'menu');
});

browser.runtime.onMessage.addListener((msg) => {
  if (!msg || msg.type !== 'native_action') return;

  if (!Object.hasOwn(ACTION_TIMEOUTS_MS, msg.action)) {
    relay({ requestId: msg.requestId || null, status: 'error', message: 'Unsupported action', final: true });
    return;
  }

  const payload = { action: msg.action };
  if (msg.url) payload.url = msg.url;
  if (msg.folder) payload.folder = msg.folder;
  if (msg.filename) payload.filename = msg.filename;

  runNativeAction(payload, msg.requestId || crypto.randomUUID(), 'sidebar');
});
