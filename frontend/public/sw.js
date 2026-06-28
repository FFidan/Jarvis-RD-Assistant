/*
 * JARVIS RD Assistant — Service Worker (offline / PWA foundation)
 *
 * Plain JS, served verbatim from /public (no bundler — cannot import the TS
 * classifier). The cacheability policy below is a HAND-MIRROR of
 * `frontend/src/lib/sw-cache-policy.ts` (which IS unit-tested). Keep the two
 * in sync: same safelist, same NON-GOAL denylist.
 *
 * Strategy:
 *   - App shell: cache-first for same-origin hashed static assets; a
 *     navigation fallback (cached index document) so reloads work offline.
 *     Build assets are content-hashed, so we cache them opportunistically on
 *     first fetch instead of hard-coding a manifest of hashed names.
 *   - Read API surfaces (SAFELIST, GET only): stale-while-revalidate — serve
 *     cached immediately, refresh in the background when online.
 *   - NON-GOAL endpoints (RAG/chat, discovery/process/embed, streams,
 *     mutations, exports): network-only passthrough, never touched.
 *   - JARVIS_LOGOUT message: purge the runtime API cache (cross-user hygiene),
 *     fulfilling the previously no-op postMessage contract in auth-store.ts.
 *
 * Update handling: a new SW installs and waits; `skipWaiting()` is called on
 * an explicit SKIP_WAITING message (P1d may wire an "update available" prompt).
 * On `activate` we `clients.claim()` and delete stale cache versions so users
 * are never trapped on a stale SW or stale precache.
 */

const CACHE_VERSION = 'v1';
const SHELL_CACHE = `jarvis-shell-${CACHE_VERSION}`;
const RUNTIME_API_CACHE = `jarvis-api-${CACHE_VERSION}`;
const NAV_FALLBACK = '/index.html';

const OWNED_CACHES = [SHELL_CACHE, RUNTIME_API_CACHE];

/* ---- cacheability policy (mirror of sw-cache-policy.ts) ---------------- */

const SAFELIST = [
  /^\/api\/papers\/?(\?.*)?$/,
  /^\/api\/papers\/brief(\?.*)?$/,
  /^\/api\/papers\/feed(\?.*)?$/,
  /^\/api\/papers\/feed\/counts(\?.*)?$/,
  /^\/api\/papers\/\d+(\?.*)?$/,
  /^\/api\/papers\/\d+\/notes(\?.*)?$/,
  /^\/api\/extractions\/table(\?.*)?$/,
  /^\/api\/dashboard\/metrics(\?.*)?$/,
  /^\/api\/stats(\?.*)?$/,
];

const DENYLIST = [
  /\/ask(\/|\b)/,
  /\/chat(\/|\b)/,
  /\/api\/papers\/\d+\/analyze\b/,
  /\/api\/discover\b/,
  /\/api\/generate\b/,
  /\/api\/summarize\b/,
  /\/api\/process[-_]/,
  /\/api\/papers\/(batch-process|process_batch|batch-summarize)\b/,
  /\/api\/extract-entities\b/,
  /\/api\/extractions\/batch\b/,
  /\/(embed|embedding|reembed)\b/,
  /\/api\/contradictions\b/,
  /\/stream\b/,
  /\/api\/jobs\/[^/]+\/stream\b/,
  /\/api\/export\//,
  /\/api\/download-pdf\//,
  /\/api\/upload-pdf\b/,
  /\/api\/snapshots\//,
  /\/api\/auth\//,
];

function isCacheableApiRequest(method, pathname, search) {
  if (method !== 'GET') return false;
  if (!pathname.startsWith('/api/')) return false;
  const pathWithSearch = pathname + (search || '');
  for (const deny of DENYLIST) {
    if (deny.test(pathname) || deny.test(pathWithSearch)) return false;
  }
  for (const allow of SAFELIST) {
    if (allow.test(pathname) || allow.test(pathWithSearch)) return true;
  }
  return false;
}

/* ---- lifecycle -------------------------------------------------------- */

