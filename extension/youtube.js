/* exported isYouTubeUrl */

// Shared by background.js and sidebar.js (loaded as a plain script in both).
const YOUTUBE_HOSTS = new Set(['youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com', 'youtu.be']);

function isYouTubeUrl(value) {
  let url;
  try {
    url = new URL(value);
  } catch (e) {
    return false;
  }
  if ((url.protocol !== 'http:' && url.protocol !== 'https:') || !YOUTUBE_HOSTS.has(url.hostname)) return false;
  if (url.hostname === 'youtu.be') return url.pathname.length > 1;
  return (url.pathname === '/watch' && url.searchParams.has('v')) || url.pathname.startsWith('/shorts/');
}
