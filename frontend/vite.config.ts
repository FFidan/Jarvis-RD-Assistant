/// <reference types="vitest/config" />
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import path from 'node:path';
import { visualizer } from 'rollup-plugin-visualizer';

const paperIngestionPort = process.env.PAPER_INGESTION_HOST_PORT || '8010';
const learningEnginePort = process.env.LEARNING_ENGINE_HOST_PORT || '8011';
const paperIngestionBase = `http://localhost:${paperIngestionPort}`;
const learningEngineBase = `http://localhost:${learningEnginePort}`;
const analyzeBundle = process.env.ANALYZE_BUNDLE === 'true';

export default defineConfig({
  plugins: [
    react(),
    tailwindcss(),
    analyzeBundle
      ? visualizer({
          filename: 'dist/bundle-stats.html',
          template: 'treemap',
          gzipSize: true,
          brotliSize: true,
        })
      : null,
  ],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  build: {
    rolldownOptions: {
      output: {
        // Bucket lazy-route-only vendor libraries into stable chunks so the main
        // bundle stays free of recharts/markdown/cytoscape code. Each `test`
        // matches that library family's module paths under node_modules.
        codeSplitting: {
          groups: [
            { name: 'vendor-cytoscape', test: /node_modules\/cytoscape/ },
            {
              // PDF reader stack (Paper Detail only) — keep pdf.js out of the
              // shared/index bundle.
              name: 'vendor-pdf',
              test: /node_modules\/(pdfjs-dist|react-pdf-highlighter-extended|react-rnd)/,
            },
            {
              name: 'vendor-recharts',
              test: /node_modules\/(recharts|react-smooth|victory-vendor|d3-|decimal\.js-light|internmap|robust-predicates|delaunator)/,
            },
            {
              name: 'vendor-markdown',
              test: /node_modules\/(react-markdown|rehype|remark|unified|mdast|hast|micromark|vfile|unist|katex|property-information|character-entities|decode-named-character-reference|longest-streak|zwitch|space-separated-tokens|comma-separated-tokens)/,
            },
          ],
        },
      },
    },
  },
  server: {
    port: 3001,
    proxy: {
      '/api/decks': learningEngineBase,
      '/api/cards': learningEngineBase,
      '/api/review': learningEngineBase,
      '/api/stats': learningEngineBase,
      '/api/generate': learningEngineBase,
      '/api/export': learningEngineBase,
      '/api/projects': learningEngineBase,
      '/api/tasks': learningEngineBase,
      '/api/milestones': learningEngineBase,
      '/api/analytics/activity': learningEngineBase,
      '/api/analytics/retention': learningEngineBase,
      '/api/analytics/reviews': learningEngineBase,
      '/api/analytics/llm-cost': learningEngineBase,
      '/api/executive': learningEngineBase,
      '/api': paperIngestionBase,
    },
  },
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: ['./src/__tests__/setup.ts'],
    exclude: ['e2e/**', 'node_modules/**'],
    // react-pdf-highlighter-extended ships only a `module` field (no main/exports),
    // which vitest's resolver can't resolve from a bare specifier (the Rolldown
    // build resolver can). Tests mock the module anyway, so point the bare
    // specifier at its ESM entry purely to satisfy resolution. Test-scoped — no
    // effect on dev/build.
    alias: {
      'react-pdf-highlighter-extended': path.resolve(
        __dirname,
        'node_modules/react-pdf-highlighter-extended/dist/esm/index.js',
      ),
    },
  },
});