self.addEventListener('install', (event) => {
  // Warm the navigation fallback so a cold offline reload still boots the
  // shell. Hashed assets are cached lazily on first fetch.
  event.waitUntil(
    caches
      .open(SHELL_CACHE)
      .then((cache) => cache.add(NAV_FALLBACK).catch(() => undefined)),
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    (async () => {
      const keys = await caches.keys();
      await Promise.all(
        keys
          .filter((k) => k.startsWith('jarvis-') && !OWNED_CACHES.includes(k))
          .map((k) => caches.delete(k)),
      );
      // Cross-user hygiene on shared devices: every SW activation starts with a
      // clean private-API cache. Stale-versioned caches are dropped above; this
      // additionally drops the CURRENT-version runtime API cache so a new SW
      // start (e.g. after a reinstall on the same SW version) cannot serve the
      // previous user's cached API responses. The shell cache is unaffected
      // (only hashed/public static assets — no per-user data).
      await caches.delete(RUNTIME_API_CACHE);
      await self.clients.claim();
    })(),
  );
});

self.addEventListener('message', (event) => {
  // Reject messages from foreign origins. Same-origin postMessage from the
  // app (navigator.serviceWorker.controller.postMessage) may arrive with
  // event.origin === '' in some browsers, so we only block when origin is
  // truthy AND doesn't match ours — leaving the legitimate same-origin path
  // unaffected.
  if (event.origin && event.origin !== self.location.origin) return;

  const data = event.data || {};
  if (data.type === 'SKIP_WAITING') {
    self.skipWaiting();
    return;
  }
  if (data.type === 'JARVIS_LOGOUT') {
    // Cross-user data hygiene: drop all cached API responses so the next user
    // never sees the previous user's data. If the caller supplied a reply port
    // (MessageChannel), acknowledge once the delete settles so the app can wait
    // for a clean cache before exposing the next identity.
    const replyPort = event.ports && event.ports[0];
    const cleared = caches.delete(RUNTIME_API_CACHE).then(
      () => replyPort && replyPort.postMessage({ type: 'JARVIS_LOGOUT_DONE' }),
      () => replyPort && replyPort.postMessage({ type: 'JARVIS_LOGOUT_DONE' }),
    );
    event.waitUntil(cleared);
  }
});

/* ---- fetch routing ---------------------------------------------------- */

self.addEventListener('fetch', (event) => {
  const req = event.request;
  let url;
  try {
    url = new URL(req.url);
  } catch {
    return;
  }

  const sameOrigin = url.origin === self.location.origin;

  // Navigation requests: network-first, fall back to cached shell offline so
  // the SPA still boots and renders last-known-good content.
  if (req.mode === 'navigate') {
    event.respondWith(
      fetch(req).catch(async () => {
        const cache = await caches.open(SHELL_CACHE);
        return (
          (await cache.match(NAV_FALLBACK)) ||
          (await cache.match('/')) ||
          Response.error()
        );
      }),
    );
    return;
  }

  if (!sameOrigin) return; // let cross-origin (fonts/CDN) pass through

  // Same-origin static assets (hashed JS/CSS/img/fonts): cache-first.
  if (
    req.method === 'GET' &&
    !url.pathname.startsWith('/api/') &&
    /\.(?:js|css|woff2?|ttf|otf|png|jpe?g|svg|webp|ico|json|webmanifest)$/.test(
      url.pathname,
    )
  ) {
    event.respondWith(
      caches.open(SHELL_CACHE).then(async (cache) => {
        const hit = await cache.match(req);
        if (hit) return hit;
        try {
          const res = await fetch(req);
          if (res && res.ok) cache.put(req, res.clone());
          return res;
        } catch {
          return hit || Response.error();
        }
      }),
    );
    return;
  }

  // Read API surfaces: stale-while-revalidate (GET + safelist only).
  if (isCacheableApiRequest(req.method, url.pathname, url.search)) {
    event.respondWith(
      caches.open(RUNTIME_API_CACHE).then(async (cache) => {
        const cached = await cache.match(req);
        const network = fetch(req)
          .then((res) => {
            if (res && res.ok) cache.put(req, res.clone());
            return res;
          })
          .catch(() => undefined);
        // Serve cached immediately if present; otherwise wait for network.
        if (cached) {
          event.waitUntil(network);
          return cached;
        }
        const res = await network;
        return res || cached || Response.error();
      }),
    );
    return;
  }

  // Everything else (NON-GOAL endpoints, mutations, streams): passthrough.
});
