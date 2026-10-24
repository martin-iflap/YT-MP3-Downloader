/* global isKnownSiteUrl */

const NATIVE_HOST_NAME = 'app_manifest';
const MENU_ID = 'download-mp3';
const NOTIFICATION_ICON = 'icons/icon-48.png';
// The installer of the latest release, downloaded directly: the one link that fixes every
// host problem, including hosts that can't update themselves (1.x, broken installs).
// Every release must carry this asset name (updater.INSTALLER_ASSET, checked by test_versions.py).
const INSTALLER_URL = 'https://github.com/martin-iflap/YT-MP3-Downloader/releases/latest/download/YT-MP3-Downloader-Setup.exe';
const HOST_MISSING_MESSAGE = 'The YT MP3 Downloader app is not installed. Install it from GitHub, then restart Firefox.';

// The oldest host this extension works with. Raise it when the extension starts to rely on
// a new host action or response field; a newer host is always fine.
const MIN_HOST_VERSION = '2.0.0';
const HOST_OUTDATED_MESSAGE = `The YT MP3 Downloader app is out of date (version ${MIN_HOST_VERSION} or newer is needed). Install the update from GitHub.`;
const HOST_BROKEN_MESSAGE = 'The YT MP3 Downloader app is not working (it closed without answering). Reinstall it from GitHub.';
// What a 1.x host answers to get_version: it treats unknown actions as a download without a URL.
const LEGACY_HOST_REPLY = 'Invalid YouTube URL';
const LEGACY_HOST_MESSAGE = "This version of the YT MP3 Downloader app can't update itself. Install the new version once, from then on it keeps itself up to date.";

// Per-action timeouts. The host allows 300s for yt-dlp plus 50s for the ffmpeg cover pass,
// and select_folder waits on the user, so these must stay above that. get_version waits
// up to 20s for `yt-dlp --version`. The updates (updater.py) may first wait 60s for another
// host's update, look up the tag (5s) and fetch the checksums (30s), then download for up
// to 180s (yt-dlp) or 360s (the installer), plus up to 30s for the last read, and test-run a
// new exe (20s): about 345s for yt-dlp (with its 20s version check first) and 485s for the
// installer. A download that fails in yt-dlp is retried once after a repair
// (core.download_with_repair): 350s, a yt-dlp update (345s), Deno (about 455s), 350s
// again, so the worst case is about 25 min.
const ACTION_TIMEOUTS_MS = {
  download: 27 * 60 * 1000,
  download_gui: 30 * 1000,
  select_folder: 10 * 60 * 1000,
  get_current_folder: 15 * 1000,
  show_in_folder: 15 * 1000,
  play: 15 * 1000,
  get_version: 30 * 1000,
  update_yt_dlp: 7 * 60 * 1000,
  update_host: 9 * 60 * 1000
};

// What the sidebar shows while a repair runs.
const REPAIR_MESSAGES = {
  update_host: 'Updating the YT MP3 Downloader app...',
  update_yt_dlp: 'Updating yt-dlp...'
};
// After update_host the installer runs on its own; poll until the new version answers.
const HOST_UPDATE_POLL_MS = 5 * 1000;
const HOST_UPDATE_WAIT_MS = 3 * 60 * 1000;

// download_gui keeps the host process alive to run the Tk window after its final
// response; disconnecting the port would make Firefox kill it. The host exits by
// itself once the window is closed.
const KEEP_ALIVE_ACTIONS = new Set(['download_gui']);

// Menus are not persisted across browser restarts for MV2 persistent backgrounds,
// so create it on every startup rather than in onInstalled. The patterns cover the links
// isKnownSiteUrl accepts (youtube.js); a click checks the link exactly.
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
    '*://m.youtube.com/live/*',
    '*://soundcloud.com/*/*',
    '*://www.soundcloud.com/*/*',
    '*://m.soundcloud.com/*/*',
    '*://on.soundcloud.com/*',
    '*://*.bandcamp.com/track/*',
    '*://mixcloud.com/*/*',
    '*://www.mixcloud.com/*/*',
    '*://m.mixcloud.com/*/*'
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
  } catch {
    return false;
  }
}

// notification id -> the MP3 a click on it shows in its folder
const notificationFiles = new Map();

