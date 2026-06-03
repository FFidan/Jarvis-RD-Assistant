import js from '@eslint/js';
import tseslint from 'typescript-eslint';
import reactPlugin from 'eslint-plugin-react';
import reactHooks from 'eslint-plugin-react-hooks';
import jsxA11y from 'eslint-plugin-jsx-a11y';

export default tseslint.config(
  js.configs.recommended,
  tseslint.configs.recommended,
  reactPlugin.configs.flat.recommended,
  {
    plugins: { 'react-hooks': reactHooks },
    rules: { ...reactHooks.configs.recommended.rules },
  },
  jsxA11y.flatConfigs.recommended,
  {
    settings: {
      react: { version: 'detect' },
    },
  },
  // Global rule overrides — downgrade noisy rules to warnings so CI can
  // block on errors only; individual files can be tightened incrementally.
  {
    rules: {
      'react/react-in-jsx-scope': 'off',
      'react/jsx-uses-react': 'off',
      'react/prop-types': 'off',
      'react/no-unescaped-entities': 'warn',
      'jsx-a11y/no-static-element-interactions': 'warn',
      'jsx-a11y/click-events-have-key-events': 'warn',
      'jsx-a11y/no-noninteractive-element-interactions': 'warn',
      'jsx-a11y/label-has-associated-control': 'warn',
      'jsx-a11y/no-autofocus': 'warn',
      'jsx-a11y/interactive-supports-focus': 'warn',
      'jsx-a11y/role-has-required-aria-props': 'warn',
      'jsx-a11y/heading-has-content': 'warn',
      '@typescript-eslint/no-explicit-any': 'warn',
      '@typescript-eslint/no-unused-vars': 'warn',
      '@typescript-eslint/no-empty-object-type': 'warn',
      '@typescript-eslint/consistent-type-assertions': [
        'warn',
        { assertionStyle: 'never' },
      ],
      'react-hooks/rules-of-hooks': 'error',
      'no-useless-escape': 'warn',
      'require-yield': 'warn',
      'prefer-const': 'warn',
      '@typescript-eslint/prefer-as-const': 'warn',
    },
  },
  {
    files: ['src/**/*.{ts,tsx}'],
    languageOptions: {
      parserOptions: {
        projectService: true,
        tsconfigRootDir: import.meta.dirname,
      },
    },
    rules: {
      'react/jsx-no-target-blank': [
        'error',
        { allowReferrer: false, enforceDynamicLinks: 'always' },
      ],
    },
  },
  // Browser scripts in public/ — provide browser globals
  {
    files: ['public/**/*.js'],
    languageOptions: {
      globals: {
        window: 'readonly',
        document: 'readonly',
        localStorage: 'readonly',
      },
    },
    rules: {
      'no-undef': 'off',
      'no-empty': 'warn',
      '@typescript-eslint/no-unused-vars': 'off',
    },
  },
  // E2E test files — relax rules that don't apply to test helpers
  {
    files: ['e2e/**/*.{ts,js}'],
    rules: {
      '@typescript-eslint/no-unused-vars': 'off',
      'prefer-const': 'warn',
      '@typescript-eslint/prefer-as-const': 'warn',
    },
  },
  {
    ignores: [
      'dist/**',
      'build/**',
      'node_modules/**',
      'playwright-report/**',
      'test-results/**',
    ],
  },
);
