// get_version: how background.js checks the host, repairs it (update_host, update_yt_dlp)
// and turns the result into one status for the sidebar.
const { test, afterEach, mock } = require('node:test');
const assert = require('node:assert/strict');
const { loadBackground, flush } = require('./harness');

const INSTALLER_URL = 'https://github.com/martin-iflap/YT-MP3-Downloader/releases/latest/download/YT-MP3-Downloader-Setup.exe';

afterEach(() => mock.timers.reset());

// A healthy report from the host (see health.host_status), with overrides per test.
function report(overrides = {}) {
  return Object.assign({
    status: 'success',
    version: '2.0.0',
    latest: '2.0.0',
    outdated: false,
    self_update: true,
    yt_dlp: { installed: true, version: '2026.08.19', latest: '2026.08.19', outdated: false },
    ffmpeg: { installed: true },
    js_runtime: { name: 'deno', installed: false },
    final: true
  }, overrides);
}

const OLD_YT_DLP = { yt_dlp: { installed: true, version: '2026.07.01', latest: '2026.08.19', outdated: true } };

// A fake host that answers every request by its action. answers[action] is one answer for
// every request, or an array used up in order (the last one repeats). An answer is a
// response (final unless it says otherwise) or a function receiving the port, e.g. to exit.
function scriptedHost(answers) {
  const calls = [];
  const nativeHost = (message, port) => {
    calls.push(message.action);
    const script = answers[message.action];
    const answer = Array.isArray(script) ? (script.length > 1 ? script.shift() : script[0]) : script;
    if (typeof answer === 'function') answer(port);
    else if (answer) port.reply(Object.assign({ final: true }, answer));
  };
  return { nativeHost, calls };
}

// Let the check-and-repair chain run: each step is a fresh port and a few promise turns.
async function settle(rounds = 20) {
  for (let i = 0; i < rounds; i++) await flush();
}

// While polling for a host update the chain sleeps; advance the mocked clock step by step.
async function settleWithClock(totalMs, stepMs = 5000) {
  for (let elapsed = 0; elapsed <= totalMs; elapsed += stepMs) {
    await settle(5);
    mock.timers.tick(stepMs);
  }
  await settle();
}

// Send get_version from the sidebar to a scripted host and return what the sidebar got.
async function checkHost(answers, { clockMs = 0 } = {}) {
  const { nativeHost, calls } = scriptedHost(answers);
  const { browser, state } = loadBackground({ nativeHost });
  browser.runtime.onMessage.fire({ type: 'native_action', requestId: 'req-1', action: 'get_version' });
  if (clockMs) await settleWithClock(clockMs);
  else await settle();
  const finals = state.sent.filter(m => m.final === true);
  assert.equal(finals.length, 1, 'exactly one final response');
  return {
    final: finals[0],
    progress: state.sent.filter(m => m.final === false).map(m => m.message),
    calls,
    state
  };
}

// --- versions -----------------------------------------------------------------------

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
  assert.equal(cmp('1.2.0', '2.0.0'), -1, 'the first part decides');
  assert.equal(cmp('2026.08.19.1', '2026.08.19'), 1);
});

// --- status without repairs ---------------------------------------------------------

test('a healthy host is relayed as success with its report, and nothing is repaired', async () => {
  const { final, calls, state } = await checkHost({ get_version: report() });
  assert.equal(final.status, 'success');
  assert.equal(final.version, '2.0.0');
  assert.equal(final.message, undefined);
  assert.deepStrictEqual(calls, ['get_version']);
  assert.equal(state.ports[0].disconnected, true);
});

test('a missing JavaScript runtime is not reported yet (no fix is shipped)', async () => {
  const { final } = await checkHost({ get_version: report({ js_runtime: { name: 'deno', installed: false } }) });
  assert.equal(final.status, 'success');
});

