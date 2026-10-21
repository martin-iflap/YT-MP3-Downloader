const { test } = require('node:test');
const assert = require('node:assert/strict');
const { loadScripts } = require('./harness');

const { isYouTubeUrl } = loadScripts(['youtube.js'], {});

test('accepts video, shorts, live and short-link URLs', () => {
  for (const url of [
    'https://www.youtube.com/watch?v=dQw4w9WgXcQ',
    'https://youtube.com/watch?v=dQw4w9WgXcQ&t=42s',
    'http://m.youtube.com/watch?v=dQw4w9WgXcQ',
    'https://music.youtube.com/watch?v=dQw4w9WgXcQ',
    'https://www.youtube.com/shorts/abc123',
    'https://www.youtube.com/live/abc123?si=x',
    'https://youtu.be/dQw4w9WgXcQ'
  ]) {
    assert.equal(isYouTubeUrl(url), true, url);
  }
});

test('rejects non-video pages, other hosts and junk', () => {
  for (const url of [
    'https://www.youtube.com/',
    'https://www.youtube.com/watch',
    'https://www.youtube.com/@channel',
    'https://youtu.be/',
    'https://www.youtube.com/shorts/',
    'https://www.youtube.com/live/',
    'https://evil.com/?youtube.com/watch?v=x',
    'https://youtube.com.evil.com/watch?v=x',
    'javascript:alert("youtube.com/watch")',
    'ftp://youtube.com/watch?v=x',
    'not a url',
    '',
    undefined,
    null
  ]) {
    assert.equal(isYouTubeUrl(url), false, String(url));
  }
});
