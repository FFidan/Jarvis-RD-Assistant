import { useEffect, useRef } from 'react';
import type { SurfaceView, LifecycleState } from '@/types';

export interface FeedKeyboardCallbacks {
  onNext?: () => void;
  onPrev?: () => void;
  onSave?: (id: number) => void;
  onSkip?: (id: number) => void;
  onMarkReading?: (id: number) => void;
  onMarkDone?: (id: number) => void;
  onSetAside?: (id: number) => void;  // reading → to_read
  onTrash?: (id: number) => void;
  onStar?: (id: number) => void;
  onUnstar?: (id: number) => void;
  onRestore?: (id: number) => void;
  onSaveAndStar?: (id: number) => void;  // Shift+s (inbox only)
  onOpenDetail?: (id: number) => void;
  onShowCheatSheet?: () => void;
  onClearSelection?: () => void;
  // Legacy callbacks REMOVED: onArchive, onDismiss, onMarkRead, onUnsave
}

export function useFeedKeyboardShortcuts(
  surface: SurfaceView,
  papers: Array<{ id: number; state?: LifecycleState }>,
  selectedIndex: number | null,
  callbacks: FeedKeyboardCallbacks,
) {
  // M17: refs to avoid stale closures — updated every render, listener registered once
  const surfaceRef = useRef(surface);
  surfaceRef.current = surface;
  const papersRef = useRef(papers);
  papersRef.current = papers;
  const selectedIndexRef = useRef(selectedIndex);
  selectedIndexRef.current = selectedIndex;
  const callbacksRef = useRef(callbacks);
  callbacksRef.current = callbacks;

  // Register the listener exactly once on mount; read from refs inside.
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      // Ignore when typing in an input/textarea/contenteditable
      const target = e.target as HTMLElement;
      if (
        target.tagName === 'INPUT' ||
        target.tagName === 'TEXTAREA' ||
        target.isContentEditable
      ) {
        return;
      }

      // Allow modifier-free single-key shortcuts only (avoid intercepting Cmd+K etc.)
      // shiftKey is allowed for Shift+s
      if (e.metaKey || e.ctrlKey || e.altKey) return;

      const surf = surfaceRef.current;
      const prs = papersRef.current;
      const idx = selectedIndexRef.current;
      const cb = callbacksRef.current;
      const current = idx != null ? prs[idx] : null;
      const state: LifecycleState = current?.state ?? 'inbox';

      // Navigation — no selected paper required
      if (e.key === 'j') { cb.onNext?.(); e.preventDefault(); return; }
      if (e.key === 'k') { cb.onPrev?.(); e.preventDefault(); return; }
      if (e.key === '?') { cb.onShowCheatSheet?.(); e.preventDefault(); return; }
      if (e.key === 'Escape') { cb.onClearSelection?.(); return; }

      // Shortcuts below require a selected paper
      if (!current) return;

      if (e.key === 'o' || e.key === 'Enter') {
        cb.onOpenDetail?.(current.id);
        e.preventDefault();
        return;
      }

      // s — state-aware: save (inbox) or star (other non-trash surfaces)
      if (e.key === 's' && !e.shiftKey) {
        if (state === 'inbox') {
          cb.onSave?.(current.id);
        } else if (state !== 'trash') {
          cb.onStar?.(current.id);
        }
        e.preventDefault();
        return;
      }

      // Shift+S — save and star (inbox only)
      if (e.key === 'S' && e.shiftKey) {
        if (state === 'inbox') cb.onSaveAndStar?.(current.id);
        e.preventDefault();
        return;
      }

      // t — trash (any non-trash surface)
      if (e.key === 't') {
        if (state !== 'trash') cb.onTrash?.(current.id);
        e.preventDefault();
        return;
      }

      // e — set aside (reading → to_read; no-op elsewhere)
      if (e.key === 'e') {
        if (state === 'reading') cb.onSetAside?.(current.id);
        e.preventDefault();
        return;
      }

      // r — restore (trash only)
      if (e.key === 'r') {
        if (state === 'trash') cb.onRestore?.(current.id);
        e.preventDefault();
        return;
      }

      // d — done (reading or to_read only)
      if (e.key === 'd') {
        if (state === 'reading' || state === 'to_read') cb.onMarkDone?.(current.id);
        e.preventDefault();
        return;
      }

      void surf; // surf is read via surfaceRef for future surface-specific logic
    };

    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, []); // mount-once — surface, papers, selectedIndex, and callbacks are read via refs
}
