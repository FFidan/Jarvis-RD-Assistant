import { useEffect } from 'react';
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

      switch (e.key) {
        case 'j':
          callbacks.onNext?.();
          break;
        case 'k':
          callbacks.onPrev?.();
          break;
        case 's':
          if (e.shiftKey) {
            callbacks.onSaveAndStar?.();
          } else if (surface === 'inbox') {
            callbacks.onSave?.();
          } else {
            callbacks.onStar?.();
          }
          break;
        case 'e':
          if (surface === 'library' || surface === 'starred' || surface === 'reading') {
            callbacks.onArchive?.();
          }
          break;
        case 'd':
          callbacks.onDismiss?.();
          break;
        case 'r':
          callbacks.onMarkRead?.();
          break;
        case 'o':
        case 'Enter':
          callbacks.onOpen?.();
          break;
        case '?':
          callbacks.onCheatSheet?.();
          break;
        case 'Escape':
          callbacks.onClearSelection?.();
          break;
      }
    };

    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [surface, callbacks]);
}
