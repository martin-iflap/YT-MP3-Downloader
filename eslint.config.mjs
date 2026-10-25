// ESLint flat config: `npm run lint`.
import js from '@eslint/js';
import globals from 'globals';

export default [
  {
    // legacy/ is reference code, the rest is not ours or not JS we ship
    ignores: ['legacy/**', '.venv/**', 'dist/**', 'build/**', 'packaging/installer/output/**']
  },
  js.configs.recommended,
  {
    // The extension: plain scripts (no modules) sharing one global scope per page.
    // Cross-file names are declared with /* global */ and /* exported */ comments.
    files: ['extension/**/*.js'],
    languageOptions: {
      ecmaVersion: 'latest',
      sourceType: 'script',
      globals: { ...globals.browser, ...globals.webextensions }
    }
  },
  {
    // The tests: CommonJS, run by Node's test runner
    files: ['tests/**/*.js'],
    languageOptions: {
      ecmaVersion: 'latest',
      sourceType: 'commonjs',
      globals: globals.node
    }
  }
];
