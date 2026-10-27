const { test, beforeEach, afterEach, mock } = require('node:test');
const assert = require('node:assert/strict');
const { loadBackground, flush } = require('./harness');

const VIDEO_URL = 'https://www.youtube.com/watch?v=dQw4w9WgXcQ';

beforeEach(() => mock.timers.enable({ apis: ['setTimeout'] }));
afterEach(() => mock.timers.reset());

function sidebarAction(browser, action, extra = {}) {
  browser.runtime.onMessage.fire({ type: 'native_action', requestId: 'req-1', action, ...extra });
}

const finals = state => state.sent.filter(m => m.final === true);

test('creates the context menu at startup', () => {
  const { state } = loadBackground();
  assert.equal(state.menus.length, 1);
  assert.equal(state.menus[0].id, 'download-mp3');
});

test('the toolbar button toggles the sidebar', () => {
  const { browser, state } = loadBackground();
  browser.browserAction.onClicked.fire({});
  browser.browserAction.onClicked.fire({});
  assert.equal(state.sidebarToggles, 2);
  assert.equal(state.sidebarOpen, false);
});

// What background.js answers the sidebar's toolbar_tip question with.
const toolbarTip = browser => Promise.all(browser.runtime.onMessage.fire({ type: 'toolbar_tip' }))
  .then(answers => answers.find(a => a !== undefined));

function withClock(fn) {
  return async () => {
    mock.timers.reset();
    mock.timers.enable({ apis: ['setTimeout', 'Date'], now: 0 });
    await fn();
  };
}

test('toolbar tip: shown when the sidebar opens some other way while the button is pinned', withClock(async () => {
  const { browser } = loadBackground();
  mock.timers.tick(60 * 1000);
  assert.equal(await toolbarTip(browser), true);
}));

test('toolbar tip: not when the toolbar button opened the sidebar', withClock(async () => {
  const { browser } = loadBackground();
  mock.timers.tick(60 * 1000);
  browser.browserAction.onClicked.fire({});
  mock.timers.tick(1000);
  assert.equal(await toolbarTip(browser), false);
  mock.timers.tick(10 * 1000);
  assert.equal(await toolbarTip(browser), true, 'a later open is not the click\'s');
}));

test('toolbar tip: not for a sidebar Firefox restores at startup', withClock(async () => {
  const { browser } = loadBackground();
  mock.timers.tick(2000);
  assert.equal(await toolbarTip(browser), false);
}));

test('toolbar tip: not when unpinned or dismissed', withClock(async () => {
  const unpinned = loadBackground({ pinned: false });
  const dismissed = loadBackground({ storage: { toolbarTipDismissed: true } });
  mock.timers.tick(60 * 1000);
  assert.equal(await toolbarTip(unpinned.browser), false);
  assert.equal(await toolbarTip(dismissed.browser), false);
}));

test('sidebar download: forwards payload, relays progress, then one final and disconnects', async () => {
  const { browser, state } = loadBackground({ sidebarOpen: true });
  sidebarAction(browser, 'download', { url: VIDEO_URL, folder: 'C:\\Music', filename: 'song' });

  const [port] = state.ports;
  assert.equal(port.name, 'app_manifest');
  assert.deepStrictEqual(port.posted, [{ action: 'download', url: VIDEO_URL, folder: 'C:\\Music', filename: 'song' }]);

  port.reply({ status: 'warning', message: 'No thumbnail found, the MP3 is saved without cover art', final: false });
  port.reply({ status: 'success', filename: 'song', final: true });
  await flush();

  assert.deepStrictEqual(state.sent, [
    { type: 'native_response', status: 'warning', message: 'No thumbnail found, the MP3 is saved without cover art', requestId: 'req-1', final: false },
    { type: 'native_response', status: 'success', filename: 'song', requestId: 'req-1', final: true }
  ]);
  assert.equal(port.disconnected, true);
  assert.equal(state.notifications.length, 0, 'sidebar is open, so it shows the result itself');
});

test('download_gui keeps the host alive after its final response', async () => {
  const { browser, state } = loadBackground();
  sidebarAction(browser, 'download_gui', { url: VIDEO_URL });
  state.ports[0].reply({ status: 'success', message: 'GUI opened', final: true });
  await flush();

  assert.equal(finals(state).length, 1);
  assert.equal(state.ports[0].disconnected, false);
});

test('unsupported actions are rejected without starting the host', () => {
  const { browser, state } = loadBackground();
  sidebarAction(browser, 'rm_rf');
  sidebarAction(browser, 'toString');

  assert.equal(state.ports.length, 0);
  assert.equal(finals(state).length, 2);
  assert.ok(finals(state).every(m => m.status === 'error' && m.message === 'Unsupported action'));
});

