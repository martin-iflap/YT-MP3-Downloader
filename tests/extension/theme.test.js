const { test } = require('node:test');
const assert = require('node:assert/strict');
const { loadScripts } = require('./harness');

// theme.js with a fake page: localStorage (or one that throws), prefers-color-scheme and the toggle.
function loadTheme({ stored, prefersLight = false, storageFails = false } = {}) {
  const items = new Map(stored === undefined ? [] : [['theme', stored]]);
  const failing = () => { throw new Error('storage is disabled'); };
  const localStorage = storageFails
    ? { getItem: failing, setItem: failing }
    : { getItem: key => items.get(key) ?? null, setItem: (key, value) => items.set(key, value) };
  const query = { matches: prefersLight, listeners: [], addEventListener(type, fn) { this.listeners.push(fn); } };
  const toggle = { dataset: {}, title: '', attributes: {}, listeners: [],
    setAttribute(name, value) { this.attributes[name] = value; },
    addEventListener(type, fn) { this.listeners.push(fn); } };
  const html = { dataset: {} };
  let ready;
  const document = {
    documentElement: html,
    getElementById: id => (id === 'themeToggle' ? toggle : null),
    addEventListener: (type, fn) => { ready = fn; }
  };
  const quiet = { ...console, error: () => {} };
  loadScripts(['theme.js'], { localStorage, matchMedia: () => query, document, console: quiet });
  ready();
  return {
    items, query, toggle,
    theme: () => html.dataset.theme,
    click: () => toggle.listeners.forEach(fn => fn()),
    schemeChanges: (light) => { query.matches = light; query.listeners.forEach(fn => fn()); }
  };
}

test('auto (the default) follows what Firefox reports, also when it changes', () => {
  const page = loadTheme();
  assert.equal(page.theme(), 'dark');
  page.schemeChanges(true);
  assert.equal(page.theme(), 'light');
  assert.equal(loadTheme({ prefersLight: true }).theme(), 'light');
});

test('a chosen theme wins over what Firefox reports', () => {
  assert.equal(loadTheme({ stored: 'light' }).theme(), 'light');
  const dark = loadTheme({ stored: 'dark', prefersLight: true });
  assert.equal(dark.theme(), 'dark');
  dark.schemeChanges(false);
  dark.schemeChanges(true);
  assert.equal(dark.theme(), 'dark');
});

test('an unknown saved theme or unreadable storage means auto', () => {
  assert.equal(loadTheme({ stored: 'sepia', prefersLight: true }).theme(), 'light');
  assert.equal(loadTheme({ storageFails: true }).theme(), 'dark');
});

test('the toggle cycles auto, light, dark, saves the choice and says what is next', () => {
  const page = loadTheme();
  assert.equal(page.toggle.title, 'Theme: Auto (click for Light)');
  assert.equal(page.toggle.attributes['aria-label'], page.toggle.title);
  assert.equal(page.toggle.dataset.theme, 'auto', 'the button shows the icon of the choice');
  page.click();
  assert.deepStrictEqual([page.theme(), page.items.get('theme'), page.toggle.title, page.toggle.dataset.theme],
    ['light', 'light', 'Theme: Light (click for Dark)', 'light']);
  page.click();
  assert.deepStrictEqual([page.theme(), page.items.get('theme'), page.toggle.dataset.theme], ['dark', 'dark', 'dark']);
  page.click();
  assert.equal(page.items.get('theme'), 'auto');
  assert.equal(page.theme(), 'dark', 'auto again: Firefox reports dark');
});

test('the toggle still works when the choice can\'t be saved', () => {
  const page = loadTheme({ storageFails: true });
  page.click();
  assert.equal(page.theme(), 'light');
  page.click();
  assert.equal(page.theme(), 'dark');
});
