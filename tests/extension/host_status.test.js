// get_version: how background.js turns the host's report into one status for the sidebar.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { loadBackground, flush } = require('./harness');

const INSTALLER_URL = 'https://github.com/martin-iflap/YT-MP3-Downloader/releases/latest';

// A healthy report from the host (see health.host_status), with overrides per test.
function report(overrides = {}) {
  return Object.assign({
    status: 'success',
    version: '2.0.0',
    yt_dlp: { installed: true, version: '2026.08.19', latest: '2026.08.19', outdated: false },
    ffmpeg: { installed: true },
    js_runtime: { name: 'deno', installed: false },
    final: true
  }, overrides);
}

// Send get_version from the sidebar, let the host answer, return the one final response.
async function checkHost(answer) {
  const { browser, state } = loadBackground();
  browser.runtime.onMessage.fire({ type: 'native_action', requestId: 'req-1', action: 'get_version' });
  const [port] = state.ports;
  assert.deepStrictEqual(port.posted, [{ action: 'get_version' }]);
  if (typeof answer === 'function') answer(port);
  else port.reply(answer);
  await flush();
  const finals = state.sent.filter(m => m.final === true);
  assert.equal(finals.length, 1);
  return { final: finals[0], port };
}

test('parseVersion accepts plain and v-prefixed dotted versions only', () => {
  const { context } = loadBackground();
  const parse = text => { const v = context.parseVersion(text); return v && [...v]; };
  assert.deepStrictEqual(parse('2.0.0'), [2, 0, 0]);
  assert.deepStrictEqual(parse('v2.1'), [2, 1]);
  assert.deepStrictEqual(parse(' 2026.08.19 '), [2026, 8, 19]);
  for (const bad of ['', 'v', '2.0.', '2.0.0-beta', 'v-1.1', 'abc', null, undefined, 2]) {
    assert.equal(context.parseVersion(bad), null, String(bad));
  }
});

test('compareVersions is numeric and pads missing parts', () => {
  const { context } = loadBackground();
  const cmp = (a, b) => context.compareVersions(context.parseVersion(a), context.parseVersion(b));
  assert.equal(cmp('10.0.0', '9.9.9'), 1, 'numeric, not text order');
  assert.equal(cmp('2.0', '2.0.0'), 0);
  assert.equal(cmp('2.0.0', '2.0.1'), -1);
  assert.equal(cmp('2026.08.19.1', '2026.08.19'), 1);
});

test('a healthy host is relayed as success with its report', async () => {
  const { final, port } = await checkHost(report());
  assert.equal(final.status, 'success');
  assert.equal(final.version, '2.0.0');
  assert.equal(final.message, undefined);
  assert.equal(port.disconnected, true);
});

test('a newer host than required is fine', async () => {
  const { final } = await checkHost(report({ version: '3.4.5' }));
  assert.equal(final.status, 'success');
});

test('a missing JavaScript runtime is not reported yet (no fix is shipped)', async () => {
  const { final } = await checkHost(report({ js_runtime: { name: 'deno', installed: false } }));
  assert.equal(final.status, 'success');
});

test('a 1.x host (answers get_version as a download) is told to update', async () => {
  const { final } = await checkHost({ status: 'error', message: 'Invalid YouTube URL' });
  assert.equal(final.status, 'warning');
  assert.match(final.message, /out of date \(version 2\.0\.0 or newer/);
  assert.equal(final.helpUrl, INSTALLER_URL);
});

test('a host below MIN_HOST_VERSION is told to update', async () => {
  const { final } = await checkHost(report({ version: '1.9.9' }));
  assert.equal(final.status, 'warning');
  assert.match(final.message, /out of date/);
  assert.equal(final.helpUrl, INSTALLER_URL);
});

test('a v2 host that failed to report is relayed as its own error, not as outdated', async () => {
  const failure = { status: 'error', message: "Error processing action 'get_version': boom", final: true };
  const { final } = await checkHost(failure);
  assert.equal(final.status, 'error');
  assert.equal(final.message, failure.message);
  assert.equal(final.helpUrl, undefined);
});

test('tool problems: missing or broken tools are errors, an old yt-dlp is a warning', async () => {
  const cases = [
    [{ ffmpeg: { installed: false } }, 'error', /ffmpeg\.exe is missing/],
    [{ ffmpeg: undefined }, 'error', /ffmpeg\.exe is missing/],
    [{ yt_dlp: { installed: false, version: null, latest: '2026.08.19', outdated: null } }, 'error', /yt-dlp\.exe is missing/],
    [{ yt_dlp: { installed: true, version: null, latest: null, outdated: null } }, 'error', /yt-dlp\.exe is not working/],
    [{ yt_dlp: null }, 'error', /yt-dlp\.exe is missing/],
    [{ yt_dlp: { installed: true, version: '2026.07.01', latest: '2026.08.19', outdated: true } }, 'warning',
      /yt-dlp is out of date \(2026\.07\.01, latest 2026\.08\.19\)/]
  ];
  for (const [overrides, status, message] of cases) {
    const { final } = await checkHost(report(overrides));
    assert.equal(final.status, status, JSON.stringify(overrides));
    assert.match(final.message, message);
    assert.equal(final.helpUrl, INSTALLER_URL);
  }
});

test('ffmpeg missing outranks an old yt-dlp', async () => {
  const { final } = await checkHost(report({
    ffmpeg: { installed: false },
    yt_dlp: { installed: true, version: '2026.07.01', latest: '2026.08.19', outdated: true }
  }));
  assert.match(final.message, /ffmpeg/);
});

test('offline (latest unknown) is not a problem', async () => {
  const { final } = await checkHost(report({ yt_dlp: { installed: true, version: '2026.08.19', latest: null, outdated: null } }));
  assert.equal(final.status, 'success');
});

test('host not installed: the not-installed message, not "out of date"', async () => {
  const { final } = await checkHost(port => port.exit('No such native application app_manifest'));
  assert.equal(final.status, 'error');
  assert.match(final.message, /not installed/);
  assert.equal(final.helpUrl, INSTALLER_URL);
});

test('only get_version answers are assessed', async () => {
  const { browser, state } = loadBackground();
  browser.runtime.onMessage.fire({ type: 'native_action', requestId: 'req-1', action: 'get_current_folder' });
  state.ports[0].reply({ status: 'error', message: 'Invalid YouTube URL', final: true });
  await flush();
  assert.equal(state.sent[0].message, 'Invalid YouTube URL');
  assert.equal(state.sent[0].status, 'error');
});
