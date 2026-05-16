/**
 * pwa — service worker registration + install affordance (Wave 3 P1a).
 *
 * Contract reference:
 *   docs/superpowers/specs/2026-05-15-shell-sidebar-admin-ia-redesign-design.md
 *   "Offline / PWA contract — CANONICAL" §1, §2, §7.
 *
 * Responsibilities (foundation only — UI lives elsewhere):
 *   - Register `/sw.js` after load (dev-safe: skipped in DEV / non-secure ctx).
 *   - Capture `beforeinstallprompt` and expose `promptInstall()` so P1d can
 *     wire a discreet "Install for offline reading" button (the button UI is
 *     P1d's concern — this module only exposes the capability + state).
 *
 * Public API (stable — P1d depends on this):
 *   - `registerServiceWorker()` — idempotent; call once from the entry.
 *   - `canInstall()` — `true` once a deferred install prompt is available.
 *   - `promptInstall()` — triggers the native prompt; resolves to the user
 *     choice outcome ('accepted' | 'dismissed' | 'unavailable').
 *   - `onInstallAvailabilityChange(cb)` — subscribe to availability changes
 *     (returns an unsubscribe fn). Lets P1d reactively show/hide its button.
 */

interface BeforeInstallPromptEvent extends Event {
  prompt: () => Promise<void>;
  userChoice: Promise<{ outcome: 'accepted' | 'dismissed' }>;
}

let deferredPrompt: BeforeInstallPromptEvent | null = null;
const availabilityListeners = new Set<(canInstall: boolean) => void>();

function emitAvailability(): void {
  const value = deferredPrompt !== null;
  for (const cb of availabilityListeners) {
    try {
      cb(value);
    } catch {
      /* a listener throwing must not break others */
    }
  }
}

/** `true` once the browser has offered a deferred install prompt. */
export function canInstall(): boolean {
  return deferredPrompt !== null;
}

/**
 * Subscribe to install-availability changes.
 * @returns unsubscribe function.
 */
export function onInstallAvailabilityChange(
  cb: (canInstall: boolean) => void,
): () => void {
  availabilityListeners.add(cb);
  return () => availabilityListeners.delete(cb);
}

/**
 * Trigger the native install prompt (must be called from a user gesture).
 * @returns the user's choice, or 'unavailable' if no prompt is pending.
 */
export async function promptInstall(): Promise<
  'accepted' | 'dismissed' | 'unavailable'
> {
  if (!deferredPrompt) return 'unavailable';
  const evt = deferredPrompt;
  deferredPrompt = null;
  emitAvailability();
  try {
    await evt.prompt();
    const choice = await evt.userChoice;
    return choice.outcome;
  } catch {
    return 'dismissed';
  }
}

let listenersWired = false;

function wireInstallListeners(): void {
  if (listenersWired || typeof window === 'undefined') return;
  listenersWired = true;

  window.addEventListener('beforeinstallprompt', (e: Event) => {
    // Suppress the mini-infobar; we surface our own affordance (P1d).
    e.preventDefault();
    deferredPrompt = e as BeforeInstallPromptEvent;
    emitAvailability();
  });

  window.addEventListener('appinstalled', () => {
    deferredPrompt = null;
    emitAvailability();
  });
}

let registered = false;

/**
 * Register the service worker. Idempotent — safe to call once from the entry.
 * No-ops when SW is unsupported, in dev (HMR + SW caching fight), or in an
 * insecure context.
 */
export function registerServiceWorker(): void {
  wireInstallListeners();

  if (registered) return;
  registered = true;

  if (typeof navigator === 'undefined' || !('serviceWorker' in navigator)) {
    return;
  }
  // Vite injects import.meta.env.DEV. Skip SW in dev so HMR isn't shadowed by
  // a stale precache; the SW + manifest still ship to dist/ for prod.
  const isDev =
    typeof import.meta !== 'undefined' && Boolean(import.meta.env?.DEV);
  if (isDev) return;

  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch((err: unknown) => {
      // Registration failure must never break the app — log and move on.
      console.warn('[pwa] service worker registration failed', err);
    });
  });
}