function notify(title, message, path) {
  browser.notifications.create({ type: 'basic', iconUrl: NOTIFICATION_ICON, title, message })
    .then((id) => { if (path) notificationFiles.set(id, path); })
    .catch(e => console.warn('Notification create failed:', e));
}

browser.notifications.onClicked.addListener((id) => {
  const path = notificationFiles.get(id);
  if (path) hostRequest({ action: 'show_in_folder', path });
});
browser.notifications.onClosed.addListener(id => notificationFiles.delete(id));

// The sidebar shows results for its own requests; notify only when nobody would see them.
async function notifyDownloadResult(origin, response) {
  if (origin === 'sidebar' && await isSidebarOpen()) return;
  if (response.status === 'success') {
    const saved = `${response.filename || 'The MP3'} saved successfully!`;
    notify('Download Complete', response.path ? `${saved} Click to show it in its folder.` : saved, response.path);
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
    // shown only when the automatic update did not work
    return {
      status: 'warning',
      message: `yt-dlp is out of date (${ytDlp.version}, latest ${ytDlp.latest}), so downloads may fail.`
    };
  }
  return null;
}

// Turn the host's get_version answer into one status: the answer itself when all is well,
// otherwise its most important problem as a warning or error with the installer link.
// source is where the answer came from (see hostRequest).
function assessHostStatus(response, source) {
  const version = parseVersion(response.version);
  if (!version) {
    // closed without answering: the host can't start, so its install is broken
    // ("not installed" already carries its own message and link)
    if (source === 'disconnect' && !response.helpUrl) {
      return { status: 'error', message: HOST_BROKEN_MESSAGE, helpUrl: INSTALLER_URL };
    }
    // a v2+ host that failed (or did not answer in time) is reported as it is, with the
    // installer: it could not repair itself. Only a 1.x host answers LEGACY_HOST_REPLY.
    if (response.message !== LEGACY_HOST_REPLY) {
      return Object.assign({}, response, { helpUrl: response.helpUrl || INSTALLER_URL });
    }
    return { status: 'warning', message: LEGACY_HOST_MESSAGE, helpUrl: INSTALLER_URL };
  }
  if (compareVersions(version, parseVersion(MIN_HOST_VERSION)) < 0) {
    return Object.assign({}, response, { status: 'warning', message: HOST_OUTDATED_MESSAGE, helpUrl: INSTALLER_URL });
  }
  const problem = toolProblem(response);
  return problem ? Object.assign({}, response, problem, { helpUrl: INSTALLER_URL }) : response;
}

// The repairs a get_version answer calls for, most important first: a newer host release
// (it may bring its own fixes), then a missing, broken or outdated yt-dlp. 1.x hosts, which
// have no updater, and failed checks get none; a host that can't update itself (a source
// checkout) gets no host update.
function neededRepairs({ response, source }) {
  if (source !== 'host' || !parseVersion(response.version)) return [];
  const repairs = [];
  if (response.self_update && response.outdated) repairs.push('update_host');
  const ytDlp = response.yt_dlp || {};
  if (!ytDlp.installed || !ytDlp.version || ytDlp.outdated) repairs.push('update_yt_dlp');
  return repairs;
}

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

// After update_host started the installer of release tag: check until that version
// answers, or give up after HOST_UPDATE_WAIT_MS. Returns the last check.
async function waitForHostVersion(tag) {
  const target = parseVersion(tag);
  let check = null;
  for (let waited = 0; waited < HOST_UPDATE_WAIT_MS; waited += HOST_UPDATE_POLL_MS) {
    await sleep(HOST_UPDATE_POLL_MS);
    check = await hostRequest({ action: 'get_version' });
    const version = parseVersion(check.response.version);
    if (version && compareVersions(version, target) >= 0) break;
  }
  return check;
}

// Add what the failed repairs reported to the status the user is shown.
function withRepairFailures(status, failures) {
  if (!failures.length) return status;
  const reasons = failures.join('; ');
  if (status.status === 'success') {
    // only the host update failed; the current host works
    return Object.assign({}, status, {
      status: 'warning',
      message: `The app could not update itself (${reasons}). Install the update from GitHub.`,
      helpUrl: INSTALLER_URL
    });
  }
  return Object.assign({}, status, { message: `${status.message} Automatic repair failed: ${reasons}.` });
}

