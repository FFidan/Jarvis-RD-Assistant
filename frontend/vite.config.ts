/// <reference types="vitest/config" />
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import path from 'node:path';
import { readFileSync } from 'node:fs';
import type { IncomingMessage, ServerResponse } from 'node:http';
import { visualizer } from 'rollup-plugin-visualizer';

// Build-time app version, exposed to the client as `__APP_VERSION__` (see the
// ambient declaration in src/vite-env.d.ts). Prefer the npm-injected env var
// (set for every `npm run <script>` invocation, incl. `npm run build`); fall
// back to reading package.json directly for direct `vite`/`vitest` binary
// invocations where that env var isn't populated.
function readPackageVersion(): string {
  const pkgJson: { version: string } = JSON.parse(
    readFileSync(new URL('./package.json', import.meta.url), 'utf-8'),
  );
  return pkgJson.version;
}

const appVersion = process.env.npm_package_version ?? readPackageVersion();

const paperIngestionPort = process.env.PAPER_INGESTION_HOST_PORT || '8010';
const learningEnginePort = process.env.LEARNING_ENGINE_HOST_PORT || '8011';
const paperIngestionBase = `http://localhost:${paperIngestionPort}`;
const learningEngineBase = `http://localhost:${learningEnginePort}`;
const analyzeBundle = process.env.ANALYZE_BUNDLE === 'true';
const mockE2EHealth = process.env.JARVIS_E2E_MOCK_HEALTH === 'true';

function writeMockHealthResponse(
  req: IncomingMessage,
  res: ServerResponse,
  next: () => void,
): void {
  const pathName = new URL(req.url ?? '/', 'http://localhost').pathname;
  const isHealthPath = pathName.startsWith('/health/paper_ingestion')
    || pathName.startsWith('/health/learning_engine');
  if (!isHealthPath) {
    next();
    return;
  }

  const body = pathName.endsWith('/internal')
    ? {
        status: 'ok',
        checks: {
          postgres: 'ok',
          qdrant: 'ok',
          litellm: 'ok',
          ollama: 'ok',
          vector: 'ok',
        },
      }
    : { status: 'ok' };

  res.statusCode = 200;
  res.setHeader('Content-Type', 'application/json');
  res.end(JSON.stringify(body));
}

export default defineConfig({
  plugins: [
    mockE2EHealth
      ? {
          name: 'jarvis-mocked-health',
          configureServer(server) {
            server.middlewares.use(writeMockHealthResponse);
          },
          configurePreviewServer(server) {
            server.middlewares.use(writeMockHealthResponse);
          },
        }
      : null,
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
  define: {
    __APP_VERSION__: JSON.stringify(appVersion),
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
      '/health/paper_ingestion/internal': {
        target: paperIngestionBase,
        rewrite: () => '/health/internal',
      },
      '/health/paper_ingestion/live': {
        target: paperIngestionBase,
        rewrite: () => '/health/live',
      },
      '/health/paper_ingestion': {
        target: paperIngestionBase,
        rewrite: () => '/health',
      },
      '/health/learning_engine/internal': {
        target: learningEngineBase,
        rewrite: () => '/health/internal',
      },
      '/health/learning_engine/live': {
        target: learningEngineBase,
        rewrite: () => '/health/live',
      },
      '/health/learning_engine': {
        target: learningEngineBase,
        rewrite: () => '/health',
      },
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
    // Reset all mocks before every test: restores `vi.fn(impl)` implementations
    // and drains `mock*Once` queues so no test inherits a sibling's leftover
    // queued values. Note `clearMocks` would NOT drain those queues — only
    // `mockReset` does. Mock defaults must therefore be written as
    // `vi.fn(impl)` (survives reset), never `vi.fn().mockResolvedValue(...)`
    // (wiped to undefined by reset).
    mockReset: true,
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
