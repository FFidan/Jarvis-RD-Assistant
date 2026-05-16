/**
 * sw-cache-policy — single source of truth for which GET API requests the
 * service worker is allowed to runtime-cache for offline read mode.
 *
 * Wave 3 P1a. Contract reference:
 *   docs/superpowers/specs/2026-05-15-shell-sidebar-admin-ia-redesign-design.md
 *   "Offline / PWA contract — CANONICAL".
 *
 * IMPORTANT: `frontend/public/sw.js` is plain JS served verbatim from /public
 * (no bundler, cannot import TS). The classifier logic here is mirrored by hand
 * in sw.js — this module exists so the policy is *unit-tested* and reviewable.
 * If you change the safelist/denylist here, mirror it in sw.js (and vice-versa).
 *
 * Policy (conservative — default DENY):
 *   - Only same-origin GET requests to a small SAFELIST of read-only,
 *     offline-capable surfaces may be runtime-cached (stale-while-revalidate).
 *   - Everything else is network-only passthrough.
 *   - The DENYLIST is an explicit belt-and-braces guard: NON-GOAL endpoints
 *     (RAG/chat, discovery/fetch/process/embedding, exports, streams) are
 *     rejected even if a future safelist edit would accidentally match them.
 *   - Non-GET methods are never cacheable (caller checks method separately).
 */

/**
 * Offline-capable read surfaces. Matched against the URL pathname.
 * Each entry is a RegExp anchored to `/api/...`.
 *
 * Derived from the canonical per-surface table (Library + Paper Detail are the
 * prime offline targets) and the live api.ts endpoint inventory:
 *   - Paper list / library / feed list + facet counts (read browsing)
 *   - Single paper detail (metadata/abstract/summary/chunks)
 *   - Paper notes (READ — offline note *editing* is an explicit NON-GOAL,
 *     enforced by the GET-only method check at the call site)
 *   - Structured extractions table
 *   - Dashboard metrics / retention stats (glanceable read aggregates)
 *   - Author / project / topic read metadata that the reading surfaces hydrate
 */
const SAFELIST: RegExp[] = [
  // Paper browsing / library list + lightweight brief + per-status feed counts
  /^\/api\/papers\/?(\?.*)?$/,
  /^\/api\/papers\/brief(\?.*)?$/,
  /^\/api\/papers\/feed(\?.*)?$/,
  /^\/api\/papers\/feed\/counts(\?.*)?$/,
  // Single paper detail (metadata / abstract / summary / chunks live here)
  /^\/api\/papers\/\d+(\?.*)?$/,
  // Paper notes — READ ONLY (GET). Editing offline is a NON-GOAL.
  /^\/api\/papers\/\d+\/notes(\?.*)?$/,
  // Structured extractions (read table)
  /^\/api\/extractions\/table(\?.*)?$/,
  // Glanceable read aggregates used by cached reading surfaces
  /^\/api\/dashboard\/metrics(\?.*)?$/,
  /^\/api\/stats(\?.*)?$/,
];

/**
 * Explicit NON-GOAL denylist. These are rejected for runtime caching even if a
 * safelist pattern would otherwise match. Mirrors the canonical
 * "Explicit offline NON-GOALS": chat/RAG, discovery/fetch/process/embedding,
 * anything calling the model layer, plus streams and exports.
 */
const DENYLIST: RegExp[] = [
  // RAG / chat / cross-paper Q&A (incl. per-paper "ask this paper" + analyze)
  /\/ask(\/|\b)/,
  /\/chat(\/|\b)/,
  /\/api\/papers\/\d+\/analyze\b/,
  // Discovery / fetch / process / embedding — the model/pipeline layer
  /\/api\/discover\b/,
  /\/api\/generate\b/,
  /\/api\/summarize\b/,
  /\/api\/process[-_]/,
  /\/api\/papers\/(batch-process|process_batch|batch-summarize)\b/,
  /\/api\/extract-entities\b/,
  /\/api\/extractions\/batch\b/,
  /\/(embed|embedding|reembed)\b/,
  /\/api\/contradictions\b/,
  // Streaming endpoints — never cache a stream
  /\/stream\b/,
  /\/api\/jobs\/[^/]+\/stream\b/,
  // Exports / file downloads / raw assets — not part of read mode
  /\/api\/export\//,
  /\/api\/download-pdf\//,
  /\/api\/upload-pdf\b/,
  /\/api\/snapshots\//,
  // Auth lifecycle — never cache (security)
  /\/api\/auth\//,
];

/**
 * Decide whether a same-origin GET request URL may be runtime-cached for
 * offline read mode.
 *
 * @param method  HTTP method (only `GET` is ever cacheable).
 * @param url     Absolute or relative request URL. Only the pathname + search
 *                are inspected; origin is the caller's concern (SW restricts to
 *                same-origin before calling this).
 * @returns `true` if the response may be runtime-cached (SWR), else `false`.
 */
export function isCacheableApiRequest(method: string, url: string): boolean {
  if (method.toUpperCase() !== 'GET') {
    return false;
  }

  let pathname: string;
  let search = '';
  try {
    const u = new URL(url, 'http://localhost');
    pathname = u.pathname;
    search = u.search;
  } catch {
    return false;
  }

  // Only /api/ traffic participates in the runtime API cache. The app shell
  // (HTML/JS/CSS) is handled by a separate static cache-first strategy.
  if (!pathname.startsWith('/api/')) {
    return false;
  }

  const pathWithSearch = pathname + search;

  // Belt-and-braces: any NON-GOAL match disqualifies regardless of safelist.
  for (const deny of DENYLIST) {
    if (deny.test(pathname) || deny.test(pathWithSearch)) {
      return false;
    }
  }

  // Default DENY: must positively match an offline-capable read surface.
  for (const allow of SAFELIST) {
    if (allow.test(pathname) || allow.test(pathWithSearch)) {
      return true;
    }
  }

  return false;
}

/** Exposed for tests / sw.js parity audits. Do not mutate. */
export const __SW_CACHE_SAFELIST = SAFELIST;
export const __SW_CACHE_DENYLIST = DENYLIST;
