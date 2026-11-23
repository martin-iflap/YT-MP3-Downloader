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

test('creates the context menu entries at startup', () => {
  const { state } = loadBackground();
  assert.deepStrictEqual(state.menus.map(m => [m.id, m.title]), [
    ['download-mp3', 'Download as MP3'],
    ['download-mp4', 'Download as MP4']
  ]);
});

// Whether Firefox shows a menu entry on a link: a match pattern as a regular expression,
// "*" is any text, a "*" scheme is http or https.
function menuShownOn(menu, url) {
  return menu.targetUrlPatterns.some(pattern => new RegExp(
    `^${pattern.replace(/[.?]/g, '\\$&').replace(/^\*/, 'https?').replace(/\*/g, '.*')}$`
  ).test(url));
}

const YOUTUBE_LINKS = [
  'https://www.youtube.com/watch?v=abc',
  'https://m.youtube.com/watch?v=abc',
  'https://music.youtube.com/watch?v=abc',
  'https://music.youtube.com/shorts/abc',
  'https://music.youtube.com/live/abc',
  'https://www.youtube.com/shorts/abc',
  'https://www.youtube.com/live/abc',
  'https://youtu.be/abc'
];
const AUDIO_SITE_LINKS = [
  'https://soundcloud.com/forss/flickermood',
  'https://m.soundcloud.com/forss/flickermood',
  'https://on.soundcloud.com/AbC123',
  'https://artist.bandcamp.com/track/song',
  'https://www.mixcloud.com/spartacus/party-time/'
];

test('MP3 appears on every kind of link a click accepts', () => {
  const { state, context } = loadBackground();
  for (const url of [...YOUTUBE_LINKS, ...AUDIO_SITE_LINKS]) {
    assert.equal(context.isKnownSiteUrl(url), true, url);
    assert.ok(menuShownOn(state.menus[0], url), `no pattern matches ${url}`);
  }
});

test('MP4 appears on YouTube links only: the audio sites have no video', () => {
  const { state, context } = loadBackground();
  for (const url of YOUTUBE_LINKS) assert.ok(menuShownOn(state.menus[1], url), url);
  for (const url of AUDIO_SITE_LINKS) {
    assert.equal(context.isAudioSiteUrl(url), true, url);
    assert.equal(menuShownOn(state.menus[1], url), false, url);
  }
});

test('context menu MP4 sends the sidebar\'s MP4 choices and folder, without a check', async () => {
  const { browser, state } = loadBackground({
    storage: { lastFolder: 'D:\\Videos', hostFormats: ['mp3', 'mp4'], mp4Options: { quality: '720', codec: 'compatible' } }
  });
  browser.contextMenus.onClicked.fire({ menuItemId: 'download-mp4', linkUrl: VIDEO_URL });
  await flush();

  assert.deepStrictEqual(state.ports.map(p => p.posted), [
    [{ action: 'download', url: VIDEO_URL, format: 'mp4', quality: '720', codec: 'compatible', folder: 'D:\\Videos' }]
  ]);
});

test('context menu MP4 without saved choices uses the defaults', async () => {
  const { browser, state } = loadBackground({ storage: { hostFormats: ['mp3', 'mp4'], mp4Options: { quality: '4320' } } });
  browser.contextMenus.onClicked.fire({ menuItemId: 'download-mp4', linkUrl: VIDEO_URL });
  await flush();

  assert.deepStrictEqual(state.ports[0].posted, [
    { action: 'download', url: VIDEO_URL, format: 'mp4', quality: 'best', codec: 'best' }
  ], 'an unknown stored quality falls back too');
});

// A host that answers get_version with formats and records the downloads it gets.
function hostWithFormats(formats) {
  const downloads = [];
  const nativeHost = (message, port) => {
    if (message.action === 'get_version') {
      port.reply({
        status: 'success', version: '2.2.0', latest: '2.2.0', outdated: false, self_update: true, formats,
        yt_dlp: { installed: true, version: '2026.08.19', latest: '2026.08.19', outdated: false },
        ffmpeg: { installed: true }, final: true
      });
    } else downloads.push(message);
  };
  return { nativeHost, downloads };
}

test('context menu MP4 checks a host it knows nothing about first, then downloads', async () => {
  const { nativeHost, downloads } = hostWithFormats(['mp3', 'mp4']);
  const { browser, state } = loadBackground({ nativeHost });
  browser.contextMenus.onClicked.fire({ menuItemId: 'download-mp4', linkUrl: VIDEO_URL });
  for (let i = 0; i < 10; i++) await flush();

  assert.deepStrictEqual(state.ports.map(p => p.posted[0].action), ['get_version', 'download']);
  assert.equal(downloads[0].format, 'mp4');
  assert.deepStrictEqual(state.storage.hostFormats, ['mp3', 'mp4']);
});

