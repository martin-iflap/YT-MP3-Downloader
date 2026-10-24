const { test } = require('node:test');
const assert = require('node:assert/strict');
const { loadScripts } = require('./harness');

const { isYouTubeUrl, isKnownSiteUrl, isWebUrl } = loadScripts(['youtube.js'], {});

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

test('known sites: YouTube videos and tracks on the audio sites', () => {
  for (const url of [
    'https://youtu.be/dQw4w9WgXcQ',
    'https://soundcloud.com/forss/flickermood',
    'https://m.soundcloud.com/forss/sets/soulhack',
    'https://on.soundcloud.com/AbC123',
    'https://artist.bandcamp.com/track/song',
    'https://www.mixcloud.com/spartacus/party-time/'
  ]) {
    assert.equal(isKnownSiteUrl(url), true, url);
  }
  for (const url of [
    'https://www.youtube.com/@channel',
    'https://soundcloud.com/',
    'https://soundcloud.com/forss',
    'https://artist.bandcamp.com/album/record',
    'https://bandcamp.com.evil.com/track/x',
    'https://evilbandcamp.com/track/x',
    'https://www.mixcloud.com/spartacus/',
    'https://example.com/a/b',
    'javascript:alert(1)',
    undefined
  ]) {
    assert.equal(isKnownSiteUrl(url), false, String(url));
  }
});

test('any http(s) URL can be pasted', () => {
  for (const url of ['https://example.com/video/1', 'http://vimeo.com/1', 'https://soundcloud.com/forss']) {
    assert.equal(isWebUrl(url), true, url);
  }
  for (const url of ['javascript:alert(1)', 'file:///C:/x.mp3', 'ftp://example.com/x', 'example.com', '', null]) {
    assert.equal(isWebUrl(url), false, String(url));
  }
});
