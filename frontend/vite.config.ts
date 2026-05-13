/// <reference types="vitest/config" />
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
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
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (id.includes('node_modules/cytoscape')) return 'vendor-cytoscape';
          // Recharts and its transitive deps (d3-*, victory-vendor, react-smooth,
          // react-is, decimal.js-light, internmap, robust-predicates, delaunator)
          // are only used by lazy-loaded routes. Bucketing them all into one
          // chunk keeps the main bundle free of recharts code.
          if (
            id.includes('node_modules/recharts') ||
            id.includes('node_modules/react-smooth') ||
            id.includes('node_modules/victory-vendor') ||
            id.includes('node_modules/d3-') ||
            id.includes('node_modules/decimal.js-light') ||
            id.includes('node_modules/internmap') ||
            id.includes('node_modules/robust-predicates') ||
            id.includes('node_modules/delaunator')
          ) {
            return 'vendor-recharts';
          }
          if (
            id.includes('node_modules/react-markdown') ||
            id.includes('node_modules/rehype') ||
            id.includes('node_modules/remark') ||
            id.includes('node_modules/unified') ||
            id.includes('node_modules/mdast') ||
            id.includes('node_modules/hast') ||
            id.includes('node_modules/micromark') ||
            id.includes('node_modules/vfile') ||
            id.includes('node_modules/unist') ||
            id.includes('node_modules/katex') ||
            id.includes('node_modules/property-information') ||
            id.includes('node_modules/character-entities') ||
            id.includes('node_modules/decode-named-character-reference') ||
            id.includes('node_modules/longest-streak') ||
            id.includes('node_modules/zwitch') ||
            id.includes('node_modules/space-separated-tokens') ||
            id.includes('node_modules/comma-separated-tokens')
          ) {
            return 'vendor-markdown';
          }
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
  },
});
