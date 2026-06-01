import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { useKeyboardShortcuts } from '@/stores/keyboard-shortcuts-store';

const SHORTCUTS: { key: string; action: string; note?: string }[] = [
  { key: 'j / k', action: 'Next / previous paper' },
  { key: 's', action: 'Save (Inbox) / Star (other surfaces)', note: 'surface-aware' },
  { key: 'Shift+s', action: 'Save & Star', note: 'Inbox only' },
  { key: 't', action: 'Trash', note: 'any non-trash surface' },
  { key: 'e', action: 'Set Aside', note: 'Reading → Reading List; no-op elsewhere' },
  { key: 'r', action: 'Restore', note: 'Trash only' },
  { key: 'd', action: 'Done', note: 'Reading or to_read only' },
  { key: 'o / Enter', action: 'Open Paper Detail' },
  { key: '?', action: 'Show this cheat sheet' },
  { key: 'Esc', action: 'Clear bulk selection' },
];

/**
 * Global keyboard shortcuts dialog. Mounted once at the AppShell level.
 * Visibility controlled by `useKeyboardShortcuts` store; opened by the TopBar icon
 * button (visible on every authenticated page) or by the `?` keypress on the
 * Research Feed.
 *
 * Shortcuts are currently active ONLY on the Research Feed surface — header
 * makes that scope clear so the dialog is informative on other pages.
 */
export function KeyboardCheatSheet() {
  const isOpen = useKeyboardShortcuts((s) => s.isOpen);
  const close = useKeyboardShortcuts((s) => s.close);

  return (
    <Dialog open={isOpen} onOpenChange={(v) => !v && close()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Keyboard Shortcuts</DialogTitle>
          <DialogDescription>
            Active on the Research Feed (/feed) when no input is focused.
          </DialogDescription>
        </DialogHeader>
        <table className="w-full text-sm">
          <tbody>
            {SHORTCUTS.map((s) => (
              <tr key={s.key} className="border-b last:border-0">
                <td className="py-2 pr-4">
                  <kbd className="rounded border bg-muted px-2 py-1 font-mono text-xs">
                    {s.key}
                  </kbd>
                </td>
                <td className="py-2">
                  {s.action}
                  {s.note && (
                    <span className="ml-2 text-muted-foreground text-xs">({s.note})</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </DialogContent>
    </Dialog>
  );
}