test('a 1.x host (answers get_version as a download) is told to update, without repairs', async () => {
  const { final, calls } = await checkHost({ get_version: { status: 'error', message: 'Invalid YouTube URL' } });
  assert.equal(final.status, 'warning');
  assert.match(final.message, /can't update itself\. Install the new version once/);
  assert.equal(final.helpUrl, INSTALLER_URL);
  assert.deepStrictEqual(calls, ['get_version'], 'a 1.x host has no updater');
});

test('a host below MIN_HOST_VERSION that can not update itself is told to update', async () => {
  const { final, calls } = await checkHost({ get_version: report({ version: '1.9.9', self_update: false, outdated: true }) });
  assert.equal(final.status, 'warning');
  assert.match(final.message, /out of date/);
  assert.equal(final.helpUrl, INSTALLER_URL);
  assert.deepStrictEqual(calls, ['get_version']);
});

test('a v2 host that failed to report is relayed as its own error, not as outdated', async () => {
  const failure = { status: 'error', message: "Error processing action 'get_version': boom" };
  const { final, calls } = await checkHost({ get_version: failure });
  assert.equal(final.status, 'error');
  assert.equal(final.message, failure.message);
  assert.equal(final.helpUrl, undefined);
  assert.deepStrictEqual(calls, ['get_version']);
});

test('a host that closes without answering is reported as a broken install', async () => {
  const { final } = await checkHost({ get_version: port => port.exit(null) });
  assert.equal(final.status, 'error');
  assert.match(final.message, /not working/);
  assert.equal(final.helpUrl, INSTALLER_URL);
});

test('host not installed: the not-installed message, not "broken" or "out of date"', async () => {
  const { final } = await checkHost({ get_version: port => port.exit('No such native application app_manifest') });
  assert.equal(final.status, 'error');
  assert.match(final.message, /not installed/);
  assert.equal(final.helpUrl, INSTALLER_URL);
});

test('a missing ffmpeg is an error (only a reinstall brings it back)', async () => {
  for (const ffmpeg of [{ installed: false }, undefined]) {
    const { final, calls } = await checkHost({ get_version: report({ ffmpeg }) });
    assert.equal(final.status, 'error');
    assert.match(final.message, /ffmpeg\.exe is missing/);
    assert.equal(final.helpUrl, INSTALLER_URL);
    assert.deepStrictEqual(calls, ['get_version']);
  }
});

test('offline (latest versions unknown) is not a problem and repairs nothing', async () => {
  const offline = report({ latest: null, outdated: null, yt_dlp: { installed: true, version: '2026.08.19', latest: null, outdated: null } });
  const { final, calls } = await checkHost({ get_version: offline });
  assert.equal(final.status, 'success');
  assert.deepStrictEqual(calls, ['get_version']);
});

test('only get_version answers are assessed', async () => {
  const { browser, state } = loadBackground();
  browser.runtime.onMessage.fire({ type: 'native_action', requestId: 'req-1', action: 'get_current_folder' });
  state.ports[0].reply({ status: 'error', message: 'Invalid YouTube URL', final: true });
  await flush();
  assert.equal(state.sent[0].message, 'Invalid YouTube URL');
  assert.equal(state.sent[0].status, 'error');
});

// --- yt-dlp repairs -----------------------------------------------------------------

test('an outdated yt-dlp is updated, and the fresh check decides the status', async () => {
  const { final, progress, calls } = await checkHost({
    get_version: [report(OLD_YT_DLP), report()],
    update_yt_dlp: { status: 'success', yt_dlp: { version: '2026.08.19' } }
  });
  assert.deepStrictEqual(calls, ['get_version', 'update_yt_dlp', 'get_version']);
  assert.deepStrictEqual(progress, ['Updating yt-dlp...']);
  assert.equal(final.status, 'success');
  assert.equal(final.yt_dlp.version, '2026.08.19');
});

test('a missing or broken yt-dlp is repaired the same way', async () => {
  for (const yt_dlp of [
    { installed: false, version: null, latest: '2026.08.19', outdated: null },
    { installed: true, version: null, latest: '2026.08.19', outdated: null },
    null
  ]) {
    const { final, calls } = await checkHost({
      get_version: [report({ yt_dlp }), report()],
      update_yt_dlp: { status: 'success', yt_dlp: { version: '2026.08.19' } }
    });
    assert.deepStrictEqual(calls, ['get_version', 'update_yt_dlp', 'get_version'], JSON.stringify(yt_dlp));
    assert.equal(final.status, 'success');
  }
});

test('a failed yt-dlp update keeps the problem and says why, without retrying', async () => {
  const { final, calls } = await checkHost({
    get_version: report(OLD_YT_DLP),
    update_yt_dlp: { status: 'error', message: 'yt-dlp could not be downloaded: GitHub could not be reached' }
  });
  assert.deepStrictEqual(calls, ['get_version', 'update_yt_dlp']);
  assert.equal(final.status, 'warning');
  assert.match(final.message, /yt-dlp is out of date \(2026\.07\.01, latest 2026\.08\.19\)/);
  assert.match(final.message, /Automatic repair failed: yt-dlp could not be downloaded: GitHub could not be reached\.$/);
  assert.equal(final.helpUrl, INSTALLER_URL);
});

test('a broken yt-dlp that the update could not fix is an error with the reason', async () => {
  const { final } = await checkHost({
    get_version: report({ yt_dlp: { installed: true, version: null, latest: null, outdated: null } }),
    update_yt_dlp: { status: 'error', message: 'yt-dlp could not be downloaded: GitHub could not be reached' }
  });
  assert.equal(final.status, 'error');
  assert.match(final.message, /yt-dlp\.exe is not working\. Reinstall .* Automatic repair failed/);
});

test('a repair runs at most once per check, even if it did not help', async () => {
  const { final, calls } = await checkHost({
    get_version: report(OLD_YT_DLP),
    update_yt_dlp: { status: 'success', yt_dlp: { version: '2026.07.01' } }
  });
  assert.deepStrictEqual(calls, ['get_version', 'update_yt_dlp', 'get_version']);
  assert.equal(final.status, 'warning');
});

test('an update that times out does not hang the check', async () => {
  mock.timers.enable({ apis: ['setTimeout'] });
  const { final, calls } = await checkHost({
    get_version: report(OLD_YT_DLP),
    update_yt_dlp: () => {} // never answers
  }, { clockMs: 6 * 60 * 1000 });
  assert.deepStrictEqual(calls, ['get_version', 'update_yt_dlp']);
  assert.match(final.message, /Automatic repair failed: Native host did not respond in time/);
});

// --- host updates -------------------------------------------------------------------

test('a newer host release is installed, then the check waits for the new version', async () => {
  mock.timers.enable({ apis: ['setTimeout'] });
  const newHost = report({ version: '2.1.0', latest: 'v2.1.0' });
  const { final, progress, calls } = await checkHost({
    get_version: [
      report({ latest: 'v2.1.0', outdated: true }),
      report({ latest: 'v2.1.0', outdated: true }), // the installer is still running
      port => port.exit(null), // the old version folder is being removed
      newHost
    ],
    update_host: { status: 'success', version: 'v2.1.0' }
  }, { clockMs: 30 * 1000 });
  assert.deepStrictEqual(calls, ['get_version', 'update_host', 'get_version', 'get_version', 'get_version']);
  assert.deepStrictEqual(progress, ['Updating the YT MP3 Downloader app...']);
  assert.equal(final.status, 'success');
  assert.equal(final.version, '2.1.0');
});

test('the new host can still repair yt-dlp after updating itself', async () => {
  mock.timers.enable({ apis: ['setTimeout'] });
  const { final, calls } = await checkHost({
    get_version: [report({ latest: 'v2.1.0', outdated: true, ...OLD_YT_DLP }), report({ version: '2.1.0', ...OLD_YT_DLP }), report({ version: '2.1.0' })],
    update_host: { status: 'success', version: 'v2.1.0' },
    update_yt_dlp: { status: 'success', yt_dlp: { version: '2026.08.19' } }
  }, { clockMs: 20 * 1000 });
  assert.deepStrictEqual(calls, ['get_version', 'update_host', 'get_version', 'update_yt_dlp', 'get_version']);
  assert.equal(final.status, 'success');
});

test('a host update that never arrives ends as a warning with the manual link', async () => {
  mock.timers.enable({ apis: ['setTimeout'] });
  const { final, calls } = await checkHost({
    get_version: report({ latest: 'v2.1.0', outdated: true }),
    update_host: { status: 'success', version: 'v2.1.0' }
  }, { clockMs: 4 * 60 * 1000 });
  assert.equal(calls.filter(c => c === 'update_host').length, 1);
  assert.equal(calls.filter(c => c === 'get_version').length, 1 + 36, 'one check, then every 5 s for 3 min');
  assert.equal(final.status, 'warning');
  assert.match(final.message, /could not update itself \(version v2\.1\.0 did not finish installing\)/);
  assert.equal(final.helpUrl, INSTALLER_URL);
});

test('a failed host update still lets yt-dlp be repaired, and both are reported', async () => {
  const { final, calls } = await checkHost({
    get_version: [report({ latest: 'v2.1.0', outdated: true, ...OLD_YT_DLP }), report({ latest: 'v2.1.0', outdated: true })],
    update_host: { status: 'error', message: 'Could not download YT-MP3-Downloader-Setup.exe: timed out' },
    update_yt_dlp: { status: 'success', yt_dlp: { version: '2026.08.19' } }
  });
  assert.deepStrictEqual(calls, ['get_version', 'update_host', 'update_yt_dlp', 'get_version']);
  assert.equal(final.status, 'warning');
  assert.match(final.message, /could not update itself \(Could not download YT-MP3-Downloader-Setup\.exe: timed out\)/);
});

test('a host that is already the latest (update_host has nothing to do) just re-checks', async () => {
  const { final, calls } = await checkHost({
    get_version: [report({ latest: 'v2.1.0', outdated: true }), report()],
    update_host: { status: 'success', version: null }
  });
  assert.deepStrictEqual(calls, ['get_version', 'update_host', 'get_version']);
  assert.equal(final.status, 'success');
});

test('a host that can not update itself (source checkout) is not asked to', async () => {
  const { final, calls } = await checkHost({ get_version: report({ latest: 'v2.1.0', outdated: true, self_update: false }) });
  assert.deepStrictEqual(calls, ['get_version']);
  assert.equal(final.status, 'success');
});

// --- concurrency --------------------------------------------------------------------

test('two sidebars asking at once share one check and both get a final answer', async () => {
  const { nativeHost, calls } = scriptedHost({
    get_version: [report(OLD_YT_DLP), report()],
    update_yt_dlp: { status: 'success', yt_dlp: { version: '2026.08.19' } }
  });
  const { browser, state } = loadBackground({ nativeHost });
  browser.runtime.onMessage.fire({ type: 'native_action', requestId: 'window-1', action: 'get_version' });
  browser.runtime.onMessage.fire({ type: 'native_action', requestId: 'window-2', action: 'get_version' });
  await settle();
  assert.deepStrictEqual(calls, ['get_version', 'update_yt_dlp', 'get_version']);
  const finals = state.sent.filter(m => m.final === true);
  assert.deepStrictEqual(finals.map(m => [m.requestId, m.status]), [['window-1', 'success'], ['window-2', 'success']]);
});