test('context menu MP4 on a host without MP4 notifies and downloads nothing', async () => {
  const { nativeHost, downloads } = hostWithFormats(undefined);
  const { browser, state } = loadBackground({ nativeHost, storage: { hostFormats: [] } });
  browser.contextMenus.onClicked.fire({ menuItemId: 'download-mp4', linkUrl: VIDEO_URL });
  for (let i = 0; i < 10; i++) await flush();

  assert.deepStrictEqual(downloads, [], 'an older host would ignore the format and save an MP3');
  assert.deepStrictEqual(state.notifications.map(n => n.title), ['Download Failed']);
  assert.match(state.notifications[0].message, /MP4 needs a newer version/);
});

test('context menu MP3 never waits for a check', async () => {
  const { browser, state } = loadBackground({ storage: { mp4Options: { quality: '720' } } });
  browser.contextMenus.onClicked.fire({ menuItemId: 'download-mp3', linkUrl: VIDEO_URL });
  await flush();

  assert.deepStrictEqual(state.ports.map(p => p.posted), [[{ action: 'download', url: VIDEO_URL }]]);
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

test('sidebar download in another format forwards its choices, and nothing else', () => {
  const { browser, state } = loadBackground({ sidebarOpen: true });
  sidebarAction(browser, 'download', { url: VIDEO_URL, format: 'mp4', quality: '720', codec: 'compatible', extra: 'x' });
  assert.deepStrictEqual(state.ports[0].posted, [
    { action: 'download', url: VIDEO_URL, format: 'mp4', quality: '720', codec: 'compatible' }
  ]);
});

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

test('long downloads are not cut off early; a real timeout ends the request once', async () => {
  const { browser, state } = loadBackground();
  sidebarAction(browser, 'download', { url: VIDEO_URL });
  const [port] = state.ports;

  mock.timers.tick(69 * 60 * 1000);
  await flush();
  assert.equal(finals(state).length, 0, 'still within the host\'s budget for a download, a repair and a retry');

  mock.timers.tick(2 * 60 * 1000);
  port.reply({ status: 'success', filename: 'late', final: true });
  await flush();

  assert.equal(finals(state).length, 1);
  assert.equal(finals(state)[0].status, 'error');
  assert.equal(port.disconnected, true);
});

const DOWNLOAD_TIMEOUT_MS = 70 * 60 * 1000;

test('a longer yt-dlp timeout from the host moves the timeout, without being relayed', withClock(async () => {
  const { browser, state } = loadBackground();
  sidebarAction(browser, 'download', { url: VIDEO_URL });
  const [port] = state.ports;

  mock.timers.tick(60 * 1000);
  port.reply({ status: 'info', deadline_s: 3600, final: false });
  mock.timers.tick(3600 * 1000 + DOWNLOAD_TIMEOUT_MS - 1000);
  await flush();
  assert.deepStrictEqual(state.sent, [], 'the deadline is not shown, and the request still runs');

  port.reply({ status: 'success', filename: 'long', final: true });
  await flush();
  assert.deepStrictEqual(finals(state).map(m => [m.status, m.filename]), [['success', 'long']]);
}));

test('the moved timeout still ends a host that never answers, exactly once', withClock(async () => {
  const { browser, state } = loadBackground();
  sidebarAction(browser, 'download', { url: VIDEO_URL });
  const [port] = state.ports;

  port.reply({ status: 'info', deadline_s: 3600, final: false });
  mock.timers.tick(3600 * 1000 + DOWNLOAD_TIMEOUT_MS);
  await flush();

  assert.equal(finals(state).length, 1);
  assert.equal(finals(state)[0].message, 'Native host did not respond in time');
  assert.equal(port.disconnected, true);
  mock.timers.tick(DOWNLOAD_TIMEOUT_MS);
  await flush();
  assert.equal(finals(state).length, 1);
}));

test('a deadline never shortens the timeout', withClock(async () => {
  const { browser, state } = loadBackground();
  sidebarAction(browser, 'download', { url: VIDEO_URL });
  const [port] = state.ports;

  port.reply({ status: 'info', deadline_s: 3600, final: false });
  mock.timers.tick(30 * 60 * 1000);
  // the retry after a repair starts yt-dlp again with a shorter timeout
  port.reply({ status: 'info', deadline_s: 600, final: false });
  mock.timers.tick(3600 * 1000 + DOWNLOAD_TIMEOUT_MS - 30 * 60 * 1000 - 1000);
  await flush();
  assert.equal(finals(state).length, 0);

  mock.timers.tick(1000);
  await flush();
  assert.equal(finals(state).length, 1);
  assert.equal(finals(state)[0].status, 'error');
}));

test('a deadline that is not a positive number is ignored and not relayed', withClock(async () => {
  const { browser, state } = loadBackground();
  sidebarAction(browser, 'download', { url: VIDEO_URL });
  const [port] = state.ports;

  for (const deadline of [-5, 0, 'soon', null, Infinity]) {
    port.reply({ status: 'info', deadline_s: deadline, final: false });
  }
  mock.timers.tick(DOWNLOAD_TIMEOUT_MS);
  await flush();

  assert.deepStrictEqual(state.sent.map(m => [m.final, m.message]), [[true, 'Native host did not respond in time']]);
}));

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

test('toolbar tip: not on Firefox before 118 (no getUserSettings)', withClock(async () => {
  const { browser } = loadBackground();
  browser.browserAction.getUserSettings = async () => { throw new Error('getUserSettings is not a function'); };
  mock.timers.tick(60 * 1000);
  assert.equal(await toolbarTip(browser), false);
}));

test('a response without "final" is final when it is a success or an error', async () => {
  const { browser, state } = loadBackground({ sidebarOpen: true });
  sidebarAction(browser, 'download', { url: VIDEO_URL });
  const [port] = state.ports;
  port.reply({ status: 'warning', message: 'No thumbnail found' });
  port.reply({ status: 'info', message: 'Updating yt-dlp...' });
  await flush();
  assert.equal(finals(state).length, 0, 'warnings and info are progress');

  port.reply({ status: 'success', filename: 'song' });
  port.reply({ status: 'error', message: 'too late' });
  await flush();
  assert.deepStrictEqual(state.sent.map(m => [m.status, m.final]), [
    ['warning', false], ['info', false], ['success', true]
  ], 'nothing after the final response');
  assert.equal(port.disconnected, true);

  sidebarAction(browser, 'download', { url: VIDEO_URL });
  state.ports[1].reply({ status: 'error', message: 'Invalid YouTube URL' });
  await flush();
  assert.deepStrictEqual(finals(state).at(-1), {
    type: 'native_response', status: 'error', message: 'Invalid YouTube URL', requestId: 'req-1', final: true
  });
  assert.equal(state.ports[1].disconnected, true);
});

test('a host that can not be started answers once with an error', async () => {
  const { browser, state } = loadBackground();
  browser.runtime.connectNative = () => { throw new Error('connectNative failed'); };
  sidebarAction(browser, 'get_current_folder');
  mock.timers.tick(60 * 60 * 1000);
  await flush();

  assert.deepStrictEqual(finals(state).map(m => [m.status, m.message]), [['error', 'Could not connect to native host']]);
});

test('a request that can not be sent answers once with an error and closes the port', async () => {
  const { browser, state } = loadBackground();
  const connect = browser.runtime.connectNative;
  browser.runtime.connectNative = (name) => {
    const port = connect(name);
    port.postMessage = () => { throw new Error('Attempt to postMessage on disconnected port'); };
    return port;
  };
  sidebarAction(browser, 'download', { url: VIDEO_URL });
  state.ports[0].exit(null); // Firefox then also reports the port as gone
  mock.timers.tick(60 * 60 * 1000);
  await flush();

  assert.deepStrictEqual(finals(state).map(m => [m.status, m.message]), [['error', 'Failed to send to native host']]);
  assert.equal(state.ports[0].disconnected, true);
});

test('clicks on other context menu entries are ignored', async () => {
  const { browser, state } = loadBackground();
  browser.contextMenus.onClicked.fire({ menuItemId: 'some-other-menu', linkUrl: VIDEO_URL });
  await flush();

  assert.equal(state.ports.length, 0);
  assert.equal(state.notifications.length, 0);
});

test('context menu on a non-video link notifies and does not start the host', async () => {
  const { browser, state } = loadBackground();
  browser.contextMenus.onClicked.fire({ menuItemId: 'download-mp3', linkUrl: 'https://www.youtube.com/@channel' });
  await flush();

  assert.equal(state.ports.length, 0);
  assert.equal(state.notifications[0].title, 'Invalid URL');
});
