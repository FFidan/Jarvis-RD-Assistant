import { useEffect, useRef } from 'react';
import type { SurfaceView } from '@/types';

interface FeedShortcutCallbacks {
  onNext?: () => void;           // j
  onPrev?: () => void;           // k
  onSave?: () => void;           // s (Inbox surface)
  onStar?: () => void;           // s (Library surface — surface-aware!)
  onSaveAndStar?: () => void;    // S (Inbox only)
  onArchive?: () => void;        // e (Library only)
  onDismiss?: () => void;        // d
  onMarkRead?: () => void;       // r
  onOpen?: () => void;           // o or Enter
  onCheatSheet?: () => void;     // ?
  onClearSelection?: () => void; // Esc
}

export function useFeedKeyboardShortcuts(
  surface: SurfaceView,
  callbacks: FeedShortcutCallbacks,
) {
  // Keep refs up-to-date every render so the stable handler always reads the
  // latest values without needing to re-register the listener.
  const surfaceRef = useRef(surface);
  const callbacksRef = useRef(callbacks);

  useEffect(() => {
    surfaceRef.current = surface;
  });

  useEffect(() => {
    callbacksRef.current = callbacks;
  });

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
      if (e.metaKey || e.ctrlKey || e.altKey) return;

      const cb = callbacksRef.current;
      const surf = surfaceRef.current;

      switch (e.key) {
        case 'j':
          cb.onNext?.();
          break;
        case 'k':
          cb.onPrev?.();
          break;
        case 's':
          if (e.shiftKey) {
            cb.onSaveAndStar?.();
          } else if (surf === 'inbox') {
            cb.onSave?.();
          } else {
            cb.onStar?.();
          }
          break;
        case 'e':
          if (surf === 'library' || surf === 'starred' || surf === 'reading') {
            cb.onArchive?.();
          }
          break;
        case 'd':
          cb.onDismiss?.();
          break;
        case 'r':
          cb.onMarkRead?.();
          break;
        case 'o':
        case 'Enter':
          cb.onOpen?.();
          break;
        case '?':
          cb.onCheatSheet?.();
          break;
        case 'Escape':
          cb.onClearSelection?.();
          break;
      }
    };

    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, []); // mount-once — surface and callbacks are read via refs
}
