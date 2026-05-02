import type { ReactNode } from 'react';

interface SectionHeaderProps {
  id?: string;        // anchor for j/k nav (Phase 2)
  marker: string;     // "Yesterday", "Now", "Today's intent", etc.
  meta?: ReactNode;   // optional middle: count, "edit", etc.
  right?: ReactNode;  // optional far-right action
}

export function SectionHeader({ id, marker, meta, right }: SectionHeaderProps) {
  return (
    <div id={id} className="flex items-baseline gap-3 mb-3">
      <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-meta">
        § {marker}
      </span>
      {meta && (
        <span className="font-mono text-[10px] text-faint">{meta}</span>
      )}
      {right && <div className="ml-auto">{right}</div>}
    </div>
  );
}