test('host not installed: reports it with the installer link immediately, exactly once', async () => {
  const { browser, state } = loadBackground();
  sidebarAction(browser, 'get_current_folder');
  state.ports[0].exit('No such native application app_manifest');
  mock.timers.tick(60 * 60 * 1000);
  await flush();

  const [final] = finals(state);
  assert.equal(finals(state).length, 1);
  assert.equal(final.status, 'error');
  assert.match(final.message, /not installed/);
  assert.equal(final.helpUrl, 'https://github.com/martin-iflap/YT-MP3-Downloader/releases/latest/download/YT-MP3-Downloader-Setup.exe');
});

test('other port errors are relayed as they are', async () => {
  const { browser, state } = loadBackground();
  sidebarAction(browser, 'get_current_folder');
  state.ports[0].exit('Error reading from native app');
  await flush();

  assert.deepStrictEqual(finals(state), [
    { type: 'native_response', status: 'error', message: 'Error reading from native app', requestId: 'req-1', final: true }
  ]);
});

test('host exiting without a final response is reported as an error', async () => {
  const { browser, state } = loadBackground();
  sidebarAction(browser, 'download', { url: VIDEO_URL });
  state.ports[0].exit(null);
  await flush();

  assert.equal(finals(state).length, 1);
  assert.equal(finals(state)[0].status, 'error');
});

test('long downloads are not cut off at 40s; a real timeout ends the request once', async () => {
  const { browser, state } = loadBackground();
  sidebarAction(browser, 'download', { url: VIDEO_URL });
  const [port] = state.ports;

  mock.timers.tick(26 * 60 * 1000);
  await flush();
  assert.equal(finals(state).length, 0, 'still within the host\'s budget for a download, a repair and a retry');

  mock.timers.tick(2 * 60 * 1000);
  port.reply({ status: 'success', filename: 'late', final: true });
  await flush();

  assert.equal(finals(state).length, 1);
  assert.equal(finals(state)[0].status, 'error');
  assert.equal(port.disconnected, true);
});

test('sidebar download notifies when the sidebar was closed by the time it finished', async () => {
  const { browser, state } = loadBackground({ sidebarOpen: true });
  sidebarAction(browser, 'download', { url: VIDEO_URL });
  state.sidebarOpen = false;
  state.ports[0].reply({ status: 'success', filename: 'song', final: true });
  await flush();

  assert.equal(state.notifications.length, 1);
  assert.equal(state.notifications[0].title, 'Download Complete');
});

test('folder lookups never notify, even when they fail', async () => {
  const { browser, state } = loadBackground({ sidebarOpen: false });
  sidebarAction(browser, 'get_current_folder');
  state.ports[0].reply({ status: 'error', message: 'The download folder "C:\\Gone" doesn\'t exist. Check the path in the sidebar, or pick a folder with Choose.', final: true });
  await flush();

  assert.equal(state.notifications.length, 0);
});

test('context menu download uses the saved folder and always notifies', async () => {
  const { browser, state } = loadBackground({ sidebarOpen: true, storage: { lastFolder: 'D:\\Music' } });
  browser.contextMenus.onClicked.fire({ menuItemId: 'download-mp3', linkUrl: VIDEO_URL });
  await flush();

  const [port] = state.ports;
  assert.deepStrictEqual(port.posted, [{ action: 'download', url: VIDEO_URL, folder: 'D:\\Music' }]);

  port.reply({ status: 'error', message: 'File with this name already exists', final: true });
  await flush();
  assert.deepStrictEqual(state.notifications.map(n => [n.title, n.message]), [
    ['Download Failed', 'File with this name already exists']
  ]);
});

test('show_in_folder and play forward the path to the host', async () => {
  const { browser, state } = loadBackground();
  sidebarAction(browser, 'show_in_folder', { path: 'C:\\Music\\Song.mp3' });
  sidebarAction(browser, 'play', { path: 'C:\\Music\\Song.mp3' });

  assert.deepStrictEqual(state.ports.map(p => p.posted), [
    [{ action: 'show_in_folder', path: 'C:\\Music\\Song.mp3' }],
    [{ action: 'play', path: 'C:\\Music\\Song.mp3' }]
  ]);
  state.ports.forEach(port => port.reply({ status: 'success', final: true }));
  await flush();
  assert.equal(finals(state).length, 2);
  assert.ok(state.ports.every(port => port.disconnected));
  assert.equal(state.notifications.length, 0);
});

