const { test } = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const { loadScripts } = require('./harness');

const context = loadScripts(['formats.js'], {});
const { knownFormat, knownOption, formatRequest, hostSupports } = context;
const FORMATS = vm.runInContext('FORMATS', context);

test('an MP3 request carries no format, so every host version understands it', () => {
  assert.deepStrictEqual({ ...formatRequest('mp3') }, {});
  assert.deepStrictEqual({ ...formatRequest('mp3', { quality: '720' }) }, {});
});

test('an MP4 request carries the format and every MP4 choice', () => {
  assert.deepStrictEqual({ ...formatRequest('mp4', { quality: '720', codec: 'compatible' }) },
    { format: 'mp4', quality: '720', codec: 'compatible' });
  assert.deepStrictEqual({ ...formatRequest('mp4', { quality: '1080', codec: 'best', other: 'x' }) },
    { format: 'mp4', quality: '1080', codec: 'best' }, 'only the format\'s own options');
});

test('a missing or unknown choice becomes the option\'s default (the first one)', () => {
  assert.deepStrictEqual({ ...formatRequest('mp4') }, { format: 'mp4', quality: 'best', codec: 'best' });
  assert.deepStrictEqual({ ...formatRequest('mp4', { quality: '144', codec: 'h265' }) },
    { format: 'mp4', quality: 'best', codec: 'best' });
  assert.equal(knownOption('mp4', 'quality', '480'), '480');
  assert.equal(knownOption('mp4', 'quality', undefined), 'best');
});

test('a stored format this version does not know falls back to MP3', () => {
  assert.equal(knownFormat('mp4'), 'mp4');
  assert.equal(knownFormat('mp3'), 'mp3');
  assert.equal(knownFormat('webm'), 'mp3');
  assert.equal(knownFormat(undefined), 'mp3');
  assert.equal(knownFormat('toString'), 'mp3', 'not an inherited property');
});

test('a host gets another format than MP3 only when it lists it', () => {
  assert.equal(hostSupports(undefined, 'mp3'), true, 'every host downloads MP3');
  assert.equal(hostSupports([], 'mp3'), true);
  assert.equal(hostSupports(['mp3', 'mp4'], 'mp4'), true);
  assert.equal(hostSupports(['mp3'], 'mp4'), false);
  assert.equal(hostSupports(undefined, 'mp4'), false, 'a host before 2.2.0 lists none');
  assert.equal(hostSupports(null, 'mp4'), false);
  assert.equal(hostSupports('mp4', 'mp4'), false, 'only a list counts');
});

test('every format has a label, and every option a default and distinct values', () => {
  for (const [format, { label, options }] of Object.entries(FORMATS)) {
    assert.equal(label, format.toUpperCase());
    for (const [name, choices] of Object.entries(options)) {
      assert.ok(choices.length > 0, `${format}.${name} has no choices`);
      const values = choices.map(([value]) => value);
      assert.equal(new Set(values).size, values.length, `${format}.${name} repeats a value`);
    }
  }
});
