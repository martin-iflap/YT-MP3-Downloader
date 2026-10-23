// Loads extension scripts into a sandbox with a fake `browser` global, the same way
// Firefox loads them as plain scripts (no modules, shared global scope).
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const EXTENSION_DIR = path.join(__dirname, '..', '..', 'extension');

// Objects created inside the sandbox have foreign prototypes; normalise for assert.deepStrictEqual.
const plain = value => JSON.parse(JSON.stringify(value));

function createEvent() {
  const listeners = [];
  return {
    addListener: fn => listeners.push(fn),
    fire: (...args) => listeners.map(fn => fn(...args))
  };
}

// nativeHost, if given, is called with each posted request and the port, to answer it
// right away (see host_status.test.js); otherwise tests answer through state.ports.
function createFakePort(name, nativeHost) {
  const port = {
    name,
    posted: [],
    disconnected: false,
    error: null,
    onMessage: createEvent(),
    onDisconnect: createEvent(),
    postMessage(message) {
      port.posted.push(plain(message));
      if (nativeHost) nativeHost(plain(message), port);
    },
    disconnect() { port.disconnected = true; },
    // Host-side helpers
    reply(message) { port.onMessage.fire(message); },
    exit(errorMessage) {
      port.error = errorMessage ? { message: errorMessage } : null;
      port.onDisconnect.fire(port);
    }
  };
  return port;
}

function createFakeBrowser({ sidebarOpen = false, storage = {}, nativeHost = null } = {}) {
  const state = { sidebarOpen, storage: { ...storage }, sent: [], notifications: [], menus: [], ports: [] };

  const browser = {
    runtime: {
      lastError: null,
      onMessage: createEvent(),
      sendMessage: async (message) => { state.sent.push(plain(message)); },
      connectNative: (name) => {
        const port = createFakePort(name, nativeHost);
        state.ports.push(port);
        return port;
      }
    },
    contextMenus: {
      onClicked: createEvent(),
      create: (props, callback) => {
        state.menus.push(plain(props));
        if (callback) callback();
      }
    },
    notifications: {
      onClicked: createEvent(),
      onClosed: createEvent(),
      create: async (options) => {
        state.notifications.push(plain(options));
        return `notification-${state.notifications.length}`;
      }
    },
    sidebarAction: {
      isOpen: async () => state.sidebarOpen
    },
    storage: {
      local: {
        get: async key => (key in state.storage ? { [key]: state.storage[key] } : {})
      }
    }
  };

  return { browser, state };
}

function loadScripts(files, globals) {
  const context = vm.createContext({
    console,
    URL,
    crypto,
    // Wrapped so node:test mock timers (which patch the real globals) apply inside the sandbox
    setTimeout: (...args) => setTimeout(...args),
    clearTimeout: handle => clearTimeout(handle),
    ...globals
  });
  for (const file of files) {
    const filename = path.join(EXTENSION_DIR, file);
    vm.runInContext(fs.readFileSync(filename, 'utf8'), context, { filename });
  }
  return context;
}

// context exposes the scripts' top-level functions and constants, e.g. context.parseVersion.
function loadBackground(options) {
  const { browser, state } = createFakeBrowser(options);
  const context = loadScripts(['youtube.js', 'background.js'], { browser });
  return { browser, state, context };
}

// Let pending promise callbacks (async listeners, sendMessage, isOpen) run.
const flush = () => new Promise(resolve => setImmediate(resolve));

module.exports = { loadScripts, loadBackground, flush };
