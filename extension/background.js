/* global browser, isYouTubeUrl */

const NATIVE_HOST_NAME = 'app_manifest';
const MENU_ID = 'download-mp3';
const NOTIFICATION_ICON = 'icons/icon-48.png';
const INSTALLER_URL = 'https://github.com/martin-iflap/YT-MP3-Downloader/releases/latest';
const HOST_MISSING_MESSAGE = 'The YT MP3 Downloader app is not installed. Install it from GitHub, then restart Firefox.';

// The oldest host this extension works with. Raise it when the extension starts to rely on
// a new host action or response field; a newer host is always fine.
const MIN_HOST_VERSION = '2.0.0';
const HOST_OUTDATED_MESSAGE = `The YT MP3 Downloader app is out of date (version ${MIN_HOST_VERSION} or newer is needed). Install the update from GitHub.`;
// What a 1.x host answers to get_version: it treats unknown actions as a download without a URL.
const LEGACY_HOST_REPLY = 'Invalid YouTube URL';

// Per-action timeouts. The host allows 300s for yt-dlp plus 50s for the ffmpeg cover pass,
// and select_folder waits on the user, so these must stay above that. get_version waits
// up to 20s for `yt-dlp --version`.
const ACTION_TIMEOUTS_MS = {
  download: 6 * 60 * 1000,
  download_gui: 30 * 1000,
  select_folder: 10 * 60 * 1000,
  get_current_folder: 15 * 1000,
  get_version: 30 * 1000
};

// download_gui keeps the host process alive to run the Tk window after its final
// response; disconnecting the port would make Firefox kill it. The host exits by
// itself once the window is closed.
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
    '*://music.youtube.com/watch*',
    '*://youtu.be/*',
    '*://www.youtube.com/shorts/*',
    '*://youtube.com/shorts/*',
    '*://m.youtube.com/shorts/*',
    '*://www.youtube.com/live/*',
    '*://youtube.com/live/*',
    '*://m.youtube.com/live/*'
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

// Turn a port error into a response. Firefox says "No such native application <name>"
// when the host is not registered, the most common first-run failure.
function disconnectResponse(error) {
  if (!error) return { status: 'error', message: 'Native host exited unexpectedly' };
  if (/no such native application/i.test(error.message)) {
    return { status: 'error', message: HOST_MISSING_MESSAGE, helpUrl: INSTALLER_URL };
  }
  return { status: 'error', message: error.message };
}

// "2.0.0", "v2.0.0" or yt-dlp's "2026.08.19" -> [2, 0, 0]; null if it is not a version.
function parseVersion(text) {
  const match = typeof text === 'string' ? /^v?(\d+(?:\.\d+)*)$/.exec(text.trim()) : null;
  return match ? match[1].split('.').map(Number) : null;
}

// Compare two parsed versions numerically (missing parts count as 0): -1, 0 or 1.
function compareVersions(a, b) {
  for (let i = 0; i < Math.max(a.length, b.length); i++) {
    const diff = (a[i] || 0) - (b[i] || 0);
    if (diff) return Math.sign(diff);
  }
  return 0;
}

// The most important problem with the host's tools, or null. The host reports the facts
// (see health.host_status); this decides what the user is told.
function toolProblem(response) {
  const ytDlp = response.yt_dlp || {};
  const reinstall = 'Reinstall the app from GitHub to fix it.';
  if (!(response.ffmpeg || {}).installed) return { status: 'error', message: `ffmpeg.exe is missing. ${reinstall}` };
  if (!ytDlp.installed) return { status: 'error', message: `yt-dlp.exe is missing. ${reinstall}` };
  if (!ytDlp.version) return { status: 'error', message: `yt-dlp.exe is not working. ${reinstall}` };
  if (ytDlp.outdated) {
    return {
      status: 'warning',
      message: `yt-dlp is out of date (${ytDlp.version}, latest ${ytDlp.latest}), so downloads may fail. Reinstall the app from GitHub to update it.`
    };
  }
  return null;
}

// Turn the host's get_version answer into one status: the answer itself when all is well,
// otherwise its most important problem as a warning or error with the installer link.
function assessHostStatus(response) {
  const version = parseVersion(response.version);
  if (!version) {
    // a v2+ host that failed is reported as it is; only a 1.x host answers like this
    if (response.message !== LEGACY_HOST_REPLY) return response;
    return { status: 'warning', message: HOST_OUTDATED_MESSAGE, helpUrl: INSTALLER_URL };
  }
  if (compareVersions(version, parseVersion(MIN_HOST_VERSION)) < 0) {
    return Object.assign({}, response, { status: 'warning', message: HOST_OUTDATED_MESSAGE, helpUrl: INSTALLER_URL });
  }
  const problem = toolProblem(response);
  return problem ? Object.assign({}, response, problem, { helpUrl: INSTALLER_URL }) : response;
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
      finish(action === 'get_version' ? assessHostStatus(response) : response, true);
    } else {
      relay(Object.assign({}, response, { requestId, final: false }));
    }
  });

  // Firefox reports why the host went away (e.g. not installed) on port.error, not runtime.lastError.
  port.onDisconnect.addListener((p) => {
    finish(disconnectResponse(p.error), false);
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
