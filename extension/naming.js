/* exported filenameLimit, filenameTooLong, filenameTooLongMessage */

// The host's filename limits (naming.py; test_versions.py checks they match), so the sidebar
// can refuse a name that does not fit before anything is downloaded. The host still checks
// the cleaned-up name; this counts the name as typed, which is never shorter in practice.
// Lengths are UTF-16 units, as Windows counts them and as JavaScript's .length does.
// A file name can have 255 units on Windows, and ".mp3" takes four.
const MAX_FILENAME_CHARS = 251;
// The whole path; Python can't see a longer one (unless Windows allows long paths).
const MAX_PATH_CHARS = 259;

// The folder as the host's Path prints it: backslashes, no repeated or trailing ones.
function normalizedFolder(folder) {
  return folder.trim().replace(/\//g, '\\').replace(/(?!^)\\+/g, '\\').replace(/\\+$/, '');
}

// The longest filename (without extension) that fits in folder; an empty folder means the
// host's default, which the sidebar does not know, so only the name's own limit applies.
function filenameLimit(folder = '') {
  const path = normalizedFolder(folder);
  if (!path) return MAX_FILENAME_CHARS;
  const room = MAX_PATH_CHARS - path.length - '\\.mp3'.length;
  return Math.max(1, Math.min(MAX_FILENAME_CHARS, room));
}

function filenameTooLong(name, folder) {
  return name.trim().length > filenameLimit(folder);
}

// The host's own message for it (naming.filename_too_long_message).
function filenameTooLongMessage(folder) {
  return `The filename is too long for this folder (at most ${filenameLimit(folder)} characters)`;
}
