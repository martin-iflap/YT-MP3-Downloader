const { test } = require('node:test');
const assert = require('node:assert/strict');
const { loadScripts } = require('./harness');

const { filenameLimit, filenameTooLong, filenameTooLongMessage, normalizedFolder } = loadScripts(['naming.js'], {});

// A folder whose path is exactly length characters long.
const folderOfLength = length => `C:\\${'a'.repeat(length - 3)}`;

test('folders are counted the way the host prints them', () => {
  // what str(pathlib.Path(folder)) gives on Windows
  assert.equal(normalizedFolder(' C:\\Music\\ '), 'C:\\Music');
  assert.equal(normalizedFolder('C:/Music//sub/'), 'C:\\Music\\sub');
  assert.equal(normalizedFolder('\\\\server\\share\\x'), '\\\\server\\share\\x', 'a network path keeps its leading \\\\');
});

test('an unknown folder (the host default) leaves the name its own limit of 251', () => {
  assert.equal(filenameLimit(), 251);
  assert.equal(filenameLimit(''), 251);
  assert.equal(filenameLimit('   '), 251);
});

test('the folder takes its share of the 259 characters, with "\\" and ".mp3"', () => {
  assert.equal(filenameLimit('C:\\Music'), 259 - 8 - 5);
  assert.equal(filenameLimit(folderOfLength(100)), 259 - 100 - 5);
  assert.equal(filenameLimit(`${folderOfLength(100)}\\`), 259 - 100 - 5, 'a trailing \\ is not counted');
  assert.equal(filenameLimit(folderOfLength(3)), 251, 'never more than a name can have');
  assert.equal(filenameLimit(folderOfLength(253)), 1);
  assert.equal(filenameLimit(folderOfLength(400)), 1, 'never below 1: the host then refuses the folder itself');
});

test('names are counted in UTF-16 units like Windows does, without surrounding spaces', () => {
  const folder = folderOfLength(100); // room for 154
  assert.equal(filenameTooLong('a'.repeat(154), folder), false);
  assert.equal(filenameTooLong('a'.repeat(155), folder), true);
  assert.equal(filenameTooLong(`  ${'a'.repeat(154)}  `, folder), false);
  assert.equal(filenameTooLong('😀'.repeat(77), folder), false, 'an emoji is two units');
  assert.equal(filenameTooLong(`${'😀'.repeat(77)}a`, folder), true);
  assert.equal(filenameTooLong('a'.repeat(251), ''), false);
  assert.equal(filenameTooLong('a'.repeat(252), ''), true);
});

test('the message names the limit for that folder, like the host\'s', () => {
  assert.equal(filenameTooLongMessage(folderOfLength(100)),
    'The filename is too long for this folder (at most 154 characters)');
});
