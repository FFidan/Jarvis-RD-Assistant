import type { ReactNode } from 'react';

export interface MarkerCaptionProps {
  id?: string;        // anchor for j/k nav (Phase 2)
  marker: string;     // "Yesterday", "Now", "Today's intent", etc.
  meta?: ReactNode;   // optional middle: count, "edit", etc.
  right?: ReactNode;  // optional far-right action
}

/**
 * Section-level marker caption ("§ Yesterday", "§ Now").
 *
 * Use only when a section contains >=2 sibling sub-blocks each with its
 * own caption. Forbidden directly inside a `TabsContent` or above a
 * `Card` whose `CardTitle` would repeat the marker. See the typography
 * contract: `docs/typography-contract.md`.
 */
export function MarkerCaption({ id, marker, meta, right }: MarkerCaptionProps) {
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