// Ask the host for its state, repair what can be repaired (each repair at most once),
// and turn the state after the last repair into the one status the user is told.
async function checkAndRepairHost(onProgress) {
  let check = await hostRequest({ action: 'get_version' });
  const attempted = new Set();
  const failures = [];
  for (;;) {
    const repair = neededRepairs(check).find(r => !attempted.has(r));
    if (!repair) break;
    attempted.add(repair);
    onProgress({ status: 'info', message: REPAIR_MESSAGES[repair] });

    const { response: result } = await hostRequest({ action: repair });
    if (result.status !== 'success') {
      failures.push(result.message || `${repair} failed`);
      continue;
    }
    if (repair === 'update_host' && result.version) {
      check = await waitForHostVersion(result.version);
      if (compareVersions(parseVersion(check.response.version) || [0], parseVersion(result.version)) < 0) {
        failures.push(`version ${result.version} did not finish installing`);
      }
    } else {
      check = await hostRequest({ action: 'get_version' });
    }
  }
  return withRepairFailures(assessHostStatus(check.response, check.source), failures);
}

// One check (with its repairs) at a time: a sidebar that asks meanwhile, e.g. in a second
// window, shares the running one and gets only its final answer.
let runningCheck = null;

function checkHost(onProgress) {
  if (!runningCheck) runningCheck = checkAndRepairHost(onProgress).finally(() => { runningCheck = null; });
  return runningCheck;
}

// Run one action on a fresh host process. Intermediate responses go to onProgress.
// Resolves exactly once with { response, source }: the host's final response (source
// 'host'), or one made up here when the host does not answer in time ('timeout'), goes
// away without a final response ('disconnect') or can't be reached ('connect').
function hostRequest(payload, onProgress = () => {}) {
  return new Promise((resolve) => {
    const action = payload.action;
    let port = null;
    let done = false;

    const finish = (response, source, disconnect) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      if (disconnect && port && !KEEP_ALIVE_ACTIONS.has(action)) port.disconnect();
      resolve({ response, source });
    };

    const timer = setTimeout(() => {
      finish({ status: 'error', message: 'Native host did not respond in time' }, 'timeout', true);
    }, ACTION_TIMEOUTS_MS[action]);

    try {
      port = browser.runtime.connectNative(NATIVE_HOST_NAME);
    } catch {
      finish({ status: 'error', message: 'Could not connect to native host' }, 'connect', false);
      return;
    }

    port.onMessage.addListener((response) => {
      if (done) return;
      const isFinal = Object.prototype.hasOwnProperty.call(response, 'final')
        ? Boolean(response.final)
        : response.status === 'success' || response.status === 'error';
      if (isFinal) finish(response, 'host', true);
      else onProgress(response);
    });

    // Firefox reports why the host went away (e.g. not installed) on port.error, not runtime.lastError.
    port.onDisconnect.addListener((p) => {
      finish(disconnectResponse(p.error), 'disconnect', false);
    });

    try {
      port.postMessage(payload);
    } catch {
      finish({ status: 'error', message: 'Failed to send to native host' }, 'connect', true);
    }
  });
}

// Answer one request from an extension page or the context menu: intermediate responses,
// then exactly one final response for requestId. get_version runs the check-and-repair.
async function runNativeAction(payload, requestId, origin) {
  const progress = response => relay(Object.assign({}, response, { requestId, final: false }));
  const response = payload.action === 'get_version'
    ? await checkHost(progress)
    : (await hostRequest(payload, progress)).response;
  relay(Object.assign({}, response, { requestId, final: true }));
  if (payload.action === 'download') notifyDownloadResult(origin, response);
}

browser.contextMenus.onClicked.addListener(async (info) => {
  if (info.menuItemId !== MENU_ID) return;
  if (!isKnownSiteUrl(info.linkUrl)) {
    notify('Invalid URL', 'Not a link to a video or track');
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
  if (msg.path) payload.path = msg.path;

  runNativeAction(payload, msg.requestId || crypto.randomUUID(), 'sidebar');
});
