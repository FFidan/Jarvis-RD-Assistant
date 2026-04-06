/// <reference types="vitest/config" />
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'node:path';

const paperIngestionPort = process.env.PAPER_INGESTION_HOST_PORT || '8010';
const learningEnginePort = process.env.LEARNING_ENGINE_HOST_PORT || '8011';
const paperIngestionBase = `http://localhost:${paperIngestionPort}`;
const learningEngineBase = `http://localhost:${learningEnginePort}`;

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
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
