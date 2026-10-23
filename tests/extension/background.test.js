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

test('sidebar download: forwards payload, relays progress, then one final and disconnects', async () => {
  const { browser, state } = loadBackground({ sidebarOpen: true });
  sidebarAction(browser, 'download', { url: VIDEO_URL, folder: 'C:\\Music', filename: 'song' });

  const [port] = state.ports;
  assert.equal(port.name, 'app_manifest');
  assert.deepStrictEqual(port.posted, [{ action: 'download', url: VIDEO_URL, folder: 'C:\\Music', filename: 'song' }]);

  port.reply({ status: 'warning', message: 'Invalid folder path, using default', final: false });
  port.reply({ status: 'success', filename: 'song', final: true });
  await flush();

  assert.deepStrictEqual(state.sent, [
    { type: 'native_response', status: 'warning', message: 'Invalid folder path, using default', requestId: 'req-1', final: false },
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
  state.ports[0].reply({ status: 'error', message: "Path doesn't exist", final: true });
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

test('context menu download without a saved folder lets the host pick its default', async () => {
  const { browser, state } = loadBackground();
  browser.contextMenus.onClicked.fire({ menuItemId: 'download-mp3', linkUrl: 'https://youtu.be/dQw4w9WgXcQ' });
  await flush();

  assert.deepStrictEqual(state.ports[0].posted, [{ action: 'download', url: 'https://youtu.be/dQw4w9WgXcQ' }]);
});

test('context menu on a non-video link notifies and does not start the host', async () => {
  const { browser, state } = loadBackground();
  browser.contextMenus.onClicked.fire({ menuItemId: 'download-mp3', linkUrl: 'https://www.youtube.com/@channel' });
  await flush();

  assert.equal(state.ports.length, 0);
  assert.equal(state.notifications[0].title, 'Invalid URL');
});
