import { Search } from 'lucide-react';

export function CommandPaletteSearch() {
  return (
    <div className="relative h-9 w-full max-w-[440px] rounded-md border border-hair bg-card hover:bg-paper transition-colors flex items-center px-3 gap-2 cursor-text">
      <Search className="h-3.5 w-3.5 text-faint shrink-0" />
      <span className="flex-1 text-[13px] text-faint select-none">Search papers, notes, cards…</span>
      <div className="flex items-center gap-0.5 shrink-0">
        <kbd className="font-mono text-[10px] px-1.5 py-0.5 rounded border border-hair bg-paper text-meta">⌘</kbd>
        <kbd className="font-mono text-[10px] px-1.5 py-0.5 rounded border border-hair bg-paper text-meta">K</kbd>
      </div>
    </div>
  );
}
