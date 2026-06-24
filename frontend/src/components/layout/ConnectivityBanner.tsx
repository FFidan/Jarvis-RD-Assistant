/**
 * ConnectivityBanner — slim app-shell banner shown when the browser reports
 * offline connectivity.
 *
 * Contract reference:
 *   internal design spec (archived)
 *   "Offline / PWA contract — CANONICAL" §6 (global connectivity banner) + §7
 *   (install affordance).
 *
 * Behaviour:
 *   - Offline: banner renders with "offline — showing saved data" copy.
 *   - Online:  banner is not rendered (zero DOM footprint; ONLINE rendering
 *              unchanged).
 *   - Install: a separate, dismissible "Install app" prompt renders only when
 *              the browser has deferred a BeforeInstallPromptEvent (P1a
 *              canInstall() / promptInstall() / onInstallAvailabilityChange).
 *              It is unobtrusive and sits directly below the banner area.
 *
 * Intentionally no animation/transition: the banner is a factual indicator, not
 * a notification. Keeping it static avoids layout shift on the reading surfaces.
 */
import { useState, useEffect, useCallback } from 'react';
import { WifiOff, Download, X } from 'lucide-react';
import { useOnlineStatus } from '@/hooks/use-online-status';
import { canInstall, promptInstall, onInstallAvailabilityChange } from '@/lib/pwa';
import { cn } from '@/lib/utils';

// ---------------------------------------------------------------------------
// Offline banner
// ---------------------------------------------------------------------------

/** Offline indicator banner — hidden when online. */
function OfflineBanner() {
  const { online } = useOnlineStatus();

  if (online) return null;

  return (
    <div
      role="status"
      aria-live="polite"
      data-testid="connectivity-banner-offline"
      className={cn(
        'flex items-center gap-2 px-4 py-1.5 text-xs font-medium',
        'bg-amber-50 text-amber-900 border-b border-amber-200',
        'dark:bg-amber-950/40 dark:text-amber-200 dark:border-amber-800',
      )}
    >
      <WifiOff className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
      <span>
        You&apos;re offline &mdash; showing saved data. Some actions are unavailable.
      </span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Install affordance
// ---------------------------------------------------------------------------

const INSTALL_BANNER_KEY = 'jarvis.install-banner-dismissed';

/** Dismissible "Install app" prompt driven by P1a pwa.ts. */
function InstallAffordance() {
  const [installable, setInstallable] = useState<boolean>(canInstall);
  const [dismissed, setDismissed] = useState(() => {
    try {
      return localStorage.getItem(INSTALL_BANNER_KEY) === '1';
    } catch {
      return false;
    }
  });

  useEffect(() => {
    // Subscribe to availability changes (canInstall fires after beforeinstallprompt).
    const unsub = onInstallAvailabilityChange(setInstallable);
    return unsub;
  }, []);

  const handleInstall = useCallback(async () => {
    await promptInstall();
    // promptInstall clears deferredPrompt and fires onInstallAvailabilityChange;
    // the subscription above will set installable → false automatically. The
    // 'appinstalled' event also fires the change, so no manual clear needed.
  }, []);

  const handleDismiss = useCallback(() => {
    try {
      localStorage.setItem(INSTALL_BANNER_KEY, '1');
    } catch {
      // Safari private mode — proceed without persistence
    }
    setDismissed(true);
  }, []);

  if (!installable || dismissed) return null;

  return (
    <div
      data-testid="install-affordance"
      className={cn(
        'flex items-center gap-2 px-4 py-1.5 text-xs',
        'bg-muted/60 border-b border-hair',
      )}
    >
      <Download className="h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
      <span className="flex-1 text-muted-foreground">
        Install JARVIS for offline reading
      </span>
      <button
        onClick={handleInstall}
        data-testid="install-affordance-button"
        className={cn(
          'rounded px-2 py-0.5 text-xs font-medium',
          'bg-primary text-primary-foreground hover:bg-primary/90',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
          'transition-colors',
        )}
      >
        Install
      </button>
      <button
        onClick={handleDismiss}
        data-testid="install-affordance-dismiss"
        aria-label="Dismiss install prompt"
        className={cn(
          'rounded p-0.5 text-muted-foreground hover:text-foreground',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
          'transition-colors',
        )}
      >
        <X className="h-3.5 w-3.5" aria-hidden="true" />
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Public export — composed banner area
// ---------------------------------------------------------------------------

/**
 * ConnectivityBanner — renders the offline banner + install affordance.
 * Mount this once at the top of AppShell's main content area.
 * Both sub-components self-suppress when they have nothing to show, so the
 * composed output is zero-height in normal online, non-installable state.
 */
export function ConnectivityBanner() {
  return (
    <>
      <OfflineBanner />
      <InstallAffordance />
    </>
  );
}
