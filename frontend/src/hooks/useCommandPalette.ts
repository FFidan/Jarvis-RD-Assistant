import { useEffect, useRef } from 'react';
import { useCommandPalette } from '@/stores/command-palette-store';
import { searchPreview } from '@/lib/api';

const DEBOUNCE_MS = 250;
const SEARCH_TIMEOUT_MS = 8_000;

/**
 * Controller hook for the global ⌘K command palette.
 *
 * Responsibilities:
 *  1. Register exactly ONE window `keydown` listener (mount-once; state is
 *     read via the store inside the handler so there is no stale closure).
 *     - ⌘K / Ctrl+K toggles the palette open/closed — and is honoured even
 *       when focus is in an input/textarea/contenteditable, so the palette
 *       is reachable from anywhere.
 *     - Esc closes it.
 *     - All other typing is left alone.
 *  2. Debounce the query (~250ms) and call the existing searchPreview API,
 *     writing results / loading / error flags into the store. Network or
 *     server failure surfaces as a friendly error state (never throws).
 *
 * Mounted once by CommandPaletteSearch (always present in the TopBar on
 * authed pages), so ⌘K works app-wide without touching AppShell.
 */
export function useCommandPaletteController() {
  const toggle = useCommandPalette((s) => s.toggle);
  const close = useCommandPalette((s) => s.close);
  const isOpen = useCommandPalette((s) => s.isOpen);
  const query = useCommandPalette((s) => s.query);
  const setLoading = useCommandPalette((s) => s.setLoading);
  const setResults = useCommandPalette((s) => s.setResults);
  const setErrored = useCommandPalette((s) => s.setErrored);

  // Refs so the mount-once keydown handler never reads a stale callback.
  const toggleRef = useRef(toggle);
  toggleRef.current = toggle;
  const closeRef = useRef(close);
  closeRef.current = close;
  const isOpenRef = useRef(isOpen);
  isOpenRef.current = isOpen;

  // ── Global keyboard shortcut (registered exactly once) ──────────────────
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      // ⌘K / Ctrl+K toggles — allowed even while typing in a field so the
      // palette is always reachable.
      if ((e.metaKey || e.ctrlKey) && (e.key === 'k' || e.key === 'K')) {
        e.preventDefault();
        toggleRef.current();
        return;
      }

      if (e.key === 'Escape' && isOpenRef.current) {
        e.preventDefault();
        closeRef.current();
      }
    };

    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, []); // mount-once — actions/open-state read via refs

  // ── Debounced search ────────────────────────────────────────────────────
  useEffect(() => {
    const trimmed = query.trim();

    if (!isOpen || trimmed.length === 0) {
      setResults([]);
      setLoading(false);
      setErrored(false);
      return;
    }

    setLoading(true);
    setErrored(false);

    let cancelled = false;
    const timer = setTimeout(async () => {
      const timeout = new Promise<never>((_, reject) =>
        setTimeout(() => reject(new Error('search_timeout')), SEARCH_TIMEOUT_MS),
      );
      try {
        const response = await Promise.race([searchPreview(trimmed), timeout]);
        if (cancelled) return;
        setResults(response.results);
        setErrored(false);
      } catch {
        if (cancelled) return;
        setResults([]);
        setErrored(true);
      } finally {
        if (!cancelled) setLoading(false);
      }
    }, DEBOUNCE_MS);

    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [query, isOpen, setResults, setLoading, setErrored]);
}
