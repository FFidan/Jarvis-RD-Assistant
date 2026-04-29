import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';

interface KeyboardCheatSheetProps {
  open: boolean;
  onClose: () => void;
}

const SHORTCUTS: { key: string; action: string; note?: string }[] = [
  { key: 'j / k', action: 'Next / previous row' },
  { key: 's', action: 'Save (Inbox) / Star (Library)', note: 'surface-aware' },
  { key: 'S', action: 'Save & Star', note: 'Inbox only' },
  { key: 'e', action: 'Archive', note: 'Library only' },
  { key: 'd', action: 'Dismiss → Trash' },
  { key: 'r', action: 'Mark Read' },
  { key: 'o / Enter', action: 'Open Paper Detail' },
  { key: '?', action: 'Show this cheat sheet' },
  { key: 'Esc', action: 'Clear bulk selection' },
];

export function KeyboardCheatSheet({ open, onClose }: KeyboardCheatSheetProps) {
  return (
    <Dialog open={open} onOpenChange={(v) => !v && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Keyboard Shortcuts</DialogTitle>
          <DialogDescription>
            Available on the Feed page (when no input is focused).
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
