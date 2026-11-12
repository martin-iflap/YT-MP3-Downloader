/* exported THEMES, storedTheme, nextTheme, applyTheme */

// The sidebar's color theme. 'auto' follows what Firefox reports (prefers-color-scheme, which
// in the sidebar comes from the Firefox theme and does not always arrive), 'light' and 'dark'
// are the user's choice from the toggle. The CSS reads it from <html data-theme>.
// Loaded in <head> and kept in localStorage, which is synchronous, so the page is drawn in its
// theme from the start (storage.local would first show a dark page, then switch).
const THEMES = ['auto', 'light', 'dark'];
const THEME_KEY = 'theme';
const THEME_LABELS = { auto: 'Auto', light: 'Light', dark: 'Dark' };
const lightQuery = matchMedia('(prefers-color-scheme: light)');

// The saved theme, or 'auto' when there is none or the storage can't be read.
function storedTheme() {
  try {
    const theme = localStorage.getItem(THEME_KEY);
    return THEMES.includes(theme) ? theme : 'auto';
  } catch {
    return 'auto';
  }
}

function nextTheme(theme) {
  return THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length];
}

function applyTheme(theme) {
  const light = theme === 'light' || (theme === 'auto' && lightQuery.matches);
  document.documentElement.dataset.theme = light ? 'light' : 'dark';
}

// kept in memory too, so the toggle still cycles when the storage can't be written
let currentTheme = storedTheme();
applyTheme(currentTheme);
lightQuery.addEventListener('change', () => applyTheme(currentTheme));

document.addEventListener('DOMContentLoaded', () => {
  const toggle = document.getElementById('themeToggle');
  const show = (theme) => {
    toggle.dataset.theme = theme; // the CSS shows its icon
    const label = `Theme: ${THEME_LABELS[theme]} (click for ${THEME_LABELS[nextTheme(theme)]})`;
    toggle.title = label;
    toggle.setAttribute('aria-label', label);
  };
  show(currentTheme);
  toggle.addEventListener('click', () => {
    currentTheme = nextTheme(currentTheme);
    try {
      localStorage.setItem(THEME_KEY, currentTheme);
    } catch (e) {
      console.error('Could not save the theme:', e);
    }
    applyTheme(currentTheme);
    show(currentTheme);
  });
});
