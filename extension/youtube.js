/* exported isYouTubeUrl, isKnownSiteUrl, isWebUrl */

// Shared by background.js and sidebar.js (loaded as a plain script in both).
const YOUTUBE_HOSTS = new Set(['youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com', 'youtu.be']);
// Other sites yt-dlp downloads from without an account (tried 2026-10-24): the host (with
// "www."/"m." or, for subdomains, any "<name>.") and what a track's path looks like. Their
// links get the context menu and fill the sidebar's URL field; any other http(s) URL can
// still be pasted (see isWebUrl).
const AUDIO_SITES = [
  { host: 'soundcloud.com', path: /^\/[^/]+\/[^/]+/ }, // /<user>/<track> (a set gives its first track)
  { host: 'on.soundcloud.com', path: /^\/[^/]+/ }, // share links
  { host: 'bandcamp.com', subdomains: true, path: /^\/track\/[^/]+/ }, // <artist>.bandcamp.com
  { host: 'mixcloud.com', path: /^\/[^/]+\/[^/]+/ } // /<user>/<show>
];

function parseWebUrl(value) {
  try {
    const url = new URL(value);
    return url.protocol === 'http:' || url.protocol === 'https:' ? url : null;
  } catch {
    return null;
  }
}

// Any http(s) URL: yt-dlp knows many sites and reports the ones it can't download.
function isWebUrl(value) {
  return parseWebUrl(value) !== null;
}

function isYouTubeUrl(value) {
  const url = parseWebUrl(value);
  if (!url || !YOUTUBE_HOSTS.has(url.hostname)) return false;
  if (url.hostname === 'youtu.be') return url.pathname.length > 1;
  return (url.pathname === '/watch' && url.searchParams.has('v')) ||
    /^\/(shorts|live)\/[^/]+/.test(url.pathname);
}

// A YouTube video or a track on one of AUDIO_SITES.
function isKnownSiteUrl(value) {
  if (isYouTubeUrl(value)) return true;
  const url = parseWebUrl(value);
  if (!url) return false;
  const host = url.hostname.replace(/^(www|m)\./, '');
  const site = AUDIO_SITES.find(s => host === s.host || (s.subdomains && host.endsWith(`.${s.host}`)));
  return Boolean(site) && site.path.test(url.pathname);
}
