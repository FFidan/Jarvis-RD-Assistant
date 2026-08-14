import { useEffect, useRef } from 'react';
import { useCommandPalette } from '@/stores/command-palette-store';
import { fetchFeedPapers } from '@/lib/api';
import type { SearchPreviewResult } from '@/types';

const DEBOUNCE_MS = 250;
const SEARCH_TIMEOUT_MS = 8_000;
const PALETTE_RESULT_LIMIT = 8;

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
 *  2. Debounce the query (~250ms) and search the paper feed, writing results /
 *     loading / error flags into the store. Network or server failure surfaces
 *     as a friendly error state (never throws).
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
        // The box is labelled as searching YOUR papers, so it searches the
        // library feed, not external sources. External discovery lives in
        // Discover, reachable from the palette footer. Results map into the
        // palette's existing shape; every hit is by definition a library match.
        const response = await Promise.race([
          fetchFeedPapers({ q: trimmed, limit: PALETTE_RESULT_LIMIT }),
          timeout,
        ]);
        if (cancelled) return;
        const mapped: SearchPreviewResult[] = response.papers.map((p) => ({
          source_type: p.source_type,
          external_id: p.external_id,
          title: p.title,
          authors: p.authors,
          abstract: p.abstract ?? null,
          published_date: p.published_date ?? null,
          url: p.url ?? '',
          pdf_url: null,
          citation_count: p.citation_count ?? 0,
          metadata: {},
          library_match: { paper_id: p.id, has_project_links: false, zotero_item_key: null },
        }));
        setResults(mapped);
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
