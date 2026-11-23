/* exported FORMATS, DEFAULT_FORMAT, knownFormat, knownOption, formatRequest, hostSupports, progressText */

// The formats the sidebar downloads. One form serves them all: the URL, folder and filename
// fields are shared, and a format's own choices (options, each a list of [value, label], the
// first one the default) show only while it is selected. The sidebar builds its selects
// from this list, so a new choice is added here only. processing is the progress text while
// yt-dlp encodes or merges the download (see progressText).
const FORMATS = {
  mp3: { label: 'MP3', processing: 'Converting to MP3...', options: {} },
  mp4: {
    label: 'MP4',
    processing: 'Saving the MP4...',
    options: {
      quality: [['best', 'Best available'], ['1080', '1080p'], ['720', '720p'], ['480', '480p']],
      codec: [['best', 'Best (AV1/VP9, smaller files)'], ['compatible', 'Compatible (H.264, plays everywhere)']]
    }
  }
};

// MP3 requests carry no format: a host that gets none downloads an MP3, so they stay what
// every host version understands.
const DEFAULT_FORMAT = 'mp3';

// A stored format, or the default if this version doesn't know it.
function knownFormat(format) {
  return Object.hasOwn(FORMATS, format) ? format : DEFAULT_FORMAT;
}

// A stored choice, or the option's default if it no longer exists.
function knownOption(format, name, value) {
  const values = FORMATS[format].options[name].map(([known]) => known);
  return values.includes(value) ? value : values[0];
}

// The fields a download request adds for this format and the user's choices.
function formatRequest(format, choices = {}) {
  if (format === DEFAULT_FORMAT) return {};
  const fields = { format };
  for (const name of Object.keys(FORMATS[format].options)) fields[name] = knownOption(format, name, choices[name]);
  return fields;
}

// Whether the host can download format. formats is what its get_version answer lists
// (`formats`); a host without that list (before 2.2.0) only knows MP3 and would ignore the
// format field, so it must not get another format.
function hostSupports(formats, format) {
  return format === DEFAULT_FORMAT || (Array.isArray(formats) && formats.includes(format));
}

// The text for a download's progress, the host's { stage, fraction }: the fraction of the
// whole download (an MP4's video and audio together), null while its size is unknown.
// Matches the app window's texts (gui.show_progress).
function progressText(progress, format) {
  if (progress.stage === 'processing') return FORMATS[format].processing;
  return Number.isFinite(progress.fraction) ? `Downloading ${Math.round(progress.fraction * 100)}%` : 'Downloading...';
}