test('clicking a download notification shows the MP3 in its folder', async () => {
  const { browser, state } = loadBackground();
  browser.contextMenus.onClicked.fire({ menuItemId: 'download-mp3', linkUrl: VIDEO_URL });
  await flush();
  state.ports[0].reply({ status: 'success', filename: 'Song', path: 'C:\\Music\\Song.mp3', final: true });
  await flush();

  const [notification] = state.notifications;
  assert.match(notification.message, /Click to show it in its folder/);
  const shows = () => state.ports.filter(p => p.posted.some(m => m.action === 'show_in_folder'));
  browser.notifications.onClicked.fire('notification-1');
  assert.deepStrictEqual(shows().map(p => p.posted), [[{ action: 'show_in_folder', path: 'C:\\Music\\Song.mp3' }]]);

  browser.notifications.onClosed.fire('notification-1');
  browser.notifications.onClicked.fire('notification-1');
  assert.equal(shows().length, 1, 'a closed notification no longer opens anything');
});

test('context menu download without a saved folder lets the host pick its default', async () => {
  const { browser, state } = loadBackground();
  browser.contextMenus.onClicked.fire({ menuItemId: 'download-mp3', linkUrl: 'https://youtu.be/dQw4w9WgXcQ' });
  await flush();

  assert.deepStrictEqual(state.ports[0].posted, [{ action: 'download', url: 'https://youtu.be/dQw4w9WgXcQ' }]);
});

test('context menu downloads a SoundCloud track', async () => {
  const { browser, state } = loadBackground();
  browser.contextMenus.onClicked.fire({ menuItemId: 'download-mp3', linkUrl: 'https://soundcloud.com/forss/flickermood' });
  await flush();

  assert.deepStrictEqual(state.ports[0].posted, [{ action: 'download', url: 'https://soundcloud.com/forss/flickermood' }]);
});

// --- the background check (at most once a day) ---

const NOW = Date.parse('2026-10-26T12:00:00Z');
const DAY_MS = 24 * 60 * 60 * 1000;
const versionChecks = state => state.ports.filter(p => p.posted.some(m => m.action === 'get_version'));

function atNow(fn) {
  return async () => {
    mock.timers.reset();
    mock.timers.enable({ apis: ['setTimeout', 'Date'], now: NOW });
    await fn();
  };
}

test('background check: a minute after startup, and the time is saved', atNow(async () => {
  const { state } = loadBackground();
  mock.timers.tick(59 * 1000);
  await flush();
  assert.equal(versionChecks(state).length, 0, 'not during the browser startup');

  mock.timers.tick(1000);
  await flush();
  assert.equal(versionChecks(state).length, 1);
  assert.equal(state.storage.lastHostCheck, NOW + 60 * 1000);
  assert.equal(state.sent.length, 0, 'silent: nothing is relayed to the sidebar');
}));

test('background check: not when the last check is less than a day old', atNow(async () => {
  const recent = loadBackground({ storage: { lastHostCheck: NOW - DAY_MS + 2 * 60 * 1000 } });
  const old = loadBackground({ storage: { lastHostCheck: NOW - DAY_MS } });
  mock.timers.tick(60 * 1000);
  await flush();
  assert.equal(versionChecks(recent.state).length, 0);
  assert.equal(versionChecks(old.state).length, 1);
}));

test('background check: after a context-menu download, once a day', atNow(async () => {
  const { browser, state } = loadBackground({ storage: { lastHostCheck: NOW - DAY_MS } });
  browser.contextMenus.onClicked.fire({ menuItemId: 'download-mp3', linkUrl: VIDEO_URL });
  await flush();
  assert.equal(versionChecks(state).length, 0, 'the download goes first');

  state.ports[0].reply({ status: 'success', filename: 'Song', final: true });
  await flush();
  await flush();
  assert.equal(versionChecks(state).length, 1);

  browser.contextMenus.onClicked.fire({ menuItemId: 'download-mp3', linkUrl: VIDEO_URL });
  await flush();
  state.ports[2].reply({ status: 'success', filename: 'Song 2', final: true });
  mock.timers.tick(60 * 1000);
  await flush();
  await flush();
  assert.equal(versionChecks(state).length, 1, 'the same day: no second check, also not at startup');
}));

test('background check: a sidebar check counts', atNow(async () => {
  const { browser, state } = loadBackground();
  sidebarAction(browser, 'get_version');
  await flush();
  mock.timers.tick(60 * 1000);
  await flush();
  assert.equal(versionChecks(state).length, 1, 'only the sidebar\'s');
}));

test('context menu on a non-video link notifies and does not start the host', async () => {
  const { browser, state } = loadBackground();
  browser.contextMenus.onClicked.fire({ menuItemId: 'download-mp3', linkUrl: 'https://www.youtube.com/@channel' });
  await flush();

  assert.equal(state.ports.length, 0);
  assert.equal(state.notifications[0].title, 'Invalid URL');
});
